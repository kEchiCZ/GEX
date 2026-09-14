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

Výjimka je `recompute_sentindex` (#1150): **vědomý** přepis konkrétního
intervalu dnů toutéž mechanikou jako živý job — partice i svíčky — když se
změnila váha vstupů (ADR-0036) a stará řada je prokazatelně chybná.
"""

import datetime as dt
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events, sentiment_daily
from gexlens_news.prediction_job import event_weight, load_weight_map
from gexlens_news.sentindex import ScoredEvent, daily_ohlc, sent_index_series
from gexlens_news.sentindex_job import SentIndexJob

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


def load_scored_events(
    engine: Engine,
    symbol: str = "ES",
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> list[ScoredEvent]:
    """Skórované eventy chronologicky — stejné filtry jako živý index.

    Váhy per symbol (ADR-0026): NQ řada se počítá z týchž eventů, ale s NQ
    vahami — proto se eventy načítají per symbol, ne jednou pro všechny.
    `since`/`until` omezí okno (retro přepočet nepotřebuje celou historii).
    """
    weights = load_weight_map(engine, symbol)
    stmt = (
        select(
            news_events.c.ts_event,
            news_events.c.category,
            news_events.c.importance,
            news_events.c.sentiment_score,
            news_events.c.sentiment_source,
        )
        .where(
            news_events.c.sentiment_score.is_not(None),
            news_events.c.category.is_not(None),
        )
        .order_by(news_events.c.ts_event)
    )
    if since is not None:
        stmt = stmt.where(news_events.c.ts_event >= since)
    if until is not None:
        stmt = stmt.where(news_events.c.ts_event <= until)
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return [
        ScoredEvent(
            ts_event=_as_utc(row.ts_event),
            category=row.category,
            importance=int(row.importance or 1),
            score=float(row.sentiment_score)
            * event_weight(weights, row.category, row.sentiment_source),
        )
        for row in rows
        if row.sentiment_score
    ]


def backfill_sentiment_daily(
    engine: Engine,
    *,
    symbols: tuple[str, ...] = ("ES", "NQ"),
    end: dt.date | None = None,
    step_minutes: int = 1,
) -> BackfillDailyStats:
    """Denní svíčky od prvního skórovaného eventu do `end` (default včerejšek).

    Den po dni: 1min řada z eventů v příspěvkovém okně → OHLC → insert
    ON CONFLICT DO NOTHING. Idempotentní — opakovaný běh nic nepřepíše.
    """
    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    now = dt.datetime.now(dt.UTC)
    today = now.date()
    last_day = end or (today - dt.timedelta(days=1))
    written = 0
    skipped = 0
    for symbol in symbols:
        events = load_scored_events(engine, symbol)
        if not events:
            logger.warning("%s: žádné skórované eventy — backfill nemá z čeho počítat", symbol)
            continue
        first_day = events[0].ts_event.date()
        timestamps = [event.ts_event for event in events]
        day = first_day
        while day <= last_day:
            day_start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
            day_end = day_start + dt.timedelta(days=1) - dt.timedelta(minutes=step_minutes)
            window_start = day_start - dt.timedelta(days=CONTRIBUTION_LOOKBACK_DAYS)
            window = events[
                bisect_left(timestamps, window_start) : bisect_right(timestamps, day_end)
            ]
            series = sent_index_series(window, day_start, day_end, step_minutes=step_minutes)
            candle = daily_ohlc(series, day)
            day = day + dt.timedelta(days=1)
            if candle is None:
                continue
            with engine.begin() as conn:
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


@dataclass(frozen=True)
class RecomputeStats:
    days_written: int  # součet přes symboly
    z_rows: int  # řádky sentiment_daily s nově dopočteným σ/close_z

    def describe(self) -> str:
        return (
            f"přepsáno {self.days_written} dní (partice + svíčka), σ/close_z u {self.z_rows} řádků"
        )


def recompute_sentindex(
    engine: Engine,
    data_dir: Path,
    *,
    start: dt.date,
    end: dt.date,
    symbols: tuple[str, ...] = ("ES", "NQ"),
    now: dt.datetime | None = None,
    step_minutes: int = 1,
) -> RecomputeStats:
    """Přepočte 1min partice a denní svíčky za interval dnů z eventů (#1150).

    Tatáž mechanika jako živý `SentIndexJob` (řada se počítá celá znovu
    z eventů, partice se přepisuje, svíčka upsertuje), jen pro zvolené dny.
    Dnešek se počítá jen do `now` — zbytek doplní živý job. Protože se
    mění denní close, σ a close_z (#640) se od `start` dál smažou a dopočtou
    znovu — `refresh_z` je jinak nechává jako immutabilní historii.
    """
    if end < start:
        raise ValueError(f"end {end} < start {start}")
    moment = now or dt.datetime.now(dt.UTC)
    job = SentIndexJob(engine, data_dir, symbols=symbols)
    written = 0
    z_rows = 0
    step = dt.timedelta(minutes=step_minutes)
    first_start = dt.datetime.combine(start, dt.time(0, 0), tzinfo=dt.UTC)
    last_end = dt.datetime.combine(end, dt.time(0, 0), tzinfo=dt.UTC) + dt.timedelta(days=1)
    for symbol in symbols:
        events = load_scored_events(
            engine,
            symbol,
            since=first_start - dt.timedelta(days=CONTRIBUTION_LOOKBACK_DAYS),
            until=min(last_end, moment),
        )
        timestamps = [event.ts_event for event in events]
        day = start
        while day <= end:
            day_start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
            day_end = min(day_start + dt.timedelta(days=1) - step, moment)
            day = day + dt.timedelta(days=1)
            if day_end < day_start:
                continue  # den v budoucnosti
            window_start = day_start - dt.timedelta(days=CONTRIBUTION_LOOKBACK_DAYS)
            window = events[
                bisect_left(timestamps, window_start) : bisect_right(timestamps, day_end)
            ]
            series = sent_index_series(window, day_start, day_end, step_minutes=step_minutes)
            job.write_series(day_start.date(), series, symbol)
            job.store_daily(day_start.date(), series, symbol)
            written += 1
        with engine.begin() as conn:
            conn.execute(
                sentiment_daily.update()
                .where(sentiment_daily.c.symbol == symbol, sentiment_daily.c.date >= start)
                .values(sigma=None, close_z=None)
            )
        z_rows += job.refresh_z(symbol)
        logger.info(
            "Retro SentIndex %s: %s – %s přepočteno (%d eventů)", symbol, start, end, len(events)
        )
    return RecomputeStats(days_written=written, z_rows=z_rows)
