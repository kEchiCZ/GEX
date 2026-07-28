"""REST endpointy SentimentLensu (#285, SPEC kap. 8).

Routy jsou namespacované (`/sentiment/index/{sym}`, ne `/sentiment/{sym}`),
aby path parametr nespolkl statické `/sentiment/state`, `/topics` a `/daily` —
pořadím registrace se to ve FastAPI ohackovat dá, ale je to křehké.

Endpointy nad tabulkami pozdějších milestones (signály, review fronta, vlny,
track record) tu **jsou už teď a vracejí prázdné výsledky**. Frontend tak má
stabilní kontrakt a N7/N8 mění jen data, ne tvar API.
"""

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Table, desc, func, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    crowd_sentiment,
    news_classifications,
    news_events,
    news_model_stats,
    news_prediction_outcomes,
    news_predictions,
    news_reactions,
    review_queue,
    sentiment_daily,
    sentiment_waves,
    signals,
    track_record,
)

logger = logging.getLogger(__name__)

SENTIMENT_SUBDIR = "sentiment"
DEFAULT_FEED_LIMIT = 200
MAX_FEED_LIMIT = 1000


def _rows(engine: Engine, stmt: Any) -> list[dict[str, Any]]:
    """Výsledek dotazu jako JSON-serializovatelné slovníky."""
    with engine.connect() as conn:
        result = conn.execute(stmt).fetchall()
    out: list[dict[str, Any]] = []
    for row in result:
        record = dict(row._mapping)
        for key, value in record.items():
            if isinstance(value, dt.datetime | dt.date):
                record[key] = value.isoformat()
            elif isinstance(value, Decimal):
                # PG vrací Numeric jako Decimal a ten by se serializoval jako
                # řetězec — frontend pak volá toFixed nad stringem a spadne
                record[key] = float(value)
        out.append(record)
    return out


def _empty_table(engine: Engine, table: Table, **filters: Any) -> list[dict[str, Any]]:
    """Dotaz nad tabulkou pozdějšího milestonu — dnes vrací prázdno, ale tvar drží."""
    stmt = select(table)
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(table.c[column] == value)
    return _rows(engine, stmt)


def build_sentiment_router(engine_factory: Any, data_dir: Path) -> APIRouter:
    """Router; `engine_factory` vrací SQLAlchemy Engine (lazy, sdílený s API)."""
    router = APIRouter(tags=["sentiment"])

    # ── Feed zpráv ─────────────────────────────────────────────────

    @router.get("/news")
    def news_feed(
        from_ts: dt.datetime | None = Query(None, alias="from"),
        to_ts: dt.datetime | None = Query(None, alias="to"),
        category: str | None = None,
        importance: int | None = None,
        kind: str | None = None,
        limit: int = Query(DEFAULT_FEED_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    ) -> dict[str, object]:
        """Feed s filtrem; nejnovější první."""
        stmt = select(news_events).order_by(desc(news_events.c.ts_event)).limit(limit)
        if from_ts is not None:
            stmt = stmt.where(news_events.c.ts_event >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(news_events.c.ts_event <= to_ts)
        if category is not None:
            stmt = stmt.where(news_events.c.category == category)
        if importance is not None:
            stmt = stmt.where(news_events.c.importance >= importance)
        if kind is not None:
            stmt = stmt.where(news_events.c.kind == kind)
        return {"news": _rows(engine_factory(), stmt)}

    @router.get("/news/upcoming")
    def news_upcoming(hours: int = Query(24, ge=1, le=168)) -> dict[str, object]:
        """Nadcházející plánované eventy — podklad pro countdown v UI (9.5)."""
        now = dt.datetime.now(dt.UTC)
        stmt = (
            select(news_events)
            .where(
                news_events.c.kind == "scheduled",
                news_events.c.ts_event > now,
                news_events.c.ts_event <= now + dt.timedelta(hours=hours),
            )
            .order_by(news_events.c.ts_event)
        )
        return {"upcoming": _rows(engine_factory(), stmt)}

    @router.get("/news/stats")
    def news_stats() -> dict[str, object]:
        """Empirický model pro inspekci — hit-raty per okno včetně Wilson LB."""
        stmt = select(news_model_stats).order_by(
            news_model_stats.c.category, news_model_stats.c.window_min
        )
        return {"stats": _rows(engine_factory(), stmt)}

    @router.get("/news/{event_id}")
    def news_detail(event_id: int) -> dict[str, object]:
        """Detail včetně reakcí, **všech verzí klasifikace** a predikcí (S11)."""
        engine = engine_factory()
        event = _rows(engine, select(news_events).where(news_events.c.id == event_id))
        if not event:
            raise HTTPException(404, f"Event {event_id} neexistuje")
        predictions = _rows(
            engine, select(news_predictions).where(news_predictions.c.event_id == event_id)
        )
        outcomes: list[dict[str, Any]] = []
        if predictions:
            ids = [int(p["id"]) for p in predictions]
            outcomes = _rows(
                engine,
                select(news_prediction_outcomes).where(
                    news_prediction_outcomes.c.prediction_id.in_(ids)
                ),
            )
        return {
            "event": event[0],
            "reactions": _rows(
                engine, select(news_reactions).where(news_reactions.c.event_id == event_id)
            ),
            # Historie verzí, ne jen poslední — bez ní nejde rekonstruovat,
            # co systém věděl v okamžiku predikce
            "classifications": _rows(
                engine,
                select(news_classifications)
                .where(news_classifications.c.event_id == event_id)
                .order_by(news_classifications.c.version),
            ),
            "predictions": predictions,
            "outcomes": outcomes,
        }

    # ── Sentiment ──────────────────────────────────────────────────

    @router.get("/sentiment/index/{symbol}")
    def sentiment_index(symbol: str, date: dt.date | None = None) -> dict[str, object]:
        """1min řada SentIndexu daného dne (SPEC 5.4)."""
        day = date or dt.datetime.now(dt.UTC).date()
        path = data_dir / "derived" / SENTIMENT_SUBDIR / f"{day.isoformat()}.parquet"
        if not path.exists():
            return {"symbol": symbol, "date": day.isoformat(), "series": []}
        try:
            rows = pq.read_table(path).to_pylist()
        except Exception:
            logger.exception("Nečitelná partice SentIndexu %s", path)
            return {"symbol": symbol, "date": day.isoformat(), "series": []}
        return {
            "symbol": symbol,
            "date": day.isoformat(),
            "series": [
                {"ts_min": row["ts_min"].isoformat(), "value": row["value"]} for row in rows
            ],
        }

    @router.get("/sentiment/daily")
    def sentiment_daily_route(
        from_date: dt.date | None = Query(None, alias="from"),
        to_date: dt.date | None = Query(None, alias="to"),
    ) -> dict[str, object]:
        """OHLC svíčky sentimentu (SPEC 7.1) — zdroj pro Daily pohled i vlny."""
        stmt = select(sentiment_daily).order_by(sentiment_daily.c.date)
        if from_date is not None:
            stmt = stmt.where(sentiment_daily.c.date >= from_date)
        if to_date is not None:
            stmt = stmt.where(sentiment_daily.c.date <= to_date)
        return {"daily": _rows(engine_factory(), stmt)}

    @router.get("/sentiment/topics")
    def sentiment_topics(active: int = Query(0)) -> dict[str, object]:
        """Topic indexy dle kategorií (SPEC 5.5).

        Počítá se z živých dat, ne z uložené řady — topic index je okamžitá
        hodnota a ukládat ho zvlášť by znamenalo držet N řad navíc.
        """
        from gexlens_news.sentindex import ScoredEvent, topic_indexes

        now = dt.datetime.now(dt.UTC)
        stmt = select(
            news_events.c.ts_event,
            news_events.c.category,
            news_events.c.importance,
            news_events.c.sentiment_score,
        ).where(
            news_events.c.ts_event >= now - dt.timedelta(days=7),
            news_events.c.sentiment_score.is_not(None),
            news_events.c.category.is_not(None),
        )
        with engine_factory().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        events = [
            ScoredEvent(
                ts_event=r.ts_event if r.ts_event.tzinfo else r.ts_event.replace(tzinfo=dt.UTC),
                category=r.category,
                importance=int(r.importance or 1),
                score=float(r.sentiment_score),
            )
            for r in rows
        ]
        topics = topic_indexes(events, now)
        if active:
            topics = [topic for topic in topics if topic.active]
        return {
            "topics": [
                {
                    "category": topic.category,
                    "value": topic.value,
                    "events_in_window": topic.events_in_window,
                    "active": topic.active,
                }
                for topic in topics
            ]
        }

    @router.get("/sentiment/state")
    def sentiment_state() -> dict[str, object]:
        """Stav RiskOn/RiskOff/Neutral — vlny přijdou v N7 (#292)."""
        engine = engine_factory()
        waves = _rows(
            engine, select(sentiment_waves).order_by(desc(sentiment_waves.c.start_date)).limit(1)
        )
        last = _rows(
            engine, select(sentiment_daily).order_by(desc(sentiment_daily.c.date)).limit(1)
        )
        return {
            "state": "unknown",  # vlny zatím nejsou počítané (N7)
            "unconfirmed": False,
            "last_close": last[0]["close"] if last else None,
            "current_wave": waves[0] if waves else None,
        }

    @router.get("/sentiment/crowd")
    def sentiment_crowd(
        source: str | None = None,
        from_ts: dt.datetime | None = Query(None, alias="from"),
        to_ts: dt.datetime | None = Query(None, alias="to"),
    ) -> dict[str, object]:
        """Crowd řady (SPEC 2.6) — plní se v N6 (#290)."""
        stmt = select(crowd_sentiment).order_by(crowd_sentiment.c.ts)
        if source is not None:
            stmt = stmt.where(crowd_sentiment.c.source == source)
        if from_ts is not None:
            stmt = stmt.where(crowd_sentiment.c.ts >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(crowd_sentiment.c.ts <= to_ts)
        return {"crowd": _rows(engine_factory(), stmt)}

    # ── Milestones N7/N8 — tvar API drží, data přibudou ────────────

    @router.get("/signals")
    def signals_route(mode: str | None = None) -> dict[str, object]:
        """Signály včetně `inputs` zdůvodnění — Signal engine je N7 (#294)."""
        return {"signals": _empty_table(engine_factory(), signals, mode=mode)}

    @router.get("/review")
    def review_route() -> dict[str, object]:
        """Review fronta — plní se v N7 (#293)."""
        return {"review": _empty_table(engine_factory(), review_queue)}

    @router.get("/stats/waves")
    def stats_waves() -> dict[str, object]:
        """Statistika vln — N8 (#297)."""
        return {"waves": _empty_table(engine_factory(), sentiment_waves)}

    @router.get("/stats/trackrecord")
    def stats_trackrecord(strategy: str | None = None) -> dict[str, object]:
        """Equity křivky — N8 (#298)."""
        return {"track_record": _empty_table(engine_factory(), track_record, strategy=strategy)}

    @router.get("/sentiment/summary")
    def sentiment_summary() -> dict[str, object]:
        """Rychlý přehled pro UI: kolik dat modul má."""
        engine = engine_factory()
        with engine.connect() as conn:
            events = conn.execute(select(func.count()).select_from(news_events)).scalar() or 0
            reactions = conn.execute(select(func.count()).select_from(news_reactions)).scalar() or 0
            buckets = conn.execute(select(func.count()).select_from(news_model_stats)).scalar() or 0
        return {"events": int(events), "reactions": int(reactions), "model_buckets": int(buckets)}

    return router
