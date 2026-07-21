"""Podkladová data (SPEC 3.6): 5s real-time bary → 1min agregace + historical backfill.

Backfill 1min barů pro aktuální den a `retention_days` dní zpět běží přes
PacingGuard — aktuální den má nejvyšší prioritu, identické požadavky se
deduplikují.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.pacing import PacingGuard

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


class UnderlyingBackfiller:
    """Historical backfill 1min barů pro den + retention okno pod PacingGuardem."""

    def __init__(
        self, client: HistoricalClientLike, guard: PacingGuard, settings: Settings
    ) -> None:
        self._client = client
        self._guard = guard
        self._settings = settings

    async def backfill(self, symbol: str, end_day: dt.date) -> dict[dt.date, list[Bar]]:
        """Stáhne 1min bary pro end_day a retention_days dní zpět.

        Aktuální den jde s prioritou 0 (UI ho potřebuje první), historie s 1.
        Dny bez dat (víkend/svátek) vrací prázdný seznam — není to chyba.
        """
        days = [
            end_day - dt.timedelta(days=offset)
            for offset in range(self._settings.retention_days + 1)
        ]

        async def fetch(day: dt.date) -> list[Bar]:
            bars = await self._guard.run(
                key=(symbol, day),
                func=lambda: self._fetch_day(symbol, day),
                priority=0 if day == end_day else 1,
            )
            return bars

        results = await asyncio.gather(*(fetch(day) for day in days))
        return dict(zip(days, results, strict=True))

    async def _fetch_day(self, symbol: str, day: dt.date) -> list[Bar]:
        bars = list(await self._client.fetch_day_bars(symbol, day))
        logger.debug("Backfill %s %s: %d barů", symbol, day, len(bars))
        return bars
