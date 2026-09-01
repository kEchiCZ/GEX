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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets

from gexlens_engine.storage.parquet_store import BAR_SOURCE_RECONSTRUCTED
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

#: Jak dlouho čekat na další dávku, než se sběr prohlásí za dokončený.
#: Server posílá historii v dávkách a konec nijak neoznamuje.
QUIET_TIMEOUT_S = 3.0
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


def missing_minutes(
    have: set[dt.datetime], since: dt.datetime, until: dt.datetime
) -> list[dt.datetime]:
    """Minuty v okně [since, until), které v `have` chybí.

    Okno je polootevřené: `until` je typicky rozdělaná minuta, kterou ještě
    není co doplňovat.
    """
    minute = since.replace(second=0, microsecond=0)
    out: list[dt.datetime] = []
    while minute < until:
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
    ) -> None:
        self._token_source = token_source
        self._quiet_timeout_s = quiet_timeout_s

    async def fetch(self, request: CandleRange) -> list[CandleBar]:
        """Svíčky pro okno; prázdný seznam = nedostupné (nikdy nevyhazuje).

        Selhání backfillu nesmí shodit start enginu — díra v datech je horší
        stav, ale pořád lepší než nespuštěná pipeline.
        """
        try:
            return await asyncio.wait_for(self._fetch(request), timeout=TOTAL_TIMEOUT_S)
        except Exception as error:
            logger.warning(
                "Backfill svíček %s selhal (%s: %s) — díra zůstává, sběr běží dál",
                request.streamer_symbol,
                type(error).__name__,
                error,
            )
            return []

    async def _fetch(self, request: CandleRange) -> list[CandleBar]:
        url, token = await self._token_source()
        symbol = f"{request.streamer_symbol}{{=1m}}"
        async with websockets.connect(
            url,
            max_size=2**24,
            ping_interval=KEEPALIVE_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
        ) as ws:
            await handshake(ws, token, {"Candle": CANDLE_FIELDS})
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
            bars = await self._collect(ws, request)
        logger.info(
            "Backfill svíček %s: %d barů v okně %s–%s",
            request.streamer_symbol,
            len(bars),
            request.since.isoformat(timespec="minutes"),
            request.until.isoformat(timespec="minutes"),
        )
        return bars

    async def _collect(self, ws: WebSocketLike, request: CandleRange) -> list[CandleBar]:
        """Čte, dokud server posílá; konec pozná podle ticha, ne podle zprávy."""
        by_minute: dict[dt.datetime, CandleBar] = {}
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._quiet_timeout_s)
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
                if request.since <= bar.ts < request.until:
                    by_minute[bar.ts] = bar
        return [by_minute[key] for key in sorted(by_minute)]


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
