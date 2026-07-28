"""Noční přepočet `news_model_stats` (#279, SPEC 2.4).

Agregáty se počítají celé znovu, ne inkrementálně: reakce se můžou dopočítat
zpětně (archiv barů je věčný) a klasifikace se verzuje, takže inkrement by se
časem rozešel s realitou. Průchod nad pár tisíci řádky je levnější než ta
nejistota.
"""

import datetime as dt
import logging
from collections.abc import Sequence

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events, news_model_stats, news_reactions
from gexlens_news.model_stats import BucketStats, ReactionSample, aggregate_samples

logger = logging.getLogger(__name__)


class ModelStatsJob:
    """Přepočte empirický model z naměřených reakcí."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load_samples(self) -> list[ReactionSample]:
        stmt = select(
            news_events.c.category,
            news_events.c.importance,
            news_events.c.surprise_z,
            news_events.c.sentiment_dir,
            news_reactions.c.symbol,
            news_reactions.c.window_min,
            news_reactions.c.ret_bp,
            news_reactions.c.contaminated,
            news_reactions.c.deferred,
        ).select_from(
            news_reactions.join(news_events, news_events.c.id == news_reactions.c.event_id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            ReactionSample(
                category=row.category,
                importance=row.importance,
                surprise_z=float(row.surprise_z) if row.surprise_z is not None else None,
                sentiment_dir=row.sentiment_dir,
                symbol=row.symbol,
                window_min=int(row.window_min),
                ret_bp=float(row.ret_bp),
                contaminated=bool(row.contaminated),
                deferred=bool(row.deferred),
            )
            for row in rows
        ]

    def store(self, stats: Sequence[BucketStats], now: dt.datetime) -> None:
        """Nahradí celou tabulku — přepočet je vždy úplný."""
        rows = [
            {
                "category": item.key.category,
                "importance": item.key.importance,
                "surprise_bucket": item.key.surprise_bucket,
                "deferred": item.key.deferred,
                "window_min": item.key.window_min,
                "symbol": item.key.symbol,
                "n": item.n,
                "ret_mean_bp": item.ret_mean_bp,
                "ret_median_bp": item.ret_median_bp,
                "ret_sigma_bp": item.ret_sigma_bp,
                "hit_rate": item.hit_rate,
                "hit_rate_lb": item.hit_rate_lb,
                "computed_at": now,
            }
            for item in stats
        ]
        with self._engine.begin() as conn:
            conn.execute(delete(news_model_stats))
            if rows:
                conn.execute(insert(news_model_stats), rows)

    def run(self, now: dt.datetime) -> int:
        """Přepočet; vrací počet bucketů."""
        samples = self.load_samples()
        stats = aggregate_samples(samples)
        self.store(stats, now)
        if stats:
            usable = [s for s in stats if s.n >= 5]
            logger.info(
                "Model stats: %d bucketů z %d oken (%d s n≥5)",
                len(stats),
                len(samples),
                len(usable),
            )
        else:
            logger.info(
                "Model stats: zatím žádný bucket — eventy nemají kategorii "
                "ani importance (doplní klasifikace v N3)"
            )
        return len(stats)
