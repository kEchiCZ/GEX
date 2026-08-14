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
from pydantic import BaseModel, Field
from sqlalchemy import Table, desc, func, insert, select
from sqlalchemy import update as sql_update
from sqlalchemy.engine import Engine

from gexlens_engine.compute.sentwaves import DailyClose, assess_state
from gexlens_engine.storage.sentiment import (
    NEWS_CATEGORIES,
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
    signal_outcomes,
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


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _percentile(ordered: list[float], fraction: float) -> float | None:
    """Nearest-rank percentil seřazené řady; None pro prázdnou."""
    if not ordered:
        return None
    rank = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[rank]


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
        symbol: str = "ES",
    ) -> dict[str, object]:
        """Feed s filtrem; nejnovější první.

        Karta zprávy (#656) nese i naměřený dopad: `reactions_bp` mapuje
        uzavřená párovací okna z `news_reactions` (minuty → ret_bp pro daný
        symbol) a `reaction_contaminated` říká, že do okna spadl jiný významný
        event (pohyb nejde přičíst téhle zprávě, SPEC 5.1).
        """
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
        rows = _rows(engine_factory(), stmt)
        event_ids = [row["id"] for row in rows if isinstance(row.get("id"), int)]
        if event_ids:
            with engine_factory().connect() as conn:
                measured = conn.execute(
                    select(
                        news_reactions.c.event_id,
                        news_reactions.c.window_min,
                        news_reactions.c.ret_bp,
                        news_reactions.c.contaminated,
                    ).where(
                        news_reactions.c.event_id.in_(event_ids),
                        news_reactions.c.symbol == symbol,
                    )
                ).all()
            by_event: dict[int, dict[str, float]] = {}
            contaminated: set[int] = set()
            for event_id, window_min, ret_bp, contam in measured:
                by_event.setdefault(event_id, {})[str(window_min)] = float(ret_bp)
                if contam:
                    contaminated.add(event_id)
            for row in rows:
                event_id = row.get("id")
                row["reactions_bp"] = by_event.get(event_id) if isinstance(event_id, int) else None
                row["reaction_contaminated"] = event_id in contaminated
        return {"news": rows}

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
    def news_stats(regime: str | None = None) -> dict[str, object]:
        """Empirický model pro inspekci — hit-raty per okno včetně Wilson LB.

        `regime` (#402): all / RiskOn / RiskOff / Neutral / gamma_positive /
        gamma_negative; bez filtru se vrací všechny pohledy.
        """
        stmt = select(news_model_stats).order_by(
            news_model_stats.c.category, news_model_stats.c.window_min
        )
        if regime is not None:
            stmt = stmt.where(news_model_stats.c.regime == regime)
        return {"stats": _rows(engine_factory(), stmt)}

    @router.get("/news/latency")
    def news_latency(days: int = Query(7, ge=1, le=14)) -> dict[str, object]:
        """Latence zdrojů: `ts_ingested − ts_event` per zdroj (#358).

        Měří zpoždění ZDROJE, ne naše (cesta je od #335 event-driven).
        Scheduled eventy se neměří — `ts_event` je plánovaný slot, ne čas
        publikace. Latence nad strop se počítají zvlášť (`n_over_cutoff`):
        typicky staré články z prvního fetche feedu nebo backfill, které by
        medián zdroje nesmyslně nafoukly — ale nesmí zmizet beze stopy.
        """
        cutoff_s = 6 * 3600
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        stmt = select(
            news_events.c.source,
            news_events.c.ts_event,
            news_events.c.ts_ingested,
        ).where(
            news_events.c.ts_ingested >= since,
            news_events.c.kind != "scheduled",
        )
        with engine_factory().connect() as conn:
            rows = conn.execute(stmt).fetchall()

        by_source: dict[str, dict[str, Any]] = {}
        ingests: dict[str, list[dt.datetime]] = {}
        for row in rows:
            entry = by_source.setdefault(
                row.source,
                {"source": row.source, "n": 0, "n_over_cutoff": 0, "latencies": []},
            )
            latency = (_utc(row.ts_ingested) - _utc(row.ts_event)).total_seconds()
            if latency < 0:
                continue  # budoucí ts_event u ne-scheduled = vadné razítko zdroje
            if latency > cutoff_s:
                entry["n_over_cutoff"] += 1
                continue
            entry["n"] += 1
            entry["latencies"].append(latency)
            ingests.setdefault(row.source, []).append(_utc(row.ts_ingested))

        out = []
        for entry in by_source.values():
            latencies = sorted(entry.pop("latencies"))
            entry["median_s"] = _percentile(latencies, 0.5)
            entry["p90_s"] = _percentile(latencies, 0.9)
            # Dávkované doručení (#358): podíl eventů do 2 s od jiného ingestu
            # téhož zdroje — u dávek je „latence" zčásti artefakt doručování
            arrivals = sorted(ingests.get(str(entry["source"]), []))
            bursts = sum(
                1
                for index in range(1, len(arrivals))
                if (arrivals[index] - arrivals[index - 1]).total_seconds() <= 2
            )
            entry["batch_share"] = bursts / len(arrivals) if arrivals else None
            out.append(entry)
        out.sort(key=lambda item: str(item["source"]))
        return {"days": days, "cutoff_s": cutoff_s, "latency": out}

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
        """1min řada SentIndexu daného dne (SPEC 5.4), per symbol (ADR-0026).

        Partice `derived/sentiment/{SYMBOL}/{den}.parquet`; historické ploché
        soubory (před ADR-0026) jsou ES legacy — fallback jen pro ES.
        """
        day = date or dt.datetime.now(dt.UTC).date()
        base = data_dir / "derived" / SENTIMENT_SUBDIR
        path = base / symbol / f"{day.isoformat()}.parquet"
        if not path.exists() and symbol == "ES":
            path = base / f"{day.isoformat()}.parquet"
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
        symbol: str | None = None,
        from_date: dt.date | None = Query(None, alias="from"),
        to_date: dt.date | None = Query(None, alias="to"),
    ) -> dict[str, object]:
        """OHLC svíčky sentimentu (SPEC 7.1) — zdroj pro Daily pohled i vlny."""
        stmt = select(sentiment_daily).order_by(sentiment_daily.c.date)
        if symbol is not None:
            stmt = stmt.where(sentiment_daily.c.symbol == symbol)
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
    def sentiment_state(symbol: str = "ES") -> dict[str, object]:
        """Stav RiskOn/RiskOff/Neutral (#292, SPEC 5.6).

        Počítá se sdílenými pravidly z `gexlens_engine.compute.sentwaves` —
        toutéž implementací, kterou news-engine ukládá vlny. Potvrzený stav
        stojí jen na UZAVŘENÝCH dnech; dnešní průběžný close dává pouze
        „unconfirmed" indikaci.
        """
        engine = engine_factory()
        stmt = (
            select(sentiment_daily.c.date, sentiment_daily.c.close)
            .where(sentiment_daily.c.symbol == symbol)
            .order_by(sentiment_daily.c.date)
        )
        with engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        today = dt.datetime.now(dt.UTC).date()
        completed = [
            DailyClose(date=row.date, close=float(row.close)) for row in rows if row.date < today
        ]
        provisional = next(
            (
                DailyClose(date=row.date, close=float(row.close))
                for row in rows
                if row.date == today
            ),
            None,
        )
        confirmed = assess_state(completed)
        provisional_assessment = (
            assess_state([*completed, provisional]) if provisional is not None else confirmed
        )
        wave = confirmed.wave
        return {
            "symbol": symbol,
            "state": confirmed.state,
            "polarity": confirmed.polarity,
            "unconfirmed": provisional is not None
            and provisional_assessment.state != confirmed.state,
            "unconfirmed_state": provisional_assessment.state,
            "last_close": provisional.close if provisional else confirmed.close,
            "ma5": confirmed.ma5,
            "ma10": confirmed.ma10,
            "threshold": confirmed.threshold,
            "current_wave": {
                "direction": wave.direction,
                "start_date": wave.start.isoformat(),
                "end_date": wave.end.isoformat() if wave.end else None,
                "depth": wave.depth,
                "length_days": wave.length_days,
            }
            if wave is not None
            else None,
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
    def signals_route(
        mode: str | None = None,
        from_ts: dt.datetime | None = Query(None, alias="from"),
        to_ts: dt.datetime | None = Query(None, alias="to"),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, object]:
        """Signály včetně `inputs` zdůvodnění a realizované úspěšnosti (#294).

        `from`/`to` vymezují čas signálu — replay konkrétního dne (#295)
        nemá listovat celou historií přes `limit`.
        """
        engine = engine_factory()
        stmt = select(signals).order_by(desc(signals.c.ts)).limit(limit)
        if mode is not None:
            stmt = stmt.where(signals.c.mode == mode)
        if from_ts is not None:
            stmt = stmt.where(signals.c.ts >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(signals.c.ts <= to_ts)
        rows = _rows(engine, stmt)
        if rows:
            ids = [int(row["id"]) for row in rows]
            outcome_rows = _rows(
                engine, select(signal_outcomes).where(signal_outcomes.c.signal_id.in_(ids))
            )
            by_signal: dict[int, list[dict[str, Any]]] = {}
            for outcome in outcome_rows:
                by_signal.setdefault(int(outcome["signal_id"]), []).append(outcome)
            for row in rows:
                row["outcomes"] = sorted(
                    by_signal.get(int(row["id"]), []), key=lambda o: o["window_min"]
                )
        return {"signals": rows}

    @router.get("/review")
    def review_route(resolved: bool = False) -> dict[str, object]:
        """Review fronta s detaily eventů (#293, SPEC 5.7); default nevyřízené."""
        engine = engine_factory()
        stmt = (
            select(
                review_queue.c.event_id,
                review_queue.c.reason,
                review_queue.c.created_at,
                review_queue.c.resolved_at,
                news_events.c.title,
                news_events.c.ts_event,
                news_events.c.category,
                news_events.c.importance,
                news_events.c.sentiment_dir,
                news_events.c.sentiment_score,
                news_events.c.sentiment_source,
            )
            .join(news_events, news_events.c.id == review_queue.c.event_id)
            .order_by(desc(review_queue.c.created_at))
        )
        if not resolved:
            stmt = stmt.where(review_queue.c.resolved_at.is_(None))
        return {"review": _rows(engine, stmt)}

    class ReviewCorrection(BaseModel):
        """Ruční korekce směru/kategorie — aspoň jedno pole (#293)."""

        direction: int | None = Field(None, ge=-1, le=1)
        category: str | None = None

    @router.post("/review/{event_id}")
    def review_correct(event_id: int, correction: ReviewCorrection) -> dict[str, object]:
        """Korekce → NOVÁ verze klasifikace (`source='manual'`, S11).

        Minulé predikce a signály zůstávají nedotčené — nesou verzi, ze
        které vznikly. Denormalizace v `news_events` se přepíše na manual,
        takže korekce se propíše do budoucích výpočtů i trénovacích statistik.
        """
        if correction.direction is None and correction.category is None:
            raise HTTPException(422, "Korekce musí měnit směr nebo kategorii")
        if correction.category is not None and correction.category not in NEWS_CATEGORIES:
            raise HTTPException(422, f"Neznámá kategorie {correction.category!r}")
        engine = engine_factory()
        now = dt.datetime.now(dt.UTC)
        with engine.begin() as conn:
            event = conn.execute(select(news_events).where(news_events.c.id == event_id)).first()
            if event is None:
                raise HTTPException(404, f"Event {event_id} neexistuje")
            latest = conn.execute(
                select(news_classifications)
                .where(news_classifications.c.event_id == event_id)
                .order_by(desc(news_classifications.c.version))
                .limit(1)
            ).first()
            direction = (
                correction.direction
                if correction.direction is not None
                else int(latest.direction if latest is not None else event.sentiment_dir or 0)
            )
            category = (
                correction.category
                or (latest.category if latest is not None else event.category)
                or "OTHER"
            )
            # Síla se korekcí nemění — oprava říká „jiný směr/kategorie",
            # ne „jiná intenzita"; bez předchozí verze neutrální 0.5
            strength = float(latest.strength) if latest is not None else 0.5
            importance = int(latest.importance if latest is not None else event.importance or 1)
            version = int(latest.version) + 1 if latest is not None else 1
            conn.execute(
                insert(news_classifications).values(
                    event_id=event_id,
                    version=version,
                    source="manual",
                    category=category,
                    importance=importance,
                    direction=direction,
                    strength=strength,
                    created_at=now,
                )
            )
            conn.execute(
                sql_update(news_events)
                .where(news_events.c.id == event_id)
                .values(
                    category=category,
                    sentiment_dir=direction,
                    sentiment_score=direction * strength,
                    sentiment_source="manual",
                )
            )
            conn.execute(
                sql_update(review_queue)
                .where(review_queue.c.event_id == event_id)
                .values(resolved_at=now)
            )
        logger.info("Ruční korekce eventu %d: dir=%s, kategorie=%s", event_id, direction, category)
        return {
            "event_id": event_id,
            "version": version,
            "direction": direction,
            "category": category,
            "strength": strength,
        }

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
