"""Backfill 1min barů podkladu z dxFeed Candle (#617, fáze 5 epicu #610).

ADR-0024 dnes rekonstrukci po pozdním startu explicitně vzdává: když engine
naběhne uprostřed seance, chybějící část dne zůstane dírou. dxFeed `Candle`
umí historii od `fromTime`, takže díru lze doplnit.

**Doplněk, ne náhrada** (matice vlastnictví ADR-0025): primární zdroj barů
zůstává IBKR historical, tastytrade jen zaplňuje chybějící minuty.

Co se rekonstruovat NEDÁ a nesmí se tak tvářit:

* **CumΔ a cokoli z tick-level toku** — svíčka nese OHLCV, ne jednotlivé
  printy s agresorem. Doplněná minuta má cenu a objem, ale žádný tok.
* Z toho plyne i pravidlo pro UI: rekonstruovaný úsek se musí odlišit,
  protože „doplněno" není totéž co „změřeno" (navazuje na #516).

**Past z ADR-0027 (dekádová kolize):** `/ESU6:XCME` s hlubokým `fromTime`
vrací svíčky z roku 2016. Symbol proto MUSÍ nést plný rok (`/ESU26:XCME`)
a bere se výhradně z chain endpointu — nikdy se neskládá ručně. Tenhle modul
symbol nesestavuje, dostane ho hotový.
"""

import asyncio
import datetime as dt
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets

from gexlens_engine.storage.parquet_store import BAR_SOURCE_RECONSTRUCTED, bar_partition_day
from gexlens_engine.tasty.dxlink import (
    KEEPALIVE_INTERVAL_S,
    PING_TIMEOUT_S,
    WebSocketLike,
    handshake,
    send_json,
)

logger = logging.getLogger(__name__)

#: Pole svíčky v pořadí, v jakém je server posílá v COMPACT formátu
CANDLE_FIELDS = ["eventSymbol", "time", "open", "high", "low", "close", "volume"]

#: Jak dlouho čekat na další NOVOU minutu, než se sběr prohlásí za dokončený.
#: Server posílá historii v dávkách a konec nijak neoznamuje. Ticho se měří
#: jen nad novými minutami (#1253): subskripce `Candle{=1m}` je živá a po
#: historii dál posílá updaty právě tvořící se minuty — u likvidního SPY
#: v RTH každou chvíli, takže „3 s bez jakékoli zprávy" nikdy nenastalo,
#: sběr vyčerpal celý strop a všechny dotažené minuty zahodil.
QUIET_TIMEOUT_S = 3.0
#: Kolik nejvýš čekat na PRVNÍ dávku historie (server ji musí vyhledat).
FIRST_DATA_TIMEOUT_S = 15.0
#: Tvrdý strop, ať jednorázový backfill nikdy nezablokuje start enginu
TOTAL_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class CandleBar:
    """Doplněný bar. Tvarem odpovídá `ibkr.underlying.Bar` (protokol `BarLike`),
    navíc nese `source` — zapisovač podle něj odliší rekonstrukci od měření."""

    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = BAR_SOURCE_RECONSTRUCTED


@dataclass(frozen=True)
class CandleRange:
    """Zadání jednoho doplnění: symbol streameru a okno, které chybí."""

    streamer_symbol: str
    since: dt.datetime
    until: dt.datetime


def partition_days(since: dt.datetime, until: dt.datetime) -> list[dt.date]:
    """UTC dny partic, do kterých okno [since, until] zasahuje (#1002).

    Okno rekonstrukce kopíruje seanci (od 22:00 UTC D−1), takže typicky vrací
    dva dny; kontrola existujících minut musí projít oba.
    """
    first = bar_partition_day(since)
    last = bar_partition_day(until)
    return [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]


def missing_minutes(
    have: set[dt.datetime], since: dt.datetime, until: dt.datetime
) -> list[dt.datetime]:
    """Minuty v okně [since, until), které v `have` chybí.

    Okno je polootevřené: `until` je typicky rozdělaná minuta, kterou ještě
    není co doplňovat.
    """
    minute = since.replace(second=0, microsecond=0)
    # `until` se sekundami (typicky `now`) by rozdělanou minutu pustil dovnitř:
    # 14:27:00 < 14:27:23 — a ta se doplnit nedá, každý běh ji hlásil jako
    # díru a její živé updaty držely sběr až do stropu (#1253)
    last = until.replace(second=0, microsecond=0)
    out: list[dt.datetime] = []
    while minute < last:
        if minute not in have:
            out.append(minute)
        minute += dt.timedelta(minutes=1)
    return out


def _row_to_bar(values: list[object]) -> CandleBar | None:
    """COMPACT řádek → Bar; None u neúplné svíčky (server je posílá i prázdné)."""
    if len(values) < len(CANDLE_FIELDS):
        return None
    try:
        ts_ms = float(values[1])  # type: ignore[arg-type]
        open_ = float(values[2])  # type: ignore[arg-type]
        high = float(values[3])  # type: ignore[arg-type]
        low = float(values[4])  # type: ignore[arg-type]
        close = float(values[5])  # type: ignore[arg-type]
        volume = float(values[6]) if values[6] is not None else 0.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if any(value != value for value in (open_, high, low, close)):  # NaN
        return None
    return CandleBar(
        ts=dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.UTC).replace(second=0, microsecond=0),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


class _Progress:
    """Co sběr zatím dotáhl a v jaké je fázi — pro diagnostiku i částečný výsledek."""

    def __init__(self) -> None:
        self.phase = "token"
        self.by_minute: dict[dt.datetime, CandleBar] = {}

    def bars(self) -> list[CandleBar]:
        return [self.by_minute[key] for key in sorted(self.by_minute)]


class CandleFetcher:
    """Jednorázové stažení 1min svíček z DXLink.

    Otevírá si VLASTNÍ krátkodobé spojení a po dotažení ho zavírá. Do živého
    streamu se nesahá schválně: ten je produkční datová cesta a backfill,
    který běží jen po startu, ji nemá čím ohrozit.
    """

    def __init__(
        self,
        token_source: Callable[[], Awaitable[tuple[str, str]]],
        *,
        quiet_timeout_s: float = QUIET_TIMEOUT_S,
        first_data_timeout_s: float = FIRST_DATA_TIMEOUT_S,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
    ) -> None:
        self._token_source = token_source
        self._quiet_timeout_s = quiet_timeout_s
        self._first_data_timeout_s = first_data_timeout_s
        self._total_timeout_s = total_timeout_s

    async def fetch(self, request: CandleRange) -> list[CandleBar]:
        """Svíčky pro okno; prázdný seznam = nedostupné (nikdy nevyhazuje).

        Selhání backfillu nesmí shodit start enginu — díra v datech je horší
        stav, ale pořád lepší než nespuštěná pipeline. Při stropu se vrátí,
        co už dorazilo (uzavřené minuty jsou platné bez ohledu na to, jak
        sběr skončil), a log říká, ve které fázi se čas ztratil (#1253).
        """
        progress = _Progress()
        started = time.monotonic()
        try:
            await asyncio.wait_for(self._fetch(request, progress), timeout=self._total_timeout_s)
        except Exception as error:
            bars = progress.bars()
            logger.warning(
                "Backfill svíček %s selhal ve fázi %s po %.0f s (%s: %s) — %s",
                request.streamer_symbol,
                progress.phase,
                time.monotonic() - started,
                type(error).__name__,
                error,
                f"zapíše se {len(bars)} dotažených minut"
                if bars
                else "díra zůstává, sběr běží dál",
            )
            return bars
        return progress.bars()

    async def _fetch(self, request: CandleRange, progress: _Progress) -> None:
        url, token = await self._token_source()
        symbol = f"{request.streamer_symbol}{{=1m}}"
        progress.phase = "connect"
        async with websockets.connect(
            url,
            max_size=2**24,
            ping_interval=KEEPALIVE_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
        ) as ws:
            progress.phase = "handshake"
            await handshake(ws, token, {"Candle": CANDLE_FIELDS})
            progress.phase = "subscribe"
            await send_json(
                ws,
                {
                    "type": "FEED_SUBSCRIPTION",
                    "channel": 1,
                    "add": [
                        {
                            "type": "Candle",
                            "symbol": symbol,
                            "fromTime": int(request.since.timestamp() * 1000),
                        }
                    ],
                },
            )
            progress.phase = "collect"
            await self._collect(ws, request, progress)
        logger.info(
            "Backfill svíček %s: %d barů v okně %s–%s",
            request.streamer_symbol,
            len(progress.by_minute),
            request.since.isoformat(timespec="minutes"),
            request.until.isoformat(timespec="minutes"),
        )

    async def _collect(self, ws: WebSocketLike, request: CandleRange, progress: _Progress) -> None:
        """Čte, dokud přibývají nové minuty; konec pozná podle ticha nad NIMI.

        Živé updaty rozdělané minuty ani keepalive ticho nepřerušují (#1253) —
        jinak by u likvidního podkladu v RTH sběr nikdy neskončil.
        """
        by_minute = progress.by_minute
        # Rozdělaná minuta (ts == floor(until)) se nesbírá — není uzavřená
        last = request.until.replace(second=0, microsecond=0)
        deadline = time.monotonic() + self._first_data_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break  # dávky došly
            message = json.loads(raw)
            if message.get("type") != "FEED_DATA":
                continue
            for chunk in _chunks(message.get("data") or []):
                bar = _row_to_bar(chunk)
                if bar is None:
                    continue
                # Okno je polootevřené a server rád přidá i minuty mimo
                if not (request.since <= bar.ts < last):
                    continue
                if bar.ts not in by_minute:
                    deadline = time.monotonic() + self._quiet_timeout_s
                by_minute[bar.ts] = bar


def _chunks(data: list[object]) -> list[list[object]]:
    """COMPACT data: [typ, [pole, pole, …, pole]] — rozseká na jednotlivé záznamy."""
    out: list[list[object]] = []
    for index in range(0, len(data), 2):
        if index + 1 >= len(data):
            break
        values = data[index + 1]
        if not isinstance(values, list):
            continue
        width = len(CANDLE_FIELDS)
        for offset in range(0, len(values), width):
            row = values[offset : offset + width]
            if len(row) == width:
                out.append(row)
    return out


async def backfill_gaps(
    fetcher: CandleFetcher,
    *,
    streamer_symbol: str,
    existing: set[dt.datetime],
    since: dt.datetime,
    until: dt.datetime,
) -> list[CandleBar]:
    """Bary pro minuty, které v particii chybí — a JEN pro ně.

    Závěrečný filtr na `wanted` je tvrdá záruka z DoD #617: i kdyby server
    poslal celý den, zapíše se výhradně to, co chybělo. Měřená minuta se
    tedy nemá jak přepsat rekonstruovanou.
    """
    gaps = missing_minutes(existing, since, until)
    if not gaps:
        return []
    bars = await fetcher.fetch(
        CandleRange(
            streamer_symbol=streamer_symbol,
            since=gaps[0],
            until=gaps[-1] + dt.timedelta(minutes=1),
        )
    )
    wanted = set(gaps)
    filled = [bar for bar in bars if bar.ts in wanted]
    logger.info(
        "Rekonstrukce %s: %d děr, doplněno %d (zbývá %d bez svíčky)",
        streamer_symbol,
        len(gaps),
        len(filled),
        len(gaps) - len(filled),
    )
    return filled
