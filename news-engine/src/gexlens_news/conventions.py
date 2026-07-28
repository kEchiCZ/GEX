"""Znaménkové konvence plánovaných řad (#280, SPEC kap. 4).

Scheduled eventy klasifikaci nepotřebují: kategorie a důležitost jsou
z kalendáře, směr plyne z `surprise_z` a konvence dané řady (vyšší CPI =
risk-off). Konvence jsou ale **default v konfiguraci, ne dogma** — jsou
režimově závislé. V období „good news is bad news" znamená slabý NFP risk-on
(naděje na snížení sazeb), takže fixní znaménko se může celé měsíce tiše mýlit.

Proto se konvence průběžně ověřují proti realizovaným reakcím: když řada
v klouzavém okně trefuje hůř než `MIN_HIT_RATE`, jde do review fronty.
Systém tedy nepředstírá, že zná pravdu — kontroluje si ji.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from gexlens_engine.compute.setupstats import wilson_lower_bound

# Konvence: +1 = vyšší hodnota než konsensus je pro riziková aktiva pozitivní,
# −1 = negativní. Pořadí rozhoduje (specifičtější vzory dřív).
SERIES_CONVENTIONS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    # Inflace nahoru = tlak na vyšší sazby = risk-off
    ("inflace", re.compile(r"\bcpi\b|\bppi\b|\bpce\b|price index|inflation", re.I), -1),
    # Sazba nahoru = risk-off
    ("sazby", re.compile(r"federal funds rate|rate decision|interest rate", re.I), -1),
    # Nezaměstnanost a nové žádosti nahoru = slabší ekonomika = risk-off
    ("nezaměstnanost", re.compile(r"unemployment rate|jobless claims", re.I), -1),
    # Zaměstnanost a růst nahoru = risk-on
    ("zaměstnanost", re.compile(r"non-?farm|payroll|employment change", re.I), 1),
    ("růst", re.compile(r"\bgdp\b|retail sales|\bpmi\b|\bism\b|durable goods", re.I), 1),
)

# Práh, pod kterým se konvence považuje za podezřelou (SPEC kap. 4: 45 %)
MIN_HIT_RATE = 0.45
# Minimum měření, aby verdikt nepadl z pár eventů
MIN_SAMPLES = 10


@dataclass(frozen=True)
class SeriesConvention:
    name: str
    sign: int


@dataclass(frozen=True)
class ConventionOutcome:
    """Jedno ověření: predikovaný směr vs. realizovaná reakce."""

    series: str
    predicted_dir: int
    realized_ret_bp: float

    @property
    def correct(self) -> bool:
        if self.predicted_dir == 0 or self.realized_ret_bp == 0:
            return False
        return (self.realized_ret_bp > 0) == (self.predicted_dir > 0)


@dataclass(frozen=True)
class ConventionCheck:
    """Výsledek ověření jedné řady."""

    series: str
    n: int
    hits: int
    hit_rate: float
    hit_rate_lb: float

    @property
    def suspicious(self) -> bool:
        """Řada trefuje hůř, než by čekáním na minci vyšlo — konvence je podezřelá.

        Hodnotí se bodová hit-rate proti prahu, ale reportuje se i Wilsonova
        dolní mez, aby šlo poznat, jestli je verdikt podložený nebo z malého n.
        """
        return self.n >= MIN_SAMPLES and self.hit_rate < MIN_HIT_RATE


def match_series(title: str) -> SeriesConvention | None:
    """Konvence pro název kalendářního eventu; None = řada není v mapě."""
    for name, pattern, sign in SERIES_CONVENTIONS:
        if pattern.search(title):
            return SeriesConvention(name=name, sign=sign)
    return None


def scheduled_direction(title: str, surprise_z: float | None) -> int | None:
    """Směr scheduled eventu ze znaménka překvapení a konvence řady.

    None = nelze určit (neznámá řada nebo chybí překvapení). Nula je legitimní
    výsledek: překvapení přesně na konsensu nedává směr.
    """
    if surprise_z is None:
        return None
    convention = match_series(title)
    if convention is None:
        return None
    if surprise_z == 0:
        return 0
    return convention.sign if surprise_z > 0 else -convention.sign


def check_conventions(outcomes: Sequence[ConventionOutcome]) -> list[ConventionCheck]:
    """Úspěšnost konvencí per řada, nejhorší první."""
    grouped: dict[str, list[ConventionOutcome]] = {}
    for outcome in outcomes:
        if outcome.predicted_dir == 0:
            continue  # bez směru není co ověřovat
        grouped.setdefault(outcome.series, []).append(outcome)

    checks = []
    for series, items in grouped.items():
        hits = sum(1 for item in items if item.correct)
        checks.append(
            ConventionCheck(
                series=series,
                n=len(items),
                hits=hits,
                hit_rate=hits / len(items),
                hit_rate_lb=wilson_lower_bound(hits, len(items)),
            )
        )
    return sorted(checks, key=lambda check: check.hit_rate)
