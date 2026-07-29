"""Backfill denních svíček SentIndexu z historických eventů (#375).

Sentiment waves (#292) potřebují MA10 z denních close — živý index běží od
28. 7., takže bez historie by stav byl Neutral ~2 týdny. Historický dataset
z FF backfillu (#277) je ale skórovaný (surprise_z + konvence řad), takže
denní OHLC jde spočítat zpětně **toutéž mechanikou jako živý index**
(`sent_index_series` + `daily_ohlc`, váhy neutrální 1.0).

Vědomé zjednodušení: skóre = aktuální (poslední) verze klasifikace, ne
point-in-time rekonstrukce. Pro kalibrační období vln je to v pořádku —
track record (#298) reportuje jen vyhodnocovací období (SPEC 5.6 split).

Živě spočítané dny se NIKDY nepřepisují (ON CONFLICT DO NOTHING) — backfill
doplňuje jen díry před startem živého sběru.
"""

import datetime as dt
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events, sentiment_daily
from gexlens_news.prediction_job import load_weight_map
from gexlens_news.sentindex import ScoredEvent, daily_ohlc, sent_index_series

logger = logging.getLogger(__name__)

# Kolik dní zpět event ještě citelně přispívá — nejdelší τ je 240 min × 1.5
# (GEOPOLITICS, importance 3); po 2 dnech zbývá < 0.4 % příspěvku
CONTRIBUTION_LOOKBACK_DAYS = 2


@dataclass(frozen=True)
class BackfillDailyStats:
    days_written: int
    days_skipped: int  # už existovaly (živý sběr) — nedotčené

    def describe(self) -> str:
        return f"zapsáno {self.days_written} dní, {self.days_skipped} existujících nedotčeno"


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def load_scored_events(engine: Engine) -> list[ScoredEvent]:
    """Všechny skórované eventy chronologicky — stejné filtry jako živý index."""
    weights = load_weight_map(engine)
    stmt = (
        select(
            news_events.c.ts_event,
            news_events.c.category,
            news_events.c.importance,
            news_events.c.sentiment_score,
        )
        .where(
            news_events.c.sentiment_score.is_not(None),
            news_events.c.category.is_not(None),
        )
        .order_by(news_events.c.ts_event)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return [
        ScoredEvent(
            ts_event=_as_utc(row.ts_event),
            category=row.category,
            importance=int(row.importance or 1),
            score=float(row.sentiment_score) * weights.get(row.category, 1.0),
        )
        for row in rows
        if row.sentiment_score
    ]


def backfill_sentiment_daily(
    engine: Engine,
    *,
    symbols: tuple[str, ...] = ("ES",),
    end: dt.date | None = None,
    step_minutes: int = 1,
) -> BackfillDailyStats:
    """Denní svíčky od prvního skórovaného eventu do `end` (default včerejšek).

    Den po dni: 1min řada z eventů v příspěvkovém okně → OHLC → insert
    ON CONFLICT DO NOTHING. Idempotentní — opakovaný běh nic nepřepíše.
    """
    events = load_scored_events(engine)
    if not events:
        logger.warning("Žádné skórované eventy — backfill nemá z čeho počítat")
        return BackfillDailyStats(days_written=0, days_skipped=0)

    today = dt.datetime.now(dt.UTC).date()
    last_day = end or (today - dt.timedelta(days=1))
    first_day = events[0].ts_event.date()
    timestamps = [event.ts_event for event in events]

    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    now = dt.datetime.now(dt.UTC)
    written = 0
    skipped = 0
    day = first_day
    while day <= last_day:
        day_start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
        day_end = day_start + dt.timedelta(days=1) - dt.timedelta(minutes=step_minutes)
        window_start = day_start - dt.timedelta(days=CONTRIBUTION_LOOKBACK_DAYS)
        window = events[bisect_left(timestamps, window_start) : bisect_right(timestamps, day_end)]
        series = sent_index_series(window, day_start, day_end, step_minutes=step_minutes)
        candle = daily_ohlc(series, day)
        day = day + dt.timedelta(days=1)
        if candle is None:
            continue
        with engine.begin() as conn:
            for symbol in symbols:
                stmt = (
                    insert(sentiment_daily)
                    .values(
                        date=candle.date,
                        symbol=symbol,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        update_time=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[sentiment_daily.c.date, sentiment_daily.c.symbol]
                    )
                    .returning(sentiment_daily.c.date)
                )
                if conn.execute(stmt).first() is not None:
                    written += 1
                else:
                    skipped += 1
    logger.info("Backfill sentiment_daily: %d zapsáno, %d existujících", written, skipped)
    return BackfillDailyStats(days_written=written, days_skipped=skipped)
