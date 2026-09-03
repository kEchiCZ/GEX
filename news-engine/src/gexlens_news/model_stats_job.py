"""Noční přepočet `news_model_stats` (#279, SPEC 2.4).

Agregáty se počítají celé znovu, ne inkrementálně: reakce se můžou dopočítat
zpětně (archiv barů je věčný) a klasifikace se verzuje, takže inkrement by se
časem rozešel s realitou. Průchod nad pár tisíci řádky je levnější než ta
nejistota.
"""

import datetime as dt
import logging

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.sentwaves import DailyClose, assess_state
from gexlens_engine.storage.sentiment import (
    news_events,
    news_model_stats,
    news_reactions,
    sentiment_daily,
    unpivot_reaction,
)
from gexlens_news.model_stats import BucketStats, ReactionSample, aggregate_by_regime

logger = logging.getLogger(__name__)


class ModelStatsJob:
    """Přepočte empirický model z naměřených reakcí."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _state_by_date(self) -> dict[dt.date, str]:
        """Point-in-time stav pro každý den — z closes PŘED daným dnem (S11).

        Stejná mechanika jako track record: stav dne d se počítá jen z closes
        < d, takže podmíněné buckety nekoukají do budoucnosti.
        """
        stmt = (
            select(sentiment_daily.c.date, sentiment_daily.c.close)
            .where(sentiment_daily.c.symbol == "ES")
            .order_by(sentiment_daily.c.date)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        closes = [DailyClose(date=row.date, close=float(row.close)) for row in rows]
        out: dict[dt.date, str] = {}
        for index, point in enumerate(closes):
            out[point.date] = assess_state(closes[:index]).state if index else "Neutral"
        return out

    def load_samples(self) -> list[ReactionSample]:
        state_by_date = self._state_by_date()
        stmt = select(
            news_events.c.category,
            news_events.c.importance,
            news_events.c.surprise_z,
            news_events.c.sentiment_dir,
            news_events.c.ts_event,
            news_reactions,
        ).select_from(
            news_reactions.join(news_events, news_events.c.id == news_reactions.c.event_id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        # Široký řádek (#998) = až 8 vzorků (jeden per změřené okno)
        return [
            ReactionSample(
                category=row["category"],
                importance=row["importance"],
                surprise_z=float(row["surprise_z"]) if row["surprise_z"] is not None else None,
                sentiment_dir=row["sentiment_dir"],
                symbol=row["symbol"],
                window_min=window.window_min,
                ret_bp=window.ret_bp,
                contaminated=window.contaminated,
                deferred=window.deferred,
                state=state_by_date.get(row["ts_event"].date()),
                gex_regime=window.gex_regime,
            )
            for row in rows
            for window in unpivot_reaction(row)
        ]

    def store(self, stats: list[tuple[str, BucketStats]], now: dt.datetime) -> None:
        """Nahradí celou tabulku — přepočet je vždy úplný."""
        rows = [
            {
                "regime": regime,
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
            for regime, item in stats
        ]
        with self._engine.begin() as conn:
            conn.execute(delete(news_model_stats))
            if rows:
                conn.execute(insert(news_model_stats), rows)

    def run(self, now: dt.datetime) -> int:
        """Přepočet; vrací počet bucketů."""
        samples = self.load_samples()
        stats = aggregate_by_regime(samples)
        self.store(stats, now)
        if stats:
            unconditional = sum(1 for regime, _ in stats if regime == "all")
            logger.info(
                "Model stats: %d řádků (%d nepodmíněných bucketů) z %d oken",
                len(stats),
                unconditional,
                len(samples),
            )
        else:
            logger.info(
                "Model stats: zatím žádný bucket — eventy nemají kategorii "
                "ani importance (doplní klasifikace v N3)"
            )
        return len(stats)
