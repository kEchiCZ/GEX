"""Review fronta — human-in-the-loop (#293, SPEC 5.7).

Do fronty jdou eventy, kde si LLM klasifikace a empirický model odporují
(opačný směr při importance ≥ 2), nebo kde LLM vrátil nízkou jistotu.
Uživatel může směr/kategorii ručně opravit (API POST /review/{id} → nová
verze `source='manual'`, S11); **neopravené položky se po uzavření reakčních
oken vyhodnotí automaticky** — systém funguje i bez zásahů, fronta se
označí `resolved_at` a zmizí ze zvýraznění.

Pinnuté detaily (SPEC hodnoty nechává otevřené):

* nízká jistota = LLM strength < 0.3,
* obě kritéria jen pro importance ≥ 2 (SPEC to říká u rozporu; u nízké
  jistoty by importance 1 frontu zaplavila OTHER šumem),
* rozpor se měří proti bucketu s n ≥ 10 (mělčí bucket není „model"),
* lookback 48 h — fronta je provozní kontrola čerstvých zpráv, ne nástroj
  na procházení historického backfillu.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    news_classifications,
    news_events,
    news_model_stats,
    news_reactions,
    review_queue,
)
from gexlens_news.model_stats import surprise_bucket
from gexlens_news.predictions import DEFAULT_PRIMARY_WINDOW_MIN
from gexlens_news.reactions import DEFAULT_WINDOWS

logger = logging.getLogger(__name__)

LOW_CONFIDENCE_STRENGTH = 0.3
MIN_IMPORTANCE = 2
MIN_BUCKET_SAMPLES = 10
LOOKBACK_HOURS = 48

REASON_DISAGREEMENT = "disagreement"
REASON_LOW_CONFIDENCE = "low_confidence"


class ReviewJob:
    """Plní a auto-uzavírá `review_queue`; běží v reaction_loop po reakcích."""

    def __init__(
        self,
        engine: Engine,
        *,
        symbol: str = "ES",
        primary_window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
    ) -> None:
        self._engine = engine
        self._symbol = symbol
        self._primary_window = primary_window_min

    def _llm_candidates(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Čerstvé eventy s poslední verzí klasifikace od LLM."""
        since = now - dt.timedelta(hours=LOOKBACK_HOURS)
        latest = (
            select(
                news_classifications.c.event_id,
                func.max(news_classifications.c.version).label("version"),
            )
            .group_by(news_classifications.c.event_id)
            .subquery()
        )
        stmt = (
            select(
                news_events.c.id,
                news_events.c.category,
                news_events.c.importance,
                news_events.c.surprise_z,
                news_events.c.market_closed,
                news_classifications.c.direction,
                news_classifications.c.strength,
            )
            .join(latest, latest.c.event_id == news_events.c.id)
            .join(
                news_classifications,
                (news_classifications.c.event_id == latest.c.event_id)
                & (news_classifications.c.version == latest.c.version),
            )
            .where(
                news_events.c.ts_event >= since,
                news_events.c.importance >= MIN_IMPORTANCE,
                news_classifications.c.source == "llm",
            )
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def _bucket_mean(self, candidate: dict[str, Any]) -> float | None:
        stmt = select(news_model_stats.c.n, news_model_stats.c.ret_mean_bp).where(
            news_model_stats.c.category == candidate["category"],
            news_model_stats.c.importance == candidate["importance"],
            news_model_stats.c.surprise_bucket
            == surprise_bucket(
                float(candidate["surprise_z"]) if candidate["surprise_z"] is not None else None
            ),
            news_model_stats.c.deferred == bool(candidate["market_closed"]),
            news_model_stats.c.window_min == self._primary_window,
            news_model_stats.c.symbol == self._symbol,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None or int(row.n) < MIN_BUCKET_SAMPLES:
            return None
        return float(row.ret_mean_bp)

    def run(self, now: dt.datetime) -> int:
        """Zařadí nové položky a auto-uzavře vyhodnocené; vrací počet nových."""
        added = 0
        for candidate in self._llm_candidates(now):
            direction = int(candidate["direction"] or 0)
            strength = float(candidate["strength"] or 0.0)
            reason: str | None = None
            if strength < LOW_CONFIDENCE_STRENGTH:
                reason = REASON_LOW_CONFIDENCE
            elif direction != 0:
                mean = self._bucket_mean(candidate)
                if mean is not None and mean * direction < 0:
                    reason = REASON_DISAGREEMENT
            if reason is None:
                continue
            with self._engine.begin() as conn:
                exists = conn.execute(
                    select(review_queue.c.event_id).where(
                        review_queue.c.event_id == candidate["id"]
                    )
                ).first()
                if exists is None:
                    conn.execute(
                        insert(review_queue).values(
                            event_id=candidate["id"], reason=reason, created_at=now
                        )
                    )
                    added += 1

        # Auto-uzavření až po NEJDELŠÍM okně (SPEC 5.7 „po uzavření oken") —
        # primární okno se zavírá už +5 min a fronta by zmizela dřív, než si
        # jí uživatel všimne; ruční oprava mezitím možná nebyla — nevadí
        measured = select(news_reactions.c.event_id).where(
            news_reactions.c.window_min == max(DEFAULT_WINDOWS)
        )
        with self._engine.begin() as conn:
            resolved = conn.execute(
                update(review_queue)
                .where(review_queue.c.resolved_at.is_(None))
                .where(review_queue.c.event_id.in_(measured))
                .values(resolved_at=now)
            )
        if added or resolved.rowcount:
            logger.info(
                "Review fronta: +%d nových, %d auto-uzavřeno", added, resolved.rowcount or 0
            )
        return added
