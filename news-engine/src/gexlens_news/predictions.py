"""Predikce, jejich vyhodnocení a váhy predictorů (#282, SPEC 2.5 a 5.3).

Čisté funkce; zápis dělá `prediction_job`.

Predikce jsou **immutable** (S11): nesou verzi klasifikace, ze které vznikly,
takže zpětná reklasifikace nikdy nemění minulé odhady. Vyhodnocení je
**per okno** — predikce „nahoru" může po minutě sedět a po hodině ne, takže
jediný příznak `correct` by neřekl, vůči čemu platí.

Váha predictoru se odvozuje z **Wilsonovy dolní meze**, ne z bodové úspěšnosti:
55 % z dvaceti pokusů je nerozlišitelné od mince a nemá dostat žádnou váhu.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from gexlens_engine.compute.setupstats import wilson_lower_bound

# Primární okno pro hit-rate a váhy. +5 min podle preference uživatele —
# nejrychlejší čitelná reakce; per kategorie přepnutelné konfigurací.
DEFAULT_PRIMARY_WINDOW_MIN = 5
# Klouzavé okno kalibrace (SPEC 5.3)
DEFAULT_ROLLING_DAYS = 90
# Minimum vyhodnocení, aby váha nevznikla z hrstky případů
MIN_SAMPLES_FOR_WEIGHT = 20


@dataclass(frozen=True)
class Outcome:
    """Jedno vyhodnocené okno predikce."""

    category: str
    predictor: str
    window_min: int
    predicted_dir: int
    realized_ret_bp: float

    @property
    def realized_dir(self) -> int:
        if self.realized_ret_bp > 0:
            return 1
        if self.realized_ret_bp < 0:
            return -1
        return 0

    @property
    def correct(self) -> bool:
        """Nulový směr (na kterékoli straně) se nepočítá jako trefa."""
        return self.predicted_dir != 0 and self.predicted_dir == self.realized_dir


@dataclass(frozen=True)
class PredictorWeight:
    category: str
    predictor: str
    window_min: int
    n: int
    hit_rate: float
    hit_rate_lb: float
    weight: float


def weight_from_hit_rate(hit_rate_lb: float) -> float:
    """Váha z dolní meze úspěšnosti: 0 na úrovni mince, 1 při jistotě.

    `2·LB − 1` škáluje edge nad 50 % do intervalu 0–1 a **záporné hodnoty
    ořezává na nulu**: predictor, který trefuje hůř než mince, nemá dostat
    zápornou váhu a otáčet znaménko — to už by nebyla kalibrace, ale
    přefitování na historii.
    """
    return max(0.0, min(1.0, 2.0 * hit_rate_lb - 1.0))


def compute_weights(
    outcomes: Sequence[Outcome],
    *,
    primary_window_min: int = DEFAULT_PRIMARY_WINDOW_MIN,
    min_samples: int = MIN_SAMPLES_FOR_WEIGHT,
) -> list[PredictorWeight]:
    """Váhy per (kategorie, predictor) z primárního okna.

    Buckety pod `min_samples` se vynechávají úplně — chybějící váha znamená
    „zatím nevíme" a volající použije neutrální 1.0, což je poctivější než
    váha spočtená z pěti případů.
    """
    grouped: dict[tuple[str, str], list[Outcome]] = {}
    for outcome in outcomes:
        if outcome.window_min != primary_window_min or outcome.predicted_dir == 0:
            continue
        grouped.setdefault((outcome.category, outcome.predictor), []).append(outcome)

    weights: list[PredictorWeight] = []
    for (category, predictor), items in grouped.items():
        if len(items) < min_samples:
            continue
        hits = sum(1 for item in items if item.correct)
        lb = wilson_lower_bound(hits, len(items))
        weights.append(
            PredictorWeight(
                category=category,
                predictor=predictor,
                window_min=primary_window_min,
                n=len(items),
                hit_rate=hits / len(items),
                hit_rate_lb=lb,
                weight=weight_from_hit_rate(lb),
            )
        )
    return sorted(weights, key=lambda w: (w.category, w.predictor))
