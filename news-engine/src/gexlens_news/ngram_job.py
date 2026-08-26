"""Stínový běh magnitudové hlavy ngram modelu (#740 fáze 2).

Směr na out-of-sample datech definitivně neprošel (#749: 56 tis. vzorků,
LB 0,493–0,497), velikost ano (lift 1,44× s leadem z #743). Zapojuje se proto
JEN magnitudová hlava — a ve stínu: predikce se zapisují jako
`news_classifications` se `source='ngram'`, `direction=0` a `strength` =
P(|pohyb| > medián). Do SentIndexu, vah ani signálů nic neteče:
`prediction_job` zdroj `ngram` výslovně přeskakuje a denormalizace
`news_events` se nedotýká.

Tvrdá podmínka před zapojením (#749): měřený náskok pochází z ~90 % z
backfillového korpusu, na živém feedu doložený není. `evaluate` proto počítá
lift ODDĚLENĚ pro živý subset (ts_ingested − ts_event ≤ 1 den), backfill
a celek do `news_ngram_shadow` — brána stejné konstrukce jako Wilson gate:
dokud live lift neporazí kategorie baseline na dostatečném vzorku, hlava se
nezapíná (rozhodnutí je na uživateli, R4).

Model se trénuje 1× denně nad celou historií čistých reakcí (walk-forward
v provozu platí z konstrukce: klasifikuje se vždy modelem učeným jen na
minulosti). Trénuje se na ES — rysy jsou čistě textové (titulek + lead,
zdroj, hodina), skóre se vyhodnocuje proti reakcím ES i NQ zvlášť.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    news_classifications,
    news_events,
    news_ngram_shadow,
    news_reactions,
)
from gexlens_news.ngram_model import LogisticModel, features, hash_row

logger = logging.getLogger(__name__)

NGRAM_SOURCE = "ngram"
#: Hranice živého subsetu (#749): backfill má ts_ingested o dny až roky později
LIVE_MAX_LAG = dt.timedelta(days=1)
#: Horní decil predikcí — stejná definice liftu jako experiment #740 fáze 1
TOP_SHARE = 0.10
#: Pod tímhle počtem vzorků je lift šum a řádek se nezapisuje
MIN_EVAL = 200
#: Pod tímhle počtem trénovacích vzorků se model netrénuje (studená DB, testy)
MIN_TRAIN = 2000
#: Kvantily P(velký pohyb) → importance 2/3; kalibrují se na trénovacích datech
IMPORTANCE_MID_Q = 0.60
IMPORTANCE_HIGH_Q = 0.90


@dataclass
class TrainedMagnitude:
    """Natrénovaná hlava + kalibrace prahů importance z trénovacích predikcí."""

    model: LogisticModel
    threshold_mid: float
    threshold_high: float
    n_train: int
    trained_at: dt.datetime

    def importance(self, p_big: float) -> int:
        if p_big >= self.threshold_high:
            return 3
        if p_big >= self.threshold_mid:
            return 2
        return 1


def _feature_row(
    title: str, source: str | None, ts_event: dt.datetime, body: str | None
) -> np.ndarray:
    return hash_row(features(title, source=source, hour_utc=ts_event.hour, body=body))


class NgramShadowJob:
    """Denní retrénink + průběžná stínová klasifikace + denní lift report."""

    def __init__(
        self,
        engine: Engine,
        *,
        train_symbol: str = "ES",
        window_min: int = 5,
        eval_symbols: tuple[str, ...] = ("ES", "NQ"),
        recent_days: int = 3,
    ) -> None:
        self._engine = engine
        self._train_symbol = train_symbol
        self._window = window_min
        self._eval_symbols = eval_symbols
        self._recent_days = recent_days
        self.trained: TrainedMagnitude | None = None
        self._last_train_day: dt.date | None = None
        self._last_eval_day: dt.date | None = None

    # ── 1) Trénink ─────────────────────────────────────────────────

    def _training_rows(self) -> list[tuple[str, str | None, dt.datetime, str | None, float]]:
        stmt = (
            select(
                news_events.c.title,
                news_events.c.source,
                news_events.c.ts_event,
                news_events.c.body,
                news_reactions.c.ret_bp,
            )
            .select_from(
                news_reactions.join(news_events, news_events.c.id == news_reactions.c.event_id)
            )
            .where(
                news_reactions.c.symbol == self._train_symbol,
                news_reactions.c.window_min == self._window,
                news_reactions.c.contaminated.is_(False),
                news_events.c.kind != "scheduled",
                news_events.c.title.is_not(None),
            )
        )
        with self._engine.connect() as conn:
            return [
                (row.title, row.source, row.ts_event, row.body, float(row.ret_bp))
                for row in conn.execute(stmt)
            ]

    def retrain(self, now: dt.datetime) -> bool:
        """Natrénuje magnitudovou hlavu nad celou historií; False = málo dat."""
        started = time.monotonic()
        samples = self._training_rows()
        if len(samples) < MIN_TRAIN:
            logger.info(
                "Ngram shadow: jen %d trénovacích vzorků (< %d) — netrénuji",
                len(samples),
                MIN_TRAIN,
            )
            return False
        rows = [_feature_row(title, source, ts, body) for title, source, ts, body, _ in samples]
        magnitudes = np.abs(np.array([ret for *_, ret in samples]))
        labels = (magnitudes > float(np.median(magnitudes))).astype(np.float64)
        model = LogisticModel().fit(rows, labels)
        train_probs = model.predict_proba(rows)
        self.trained = TrainedMagnitude(
            model=model,
            threshold_mid=float(np.quantile(train_probs, IMPORTANCE_MID_Q)),
            threshold_high=float(np.quantile(train_probs, IMPORTANCE_HIGH_Q)),
            n_train=len(samples),
            trained_at=now,
        )
        logger.info(
            "Ngram shadow: natrénováno na %d vzorcích za %.0f s (prahy importance %.3f/%.3f)",
            len(samples),
            time.monotonic() - started,
            self.trained.threshold_mid,
            self.trained.threshold_high,
        )
        return True

    # ── 2) Stínová klasifikace ─────────────────────────────────────

    def classify(self, now: dt.datetime, *, limit: int = 500) -> int:
        """Doplní `source='ngram'` verzi eventům, které už mají jinou klasifikaci.

        Podmínka „už má rule/llm verzi" je nutná: fronta pravidlového passu
        vyřazuje eventy s JAKOUKOLI klasifikací (#373), takže ngram verze
        zapsaná první by event připravila o kategorii a importance.
        """
        if self.trained is None:
            return 0
        has_other = select(news_classifications.c.event_id).where(
            news_classifications.c.source != NGRAM_SOURCE
        )
        has_ngram = select(news_classifications.c.event_id).where(
            news_classifications.c.source == NGRAM_SOURCE
        )
        stmt = (
            select(
                news_events.c.id,
                news_events.c.title,
                news_events.c.source,
                news_events.c.ts_event,
                news_events.c.body,
                news_events.c.category,
            )
            .where(
                news_events.c.id.in_(has_other),
                news_events.c.id.not_in(has_ngram),
                news_events.c.kind != "scheduled",
                news_events.c.title.is_not(None),
                news_events.c.ts_event >= now - dt.timedelta(days=self._recent_days),
            )
            .order_by(news_events.c.ts_event.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            pending = list(conn.execute(stmt).fetchall())
            if not pending:
                return 0
            versions = {
                int(event_id): int(version)
                for event_id, version in conn.execute(
                    select(
                        news_classifications.c.event_id,
                        func.max(news_classifications.c.version),
                    )
                    .where(news_classifications.c.event_id.in_([int(row.id) for row in pending]))
                    .group_by(news_classifications.c.event_id)
                )
            }
        probs = self.trained.model.predict_proba(
            [_feature_row(row.title, row.source, row.ts_event, row.body) for row in pending]
        )
        rows = [
            {
                "event_id": int(row.id),
                "version": versions.get(int(row.id), 0) + 1,
                "source": NGRAM_SOURCE,
                "category": row.category,
                "importance": self.trained.importance(float(p)),
                # Směr neprošel měřením (#749) — hlava ho nepredikuje.
                # `strength` nese P(velký pohyb): z ní se zpětně počítá lift.
                "direction": 0,
                "strength": float(p),
                "created_at": now,
            }
            for row, p in zip(pending, probs, strict=True)
        ]
        with self._engine.begin() as conn:
            conn.execute(insert(news_classifications), rows)
        logger.info("Ngram shadow: %d eventů klasifikováno", len(rows))
        return len(rows)

    # ── 3) Lift na živém subsetu ───────────────────────────────────

    @staticmethod
    def _lift(magnitudes: np.ndarray, scores: np.ndarray) -> tuple[float, float, float]:
        """(lift, průměr horního decilu, celkový průměr) podle daného skóre."""
        top = max(1, int(len(magnitudes) * TOP_SHARE))
        order = np.argsort(-scores, kind="stable")
        top_mean = float(magnitudes[order[:top]].mean())
        overall = float(magnitudes.mean())
        return (top_mean / overall if overall > 0 else 0.0, top_mean, overall)

    def evaluate(self, now: dt.datetime) -> int:
        """Přepočte lift per (symbol, subset) do `news_ngram_shadow`.

        Baseline = týž lift s řazením podle průměrné |reakce| kategorie na
        témže subsetu (metodika #749). Model musí porazit baseline na živém
        subsetu, jinak se hlava nezapne.

        Subset `live` počítá JEN PROSPEKTIVNÍ klasifikace — vzniklé dřív,
        než se reakce vůbec změřila (created_at ≤ ts_event + okno). Dohnaná
        klasifikace starších eventů je postdikce: jejich reakce už mohla být
        v tréninku a lift by nafoukl leak (první běh po nasazení: 1,905×
        z 3denního backlogu vs. poctivá brána). `all`/`backfill` zůstávají
        in-sample referencí a s #749 se srovnávají jen orientačně.
        """
        stmt = (
            select(
                news_classifications.c.strength,
                news_classifications.c.created_at,
                news_events.c.category,
                news_events.c.ts_event,
                news_events.c.ts_ingested,
                news_reactions.c.symbol,
                news_reactions.c.ret_bp,
            )
            .select_from(
                news_classifications.join(
                    news_events, news_events.c.id == news_classifications.c.event_id
                ).join(news_reactions, news_reactions.c.event_id == news_events.c.id)
            )
            .where(
                news_classifications.c.source == NGRAM_SOURCE,
                news_classifications.c.strength.is_not(None),
                news_reactions.c.window_min == self._window,
                news_reactions.c.contaminated.is_(False),
                news_reactions.c.symbol.in_(self._eval_symbols),
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        written = 0
        n_train = self.trained.n_train if self.trained else 0
        results: list[dict[str, object]] = []
        for symbol in self._eval_symbols:
            per_symbol = [row for row in rows if row.symbol == symbol]
            subsets: dict[str, list[Any]] = {"all": per_symbol, "live": [], "backfill": []}
            window_delta = dt.timedelta(minutes=self._window)
            for row in per_symbol:
                lag = row.ts_ingested - row.ts_event
                prospective = row.created_at <= row.ts_event + window_delta
                if lag <= LIVE_MAX_LAG and prospective:
                    subsets["live"].append(row)
                elif lag > LIVE_MAX_LAG:
                    subsets["backfill"].append(row)
            for subset, items in subsets.items():
                if len(items) < MIN_EVAL:
                    continue
                magnitudes = np.abs(np.array([float(row.ret_bp) for row in items]))
                scores = np.array([float(row.strength) for row in items])
                grouped: dict[str | None, list[float]] = {}
                for row, magnitude in zip(items, magnitudes, strict=True):
                    grouped.setdefault(row.category, []).append(float(magnitude))
                cat_mean = {cat: float(np.mean(vals)) for cat, vals in grouped.items()}
                baseline_scores = np.array([cat_mean[row.category] for row in items])
                lift, top_mean, overall = self._lift(magnitudes, scores)
                baseline_lift, _, _ = self._lift(magnitudes, baseline_scores)
                results.append(
                    {
                        "symbol": symbol,
                        "window_min": self._window,
                        "subset": subset,
                        "n": len(items),
                        "lift": lift,
                        "baseline_lift": baseline_lift,
                        "top_decile_mean_bp": top_mean,
                        "mean_bp": overall,
                        "model_n_train": n_train,
                        "computed_at": now,
                    }
                )
                logger.info(
                    "Ngram shadow %s/%s: n=%d, lift %.3f× (baseline %.3f×)",
                    symbol,
                    subset,
                    len(items),
                    lift,
                    baseline_lift,
                )
                written += 1
        # Full-replace VŠECH řádků vyhodnocovaných symbolů, ne jen počítaných
        # klíčů: subset, který spadl pod MIN_EVAL, by jinak v tabulce nechal
        # stale hodnotu z minulého běhu (stalo se s leaknutým live po #867)
        with self._engine.begin() as conn:
            conn.execute(
                delete(news_ngram_shadow).where(
                    news_ngram_shadow.c.symbol.in_(self._eval_symbols),
                    news_ngram_shadow.c.window_min == self._window,
                )
            )
            if results:
                conn.execute(insert(news_ngram_shadow), results)
        return written

    # ── Orchestrace ────────────────────────────────────────────────

    def run(self, now: dt.datetime) -> int:
        """Jeden cyklus: denní retrénink, průběžná klasifikace, denní lift."""
        if self._last_train_day != now.date():
            # Neúspěch (málo dat) se nezkouší celý den znovu — dat přibývá pomalu
            self._last_train_day = now.date()
            self.retrain(now)
        classified = self.classify(now)
        if self._last_eval_day != now.date():
            self._last_eval_day = now.date()
            self.evaluate(now)
        return classified
