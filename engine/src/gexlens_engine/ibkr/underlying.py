"""Podkladová data (SPEC 3.6): 5s real-time bary → 1min agregace + historical backfill.

Backfill 1min barů pro aktuální den a `bars_backfill_days` dní zpět běží přes
PacingGuard — aktuální den má nejvyšší prioritu, identické požadavky se
deduplikují. Okno je záměrně nezávislé na retenci (ADR-0022): backfill
nepřeskakuje dny, které už na disku jsou, takže by širší retence znamenala
desítky zbytečných historických dotazů při každém startu.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.pacing import PacingGuard
from gexlens_engine.storage.parquet_store import BAR_SOURCE_HISTORICAL, BarLike

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bar:
    """OHLCV bar; ts = začátek intervalu (UTC)."""

    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    #: Původ minuty (#1055): None = živá cesta (writer zapíše `ibkr`),
    #: `ibkr_hist` = doplněno z IBKR historical — platná cena, ale v tu dobu
    #: engine neměřil. Živá agregace pole nevyplňuje.
    source: str | None = None


class HistoricalClientLike(Protocol):
    """Zdroj historických 1min barů pro jeden den (mock: MockHistoricalClient)."""

    async def fetch_day_bars(self, symbol: str, day: dt.date) -> Sequence[Bar]: ...


MinuteBarCallback = Callable[[Bar], None]


class RealTimeBarAggregator:
    """Agreguje 5s bary z reqRealTimeBars do 1min barů (SPEC 3.6)."""

    def __init__(self, on_minute_bar: MinuteBarCallback) -> None:
        self._on_minute_bar = on_minute_bar
        self._current: Bar | None = None

    @property
    def current(self) -> Bar | None:
        """Rozdělaná (dosud neuzavřená) minuta — zdroj provizorního baru, ADR-0005."""
        return self._current

    def add_5s_bar(self, bar: Bar) -> None:
        minute_start = bar.ts.replace(second=0, microsecond=0)
        current = self._current
        if current is None:
            self._current = Bar(
                ts=minute_start,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            return
        if minute_start != current.ts:
            self._on_minute_bar(current)
            self._current = Bar(
                ts=minute_start,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            return
        self._current = Bar(
            ts=current.ts,
            open=current.open,
            high=max(current.high, bar.high),
            low=min(current.low, bar.low),
            close=bar.close,
            volume=current.volume + bar.volume,
        )

    def flush(self) -> Bar | None:
        """Uzavře a emituje rozpracovanou minutu (konec seance / odpojení)."""
        current = self._current
        self._current = None
        if current is not None:
            self._on_minute_bar(current)
        return current


class BarsStallDetector:
    """Detekce tiché ztráty 5s barů (#221).

    Po nočním výpadku TWS farem přestanou real-time bary chodit, zatímco spot
    ticky jedou dál — svíčky se nekreslí a výpadek je pro uživatele neviditelný.
    Detektor počítá po sobě jdoucí minutové cykly, kdy spot žije, ale žádný bar
    nedorazil; po prahu ohlásí `"stalled"`, po návratu barů `"recovered"`.
    Bez pohybu spotu (zavřený trh, noční přestávka CME) se čítač nezvyšuje —
    chybějící bary tam nejsou závada.

    Po `"stalled"` pipeline stream sama obnoví (#1082: Error 1100/1102 zabije
    `reqRealTimeBars`, aniž by spadlo API spojení, takže reconnect hook neběží).
    Když bary nechodí ani po obnově, po každém dalším prahu se vrací
    `"still_stalled"` — další pokus o obnovu, bez opakování alertu.
    """

    def __init__(self, stall_minutes: int) -> None:
        self._stall_minutes = stall_minutes
        self._quiet_cycles = 0
        self._stalled = False

    @property
    def stalled(self) -> bool:
        return self._stalled

    def observe(self, *, bar_activity: bool, spot_moving: bool) -> str | None:
        """Jeden minutový cyklus; vrací "stalled"/"recovered" právě jednou, jinak None."""
        if bar_activity:
            self._quiet_cycles = 0
            if self._stalled:
                self._stalled = False
                return "recovered"
            return None
        if not spot_moving:
            return None
        self._quiet_cycles += 1
        if self._quiet_cycles < self._stall_minutes:
            return None
        # Práh dosažen: čítač jede od nuly, ať se každý další pokus o obnovu
        # streamu odehraje po stejné době ticha jako ten první
        self._quiet_cycles = 0
        if self._stalled:
            return "still_stalled"
        self._stalled = True
        return "stalled"


#: Relativní odchylka doplněných barů od měřených, nad kterou jde o jiný
#: kontrakt (#1232): U6 × Z6 v roll týdnu září 2026 = 1,5 % u NQ, 0,9 % u ES;
#: pohyb trhu mezi sousedními minutami je řádově 0,05 %
BACKFILL_CONTRACT_TOLERANCE = 0.003
#: Jak daleko (min) se hledá měřený soused doplněného baru
BACKFILL_NEIGHBOUR_MINUTES = 3
#: Minimální počet párů pro výrok — pod ním se nekontroluje (nic měřeného)
BACKFILL_MIN_PAIRS = 5


def contract_mismatch(
    measured: Mapping[dt.datetime, float],
    incoming: Sequence[BarLike],
    *,
    neighbour_minutes: int = BACKFILL_NEIGHBOUR_MINUTES,
    min_pairs: int = BACKFILL_MIN_PAIRS,
) -> float | None:
    """Medián relativní odchylky close doplněných barů od nejbližší měřené minuty.

    Roll týden září 2026 (#1232): engine sledoval dobíhající NQU6, backfill děr
    z IBKR historical přinesl NQZ6 (+430 b) a partice 8.–18. 9. měly svíčky
    skákající o ±400 b každých pár minut. Kalendářní pravidlo nestačí (kdo byl
    front, se změnilo s #1189) — porovnává se s tím, co pipeline opravdu
    měřila. None = málo párů (prázdná partice, jiné hodiny), nelze rozhodnout.
    """
    if not measured or not incoming:
        return None
    keys = sorted(measured)
    deviations: list[float] = []
    for bar in incoming:
        best: float | None = None
        for offset in range(neighbour_minutes + 1):
            for sign in (1, -1) if offset else (1,):
                ts = bar.ts + dt.timedelta(minutes=offset * sign)
                close = measured.get(ts)
                if close and close > 0:
                    best = abs(bar.close - close) / close
                    break
            if best is not None:
                break
        if best is not None:
            deviations.append(best)
    del keys
    if len(deviations) < min_pairs:
        return None
    deviations.sort()
    mid = len(deviations) // 2
    if len(deviations) % 2:
        return deviations[mid]
    return (deviations[mid - 1] + deviations[mid]) / 2


class UnderlyingBackfiller:
    """Historical backfill 1min barů pro den + retention okno pod PacingGuardem."""

    def __init__(
        self, client: HistoricalClientLike, guard: PacingGuard, settings: Settings
    ) -> None:
        self._client = client
        self._guard = guard
        self._settings = settings

    async def backfill(self, symbol: str, end_day: dt.date) -> dict[dt.date, list[Bar]]:
        """Stáhne 1min bary pro end_day a `bars_backfill_days` dní zpět.

        Aktuální den jde s prioritou 0 (UI ho potřebuje první), historie s 1.
        Dny bez dat (víkend/svátek) vrací prázdný seznam — není to chyba.
        """
        days = [
            end_day - dt.timedelta(days=offset)
            for offset in range(self._settings.bars_backfill_days + 1)
        ]

        async def fetch(day: dt.date) -> list[Bar]:
            # Selhání jednoho dne (mrtvá HMDS farma, timeout) nesmí zahodit
            # zbytek okna — den se přeskočí a doplní až příští backfill (#221)
            try:
                return await self._guard.run(
                    key=(symbol, day),
                    func=lambda: self._fetch_day(symbol, day),
                    priority=0 if day == end_day else 1,
                )
            except Exception:
                logger.warning("Backfill %s %s selhal — den se přeskakuje", symbol, day)
                return []

        results = await asyncio.gather(*(fetch(day) for day in days))
        return dict(zip(days, results, strict=True))

    async def backfill_day(self, symbol: str, day: dt.date) -> list[Bar]:
        """Jediný den s nejvyšší prioritou — doplnění díry po výpadku streamu (#221)."""
        return await self._guard.run(
            key=(symbol, day),
            func=lambda: self._fetch_day(symbol, day),
            priority=0,
        )

    async def _fetch_day(self, symbol: str, day: dt.date) -> list[Bar]:
        # Původ se razí tady, ne v klientovi (#1055): každý bar z historical
        # cesty je doplněný, ať ho dodal živý IBKR klient nebo mock
        bars = [
            replace(bar, source=BAR_SOURCE_HISTORICAL)
            for bar in await self._client.fetch_day_bars(symbol, day)
        ]
        logger.debug("Backfill %s %s: %d barů", symbol, day, len(bars))
        return bars
