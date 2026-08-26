"""Zápis predikcí, per-okno vyhodnocení a přepočet vah (#282).

Tři fáze, které na sebe navazují:

1. **Predikce** vzniká z klasifikace (immutable, S11 — nese `classification_version`).
2. **Outcome** se zapíše, jakmile existuje reakce; jeden řádek na okno.
3. **Váhy** se přepočítají z outcomes v klouzavém okně a vstupují do skóre.

Fáze 2 a 3 jsou oddělené schválně: reakce se dopočítávají zpětně, takže
outcome může přijít dlouho po predikci, a váhy se nesmí opírat o to, co
zrovna doběhlo.
"""

import datetime as dt
import logging

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    news_classifications,
    news_events,
    news_prediction_outcomes,
    news_predictions,
    news_reactions,
    news_weights,
)
from gexlens_news.predictions import (
    DEFAULT_PRIMARY_WINDOW_MIN,
    DEFAULT_ROLLING_DAYS,
    Outcome,
    compute_weights,
)

logger = logging.getLogger(__name__)


class PredictionJob:
    """Predikce z klasifikace + jejich vyhodnocení proti naměřeným reakcím."""

    def __init__(
        self,
        engine: Engine,
        *,
        primary_window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
        rolling_days: int = DEFAULT_ROLLING_DAYS,
    ) -> None:
        self._engine = engine
        self._primary_window = primary_window_min
        self._rolling_days = rolling_days

    # ── 1) Predikce ────────────────────────────────────────────────

    def create_predictions(self, now: dt.datetime, *, limit: int = 500) -> int:
        """Založí predikci pro klasifikace, které ji ještě nemají."""
        existing = select(news_predictions.c.event_id, news_predictions.c.predictor)
        with self._engine.connect() as conn:
            already = {(row.event_id, row.predictor) for row in conn.execute(existing)}
            rows = conn.execute(
                select(
                    news_classifications.c.event_id,
                    news_classifications.c.version,
                    news_classifications.c.source,
                    news_classifications.c.direction,
                    news_classifications.c.strength,
                )
                # Stínová ngram hlava (#740 fáze 2) predikce NEzakládá:
                # direction=0 by plnilo outcomes prohrami a řádek v
                # `news_weights` by přes `load_weight_map` (dict per kategorie,
                # bez filtru predictoru) mohl přepsat váhu pravidel
                .where(news_classifications.c.source != "ngram")
                .order_by(news_classifications.c.event_id.desc())
                .limit(limit)
            ).fetchall()

        pending = [
            {
                "event_id": int(row.event_id),
                "predicted_dir": int(row.direction or 0),
                "predicted_strength": float(row.strength) if row.strength is not None else None,
                "predictor": row.source,
                "classification_version": int(row.version),
                "created_at": now,
            }
            for row in rows
            if (row.event_id, row.source) not in already and row.direction is not None
        ]
        if not pending:
            return 0
        with self._engine.begin() as conn:
            conn.execute(insert(news_predictions), pending)
        logger.info("Predikce: %d nových", len(pending))
        return len(pending)

    # ── 2) Vyhodnocení per okno ────────────────────────────────────

    def evaluate(self, now: dt.datetime) -> int:
        """Zapíše outcomes pro predikce, jejichž reakce už existují."""
        measured = select(
            news_prediction_outcomes.c.prediction_id,
            news_prediction_outcomes.c.symbol,
            news_prediction_outcomes.c.window_min,
        )
        stmt = select(
            news_predictions.c.id,
            news_predictions.c.predicted_dir,
            news_reactions.c.symbol,
            news_reactions.c.window_min,
            news_reactions.c.ret_bp,
            news_reactions.c.contaminated,
        ).select_from(
            news_predictions.join(
                news_reactions, news_reactions.c.event_id == news_predictions.c.event_id
            )
        )
        with self._engine.connect() as conn:
            done = {(r.prediction_id, r.symbol, r.window_min) for r in conn.execute(measured)}
            rows = conn.execute(stmt).fetchall()

        pending = []
        for row in rows:
            # Kontaminované okno neměří reakci na tuhle zprávu — vyhodnocovat
            # proti němu by predictoru přičítalo cizí pohyb (SPEC 5.1)
            if row.contaminated:
                continue
            key = (int(row.id), row.symbol, int(row.window_min))
            if key in done:
                continue
            ret_bp = float(row.ret_bp)
            realized = 1 if ret_bp > 0 else (-1 if ret_bp < 0 else 0)
            pending.append(
                {
                    "prediction_id": int(row.id),
                    "symbol": row.symbol,
                    "window_min": int(row.window_min),
                    "realized_dir": realized,
                    "correct": bool(row.predicted_dir != 0 and row.predicted_dir == realized),
                    "computed_at": now,
                }
            )
        if not pending:
            return 0
        with self._engine.begin() as conn:
            conn.execute(insert(news_prediction_outcomes), pending)
        logger.info("Vyhodnocení predikcí: %d oken", len(pending))
        return len(pending)

    # ── 3) Váhy ────────────────────────────────────────────────────

    def load_outcomes(self, now: dt.datetime, symbol: str) -> list[Outcome]:
        since = now - dt.timedelta(days=self._rolling_days)
        stmt = (
            select(
                news_events.c.category,
                news_predictions.c.predictor,
                news_predictions.c.predicted_dir,
                news_prediction_outcomes.c.window_min,
                news_prediction_outcomes.c.realized_dir,
            )
            .select_from(
                news_prediction_outcomes.join(
                    news_predictions,
                    news_predictions.c.id == news_prediction_outcomes.c.prediction_id,
                ).join(news_events, news_events.c.id == news_predictions.c.event_id)
            )
            .where(
                news_prediction_outcomes.c.symbol == symbol,
                news_prediction_outcomes.c.computed_at >= since,
                news_events.c.category.is_not(None),
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            Outcome(
                category=row.category,
                predictor=row.predictor,
                window_min=int(row.window_min),
                predicted_dir=int(row.predicted_dir),
                # Směr stačí — velikost do hit-rate nevstupuje
                realized_ret_bp=float(row.realized_dir),
            )
            for row in rows
        ]

    def recompute_weights(self, now: dt.datetime, *, symbol: str = "ES") -> int:
        """Přepočte váhy symbolu z klouzavého okna; nahradí jen řádky symbolu.

        Per symbol (ADR-0026): outcomes ES a NQ se nesmí míchat — kategorie
        s dobrým track recordem na NQ by jinak nafoukla i váhu v ES indexu.
        """
        weights = compute_weights(
            self.load_outcomes(now, symbol), primary_window_min=self._primary_window
        )
        with self._engine.begin() as conn:
            conn.execute(delete(news_weights).where(news_weights.c.symbol == symbol))
            if weights:
                conn.execute(
                    insert(news_weights),
                    [
                        {
                            "category": w.category,
                            "predictor": w.predictor,
                            "window_min": w.window_min,
                            "symbol": symbol,
                            "n": w.n,
                            "hit_rate": w.hit_rate,
                            "hit_rate_lb": w.hit_rate_lb,
                            "weight": w.weight,
                            "computed_at": now,
                        }
                        for w in weights
                    ],
                )
        if weights:
            logger.info(
                "Váhy %s: %d kategorií, nejlepší %s",
                symbol,
                len(weights),
                max(weights, key=lambda w: w.weight).category,
            )
        return len(weights)

    def run(
        self, now: dt.datetime, *, symbols: tuple[str, ...] = ("ES", "NQ")
    ) -> tuple[int, int, int]:
        created = self.create_predictions(now)
        evaluated = self.evaluate(now)
        weights = 0
        for symbol in symbols:
            weights += self.recompute_weights(now, symbol=symbol)
        return created, evaluated, weights


def load_weight_map(engine: Engine, symbol: str = "ES") -> dict[str, float]:
    """Váhy per kategorie pro škálování skóre (SPEC 5.3), per symbol (ADR-0026).

    Chybějící váha = neutrální 1.0. Do kalibrace se tedy skóre nezkresluje ani
    nenuluje — jen se nezpřesňuje.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_weights.c.category, news_weights.c.weight).where(
                news_weights.c.symbol == symbol
            )
        ).fetchall()
    return {row.category: float(row.weight) for row in rows}
