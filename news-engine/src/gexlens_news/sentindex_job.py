"""Výpočet a uložení SentIndexu (#283, SPEC 5.4, 5.5 a 7.1).

Ukládání podle SPEC 5.4: 1min řada do `data/derived/sentiment/` (podléhá
14denní retenci enginu, protože je to odvozená řada) a **denní OHLC do
PostgreSQL navždy** — z něj se počítají vlny (5.6) a svíčky (7.1).

News-engine je jediný zapisovatel do `derived/sentiment/`; datový engine si
tam nesahá. Sdílený adresář je tak rozdělený podle vlastnictví, ne zamykáním.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events, sentiment_daily
from gexlens_news.sentindex import (
    ScoredEvent,
    TopicIndex,
    daily_ohlc,
    sent_index_series,
    topic_indexes,
)

logger = logging.getLogger(__name__)

SENTIMENT_SUBDIR = "sentiment"
# Jak daleko zpět se berou eventy do indexu. Delší historie nemá smysl —
# i nejpomalejší kategorie po pár dnech dohasne pod práh (SPEC 5.4).
LOOKBACK_DAYS = 7

SERIES_SCHEMA = pa.schema(
    [
        ("ts_min", pa.timestamp("us", tz="UTC")),
        ("value", pa.float64()),
    ]
)


class SentIndexJob:
    """Přepočte dnešní řadu indexu a uloží denní svíčku."""

    def __init__(
        self,
        engine: Engine,
        data_dir: Path,
        *,
        symbols: Sequence[str] = ("ES",),
    ) -> None:
        self._engine = engine
        self._dir = data_dir / "derived" / SENTIMENT_SUBDIR
        # SPEC 5.4 definuje jeden globální index; symboly jsou tu proto, aby
        # per-symbol vážení (SPEC 6.5) šlo zapnout bez změny schématu
        self._symbols = list(symbols)

    def load_events(self, until: dt.datetime) -> list[ScoredEvent]:
        """Klasifikované události v okně; bez skóre do indexu nevstupují."""
        since = until - dt.timedelta(days=LOOKBACK_DAYS)
        stmt = select(
            news_events.c.ts_event,
            news_events.c.category,
            news_events.c.importance,
            news_events.c.sentiment_score,
        ).where(
            news_events.c.ts_event >= since,
            news_events.c.ts_event <= until,
            news_events.c.sentiment_score.is_not(None),
            news_events.c.category.is_not(None),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            ScoredEvent(
                ts_event=_as_utc(row.ts_event),
                category=row.category,
                importance=int(row.importance or 1),
                score=float(row.sentiment_score),
            )
            for row in rows
            if row.sentiment_score
        ]

    def write_series(self, day: dt.date, series: Sequence[tuple[dt.datetime, float]]) -> Path:
        """Přepíše denní partici — řada se počítá celá znovu z eventů."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{day.isoformat()}.parquet"
        table = pa.Table.from_pylist(
            [{"ts_min": moment, "value": value} for moment, value in series],
            schema=SERIES_SCHEMA,
        )
        pq.write_table(table, path)
        return path

    def store_daily(self, day: dt.date, series: Sequence[tuple[dt.datetime, float]]) -> None:
        candle = daily_ohlc(series, day)
        if candle is None:
            return
        dialect = self._engine.dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        now = dt.datetime.now(dt.UTC)
        with self._engine.begin() as conn:
            for symbol in self._symbols:
                values = {
                    "date": day,
                    "symbol": symbol,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "update_time": now,
                }
                stmt = insert(sentiment_daily).values(**values)
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[sentiment_daily.c.date, sentiment_daily.c.symbol],
                        set_={
                            key: values[key]
                            for key in ("open", "high", "low", "close", "update_time")
                        },
                    )
                )

    def run(self, now: dt.datetime) -> tuple[int, list[TopicIndex]]:
        """Přepočet dneška; vrací délku řady a aktuální topic indexy."""
        events = self.load_events(now)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        series = sent_index_series(events, day_start, now)
        if series:
            self.write_series(now.date(), series)
            self.store_daily(now.date(), series)
        topics = topic_indexes(events, now)
        active = [topic for topic in topics if topic.active]
        logger.info(
            "SentIndex %s: %.3f (z %d eventů), aktivních topiců %d%s",
            now.date(),
            series[-1][1] if series else 0.0,
            len(events),
            len(active),
            f" — {', '.join(t.category for t in active[:3])}" if active else "",
        )
        return len(series), topics


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
