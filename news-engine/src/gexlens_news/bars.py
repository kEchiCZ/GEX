"""Čtení 1min barů podkladu z parquet archivu GEXLens (#276).

News-engine si bary jen čte — zapisuje je datový engine. Archiv je věčný
(S4, #275), takže reakce jde přepočítat i zpětně.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path

import pyarrow.parquet as pq

from gexlens_engine.compute.settle import session_bounds, trading_session_date
from gexlens_news.reactions import Bar

logger = logging.getLogger(__name__)

BARS_SUBDIR = "bars"


class BarsRepository:
    """Denní partice `derived/{symbol}/bars/{YYYY-MM-DD}.parquet`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
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
        # Jedna minuta = jeden bar, i když ji nese víc partic (#1002): engine
        # dřív zapsal půlnoční bar i rekonstruovaný večerní blok do partice
        # dne seance vedle měřené kopie v partici UTC dne. Vyhrává řádek z
        # partice, do které minuta podle UTC dne patří; jinak první nalezený.
        by_minute: dict[dt.datetime, Bar] = {}
        while day <= last:
            for bar in self.load_day(symbol, day):
                if bar.ts not in by_minute or bar.ts.date() == day:
                    by_minute[bar.ts] = bar
            day += dt.timedelta(days=1)
        return sorted(
            (bar for bar in by_minute.values() if start <= bar.ts <= end),
            key=lambda bar: bar.ts,
        )

    def recent_sessions(self, symbol: str, before: dt.date, count: int) -> list[Sequence[Bar]]:
        """Posledních `count` **obchodních seancí** (Globex, ADR-0023) před dnem `before`.

        Do #1001 se za seanci brala UTC partice: nedělní pahýl (22:00–23:59)
        i zkrácený pátek se počítaly jako celé seance a žádná minuta dne tak
        neměla 20 vzorků — baseline nikdy nevyhověla a `vol_z` zůstával NULL.
        Seance se skládá přes `trading_session_date` (večer partice D−1 + den D),
        minuta je jednou (dedup v `load_range`, #1002).
        """
        end = session_bounds(before)[0] - dt.timedelta(minutes=1)
        # dost partic na `count` seancí i s víkendy a svátky
        start = end - dt.timedelta(days=2 * count + 14)
        by_session: dict[dt.date, list[Bar]] = {}
        for bar in self.load_range(symbol, start, end):
            by_session.setdefault(trading_session_date(bar.ts), []).append(bar)
        return [by_session[day] for day in sorted(by_session)[-count:]]


#: Partic v paměti: baseline čte ~57 partic na symbol (`recent_sessions`: 2 × 20
#: seancí + 14 dní + kraje) a okno eventu ~13; dva symboly ≈ 140, s rezervou 160.
#: Méně než 140 čte partice dokola (měřeno 64 → 5,5× víc čtení).
CACHED_BARS_CAPACITY = 160


class CachedBars(BarsRepository):
    """Partice barů s omezenou cache pro průchod přes mnoho eventů (#1494).

    Eventy podle času čtou tytéž dny dokola (okno ±5 dní, baseline 57 partic).
    Bez kontroly mtime — patří jen do jednoho průchodu (doměření minutové fáze,
    skript), ne do dlouho žijícího jobu, kterému engine dnešní partici přepisuje.
    """

    def __init__(self, data_dir: Path, capacity: int = CACHED_BARS_CAPACITY) -> None:
        super().__init__(data_dir)
        self._capacity = capacity
        self._cache: dict[tuple[str, dt.date], list[Bar]] = {}

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        key = (symbol, day)
        cached = self._cache.get(key)
        if cached is None:
            cached = super().load_day(symbol, day)
            if len(self._cache) >= self._capacity:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = cached
        return cached
