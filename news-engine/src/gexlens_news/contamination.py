"""Kontaminace K1 reakcí: přepočet příznaků `cont_<w>` (#1522, ADR-0045 bod 7).

`ReactionJob` zapisuje příznak jednou, při měření reakce, z eventů, které už
jsou v DB. Event zapsaný později s `ts_event` v okně se do spočtené reakce
nepromítne: pozdní příjem (Finnhub, IBKR BRFG, `rss_news`), první stažení
nového zdroje, backfill. Tytéž funkce proto volá hodinový přepočet posledních
7 dní v news-engine (`ContaminationJob`, rozhodnutí vlastníka 12. 10.:
varianta D) i `scripts/reclassify_news_rules.py` nad celou historií.
"""

from __future__ import annotations

import bisect
import datetime as dt
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import bindparam, select, update
from sqlalchemy.engine import Connection, Engine

from gexlens_engine.storage.sentiment import news_events, news_reactions
from gexlens_news.bars import BARS_SUBDIR
from gexlens_news.reaction_job import CLOSURE_LOOKAHEAD_DAYS, CONTAMINATION_MIN_IMPORTANCE
from gexlens_news.reactions import DEFAULT_WINDOWS, contaminates, window_contaminated

logger = logging.getLogger(__name__)

#: Rozsah kontaminujících eventů po zprávě — jako `ReactionJob._contaminating`
CONTAMINATION_SPAN = dt.timedelta(minutes=max(DEFAULT_WINDOWS) + 1)
#: Hodinový přepočet (#1522 D): drift K1 je vidět nejvýš hodinu
CONTAMINATION_REFRESH_EVERY = dt.timedelta(hours=1)
#: Jak daleko zpět. Pozdní příjem je hodiny až dny (Finnhub medián ~11 h,
#: IBKR páska po přihlášení Gateway 2,4 dne); starší backfill pokryje skript
CONTAMINATION_REFRESH_LOOKBACK = dt.timedelta(days=7)


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


class FirstBars:
    """První obchodovaný bar ≥ čas per symbol — jen sloupec `ts_min`, cache per partice."""

    def __init__(self, data_dir: Path) -> None:
        self._derived = data_dir / "derived"
        self._days: dict[tuple[str, dt.date], list[dt.datetime]] = {}
        self.partition_reads = 0

    def _stamps(self, symbol: str, day: dt.date) -> list[dt.datetime]:
        key = (symbol, day)
        if key not in self._days:
            path = self._derived / symbol / BARS_SUBDIR / f"{day.isoformat()}.parquet"
            stamps: list[dt.datetime] = []
            if path.exists():
                self.partition_reads += 1
                column = pq.read_table(path, columns=["ts_min"]).column("ts_min").to_pylist()
                stamps = sorted(_utc(ts) for ts in column if ts is not None)
            self._days[key] = stamps
        return self._days[key]

    def first_at_or_after(self, symbol: str, moment: dt.datetime) -> dt.datetime | None:
        """Stejný rozsah jako `ReactionJob` (±1 den partic kolem 5denního výhledu).

        Minuta může ležet i v partici sousedního dne (#1002), proto minimum přes
        všechny partice rozsahu, ne první nalezená.
        """
        limit = moment + dt.timedelta(days=CLOSURE_LOOKAHEAD_DAYS) + CONTAMINATION_SPAN
        best: dt.datetime | None = None
        day = moment.date() - dt.timedelta(days=1)
        while day <= limit.date() + dt.timedelta(days=1):
            stamps = self._stamps(symbol, day)
            index = bisect.bisect_left(stamps, moment)
            if index < len(stamps) and stamps[index] <= limit:
                best = stamps[index] if best is None else min(best, stamps[index])
            day += dt.timedelta(days=1)
        return best


@dataclass
class Contaminators:
    """Časy a kategorie eventů s importance ≥ 2, vzestupně."""

    times: list[dt.datetime] = field(default_factory=list)
    categories: list[str | None] = field(default_factory=list)

    @classmethod
    def build(cls, events: Iterable[tuple[dt.datetime, str | None, int | None]]) -> Contaminators:
        items = sorted(
            (ts, category)
            for ts, category, importance in events
            if (importance or 0) >= CONTAMINATION_MIN_IMPORTANCE
        )
        return cls([ts for ts, _ in items], [category for _, category in items])

    def around(self, ts: dt.datetime, category: str | None) -> list[dt.datetime]:
        """Kontaminující eventy v (ts, ts + 61 min] — jako `ReactionJob._contaminating`."""
        lo = bisect.bisect_right(self.times, ts)
        hi = bisect.bisect_right(self.times, ts + CONTAMINATION_SPAN)
        return [
            self.times[index]
            for index in range(lo, hi)
            if contaminates(category, self.categories[index])
        ]


@dataclass(frozen=True)
class ContaminationChange:
    event_id: int
    symbol: str
    old: dict[int, bool | None]
    new: dict[int, bool | None]


def recompute_contamination(
    reactions: Iterable[Any],
    events: dict[int, tuple[dt.datetime, str | None]],
    contaminators: Contaminators,
    first_bars: FirstBars,
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> list[ContaminationChange]:
    """Nové `cont_<w>` pro řádky reakcí; vrací jen řádky, kde se něco mění.

    Okno bez měření (NULL) zůstává NULL. Deferred řádek bez prvního baru
    (partice mezitím chybí) se nemění — nic se nehádá.
    """
    changes: list[ContaminationChange] = []
    for row in reactions:
        event = events.get(int(row.event_id))
        if event is None:
            continue
        ts_event, category = event
        start = ts_event
        if row.deferred_min:
            first_bar = first_bars.first_at_or_after(str(row.symbol), ts_event)
            if first_bar is None:
                continue
            start = first_bar
        others = contaminators.around(ts_event, category)
        old = {window: row._mapping[f"cont_{window}"] for window in windows}
        new = {
            window: (
                None
                if old[window] is None
                else window_contaminated(ts_event, start, window, others)
            )
            for window in windows
        }
        normalized_old = {w: (None if v is None else bool(v)) for w, v in old.items()}
        if new != normalized_old:
            changes.append(
                ContaminationChange(int(row.event_id), str(row.symbol), normalized_old, new)
            )
    return changes


def reaction_rows(conn: Connection, since: dt.datetime | None = None) -> list[Any]:
    """Řádky reakcí s minutovou fází; `since` = jen eventy s `ts_event ≥ since`."""
    stmt = select(
        news_reactions.c.event_id,
        news_reactions.c.symbol,
        news_reactions.c.deferred_min,
        *(news_reactions.c[f"cont_{window}"] for window in DEFAULT_WINDOWS),
    ).where(news_reactions.c.computed_at_min.is_not(None))
    if since is not None:
        stmt = stmt.join(news_events, news_events.c.id == news_reactions.c.event_id).where(
            news_events.c.ts_event >= since
        )
    return list(conn.execute(stmt).fetchall())


def classified_events(conn: Connection, since: dt.datetime | None = None) -> list[Any]:
    """Čas, kategorie a importance eventů; `since` = jen `ts_event ≥ since`."""
    stmt = select(
        news_events.c.id,
        news_events.c.ts_event,
        news_events.c.category,
        news_events.c.importance,
    )
    if since is not None:
        stmt = stmt.where(news_events.c.ts_event >= since)
    return list(conn.execute(stmt).fetchall())


def write_contamination(
    engine: Engine, changes: Sequence[ContaminationChange], *, batch: int
) -> int:
    statement = (
        update(news_reactions)
        .where(
            news_reactions.c.event_id == bindparam("b_event"),
            news_reactions.c.symbol == bindparam("b_symbol"),
        )
        .values({f"cont_{w}": bindparam(f"b_cont_{w}") for w in DEFAULT_WINDOWS})
    )
    for start in range(0, len(changes), batch):
        chunk = changes[start : start + batch]
        with engine.begin() as conn:
            conn.execute(
                statement,
                [
                    {
                        "b_event": change.event_id,
                        "b_symbol": change.symbol,
                        **{f"b_cont_{w}": change.new[w] for w in DEFAULT_WINDOWS},
                    }
                    for change in chunk
                ],
            )
    return len(changes)


class ContaminationJob:
    """Hodinový přepočet K1 reakcí posledních 7 dní (#1522, varianta D).

    Kontaminující event reakce s `ts_event ≥ since` leží nejvýš 61 min po ní,
    takže stačí eventy od `since`. Přepočet je úplný jako ve skriptu: chytí
    pozdě zapsaný event (False → True) i změnu kategorie (True → False).
    """

    def __init__(
        self,
        engine: Engine,
        data_dir: Path,
        *,
        every: dt.timedelta = CONTAMINATION_REFRESH_EVERY,
        lookback: dt.timedelta = CONTAMINATION_REFRESH_LOOKBACK,
        batch: int = 1000,
    ) -> None:
        self._engine = engine
        self._data_dir = data_dir
        self._every = every
        self._lookback = lookback
        self._batch = batch
        # Poslední přepočet; None = ještě nebyl (první cyklus po startu)
        self._last_at: dt.datetime | None = None

    def run(self, now: dt.datetime) -> int:
        """Vrací počet změněných řádků reakcí; mimo hodinový takt 0."""
        if self._last_at is not None and now - self._last_at < self._every:
            return 0
        self._last_at = now
        since = now - self._lookback
        began = time.perf_counter()
        with self._engine.connect() as conn:
            classified = classified_events(conn, since)
            reactions = reaction_rows(conn, since)
        events = {int(row.id): (_utc(row.ts_event), row.category) for row in classified}
        contaminators = Contaminators.build(
            (_utc(row.ts_event), row.category, row.importance) for row in classified
        )
        changes = recompute_contamination(
            reactions, events, contaminators, FirstBars(self._data_dir)
        )
        written = write_contamination(self._engine, changes, batch=self._batch)
        logger.info(
            "Kontaminace K1 (#1522): %d řádků reakcí od %s, změněno %d (%.1f s)",
            len(reactions),
            since.isoformat(timespec="minutes"),
            written,
            time.perf_counter() - began,
        )
        return written
