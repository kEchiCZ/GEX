"""Čtení 1min barů podkladu z parquet archivu GEXLens (#276).

News-engine si bary jen čte — zapisuje je datový engine. Archiv je věčný
(S4, #275), takže reakce jde přepočítat i zpětně.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path

import pyarrow.parquet as pq

from gexlens_news.reactions import Bar

logger = logging.getLogger(__name__)

BARS_SUBDIR = "bars"


class BarsRepository:
    """Denní partice `derived/{symbol}/bars/{YYYY-MM-DD}.parquet`."""

    def __init__(self, data_dir: Path) -> None:
        self._derived = data_dir / "derived"

    def _path(self, symbol: str, day: dt.date) -> Path:
        return self._derived / symbol / BARS_SUBDIR / f"{day.isoformat()}.parquet"

    def sessions(self, symbol: str) -> list[dt.date]:
        """Dny s dostupnými bary, vzestupně."""
        directory = self._derived / symbol / BARS_SUBDIR
        if not directory.exists():
            return []
        days: list[dt.date] = []
        for path in directory.glob("*.parquet"):
            try:
                days.append(dt.date.fromisoformat(path.stem))
            except ValueError:
                logger.debug("Partice s nečitelným datem: %s", path)
        return sorted(days)

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        path = self._path(symbol, day)
        if not path.exists():
            return []
        try:
            table = pq.read_table(path)
        except Exception:
            logger.exception("Nečitelná partice barů %s — přeskakuji", path)
            return []
        rows = table.to_pylist()
        bars: list[Bar] = []
        for row in rows:
            ts = row.get("ts_min")
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.UTC)
            bars.append(
                Bar(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"] or 0.0),
                )
            )
        return sorted(bars, key=lambda bar: bar.ts)

    def load_range(self, symbol: str, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        """Bary v intervalu; čte i sousední dny, aby okno přes půlnoc nechybělo.

        Prochází **každý** den rozsahu, ne jen okolí krajů. Původní verze četla
        pouze ±1 den kolem `start` a `end`, takže u delšího rozsahu vynechala
        prostředek — a víkendová zpráva pak neměla z čeho měřit (#339).
        """
        day = start.date() - dt.timedelta(days=1)
        last = end.date() + dt.timedelta(days=1)
        collected: list[Bar] = []
        while day <= last:
            collected.extend(self.load_day(symbol, day))
            day += dt.timedelta(days=1)
        return [bar for bar in collected if start <= bar.ts <= end]

    def recent_sessions(self, symbol: str, before: dt.date, count: int) -> list[Sequence[Bar]]:
        """Posledních `count` seancí před dnem `before` — podklad volume baseline."""
        days = [day for day in self.sessions(symbol) if day < before]
        return [self.load_day(symbol, day) for day in days[-count:]]
