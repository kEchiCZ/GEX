"""Sebekontrola setup detektoru (#309): agregace výsledků a verdikt zhoršení.

Čisté funkce bez I/O — načtení uzavřených setupů a publikaci alertu dělá
`InstrumentPipeline`. Vzniklo z 27. 7., kdy detektor týden ztrácel (−43,5R za
166 uzavřených) a přišlo se na to jen náhodným dotazem do PG: stránka Setupy
čísla ukazovala, ale nic nekřičelo.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ClosedSetup:
    """Uzavřený setup pro statistiku (podmnožina `setups`)."""

    template: str
    direction: str
    status: str
    outcome_r: float


@dataclass(frozen=True)
class SetupParamsStats:
    """Prahy sebekontroly (#309)."""

    window_days: int = 7
    # Jeden špatný obchod verdikt nedělá — pod minimem se nehodnotí
    min_samples: int = 10
    # Propad ΣR, od kterého se hlásí zhoršení (záporné číslo)
    max_drawdown_r: float = -10.0


@dataclass(frozen=True)
class Bucket:
    """Statistika jedné skupiny (celek, šablona nebo šablona × směr)."""

    label: str
    n: int
    wins: int
    sum_r: float

    @property
    def avg_r(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    @property
    def hit_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def hit_rate_lb(self) -> float:
        """Wilson dolní mez 95% intervalu úspěšnosti.

        Bodová úspěšnost je při malém n nerozlišitelná od mince (σ ≈ 11 p.b.
        při n=20), takže reportovat samotné procento by lhalo. Stejný důvod
        jako u gate SentimentLensu (SPEC 6.2).
        """
        return wilson_lower_bound(self.wins, self.n)


@dataclass(frozen=True)
class SetupReport:
    """Výsledek sebekontroly za okno."""

    overall: Bucket
    per_template: list[Bucket] = field(default_factory=list)
    per_template_direction: list[Bucket] = field(default_factory=list)

    @property
    def worst_template(self) -> Bucket | None:
        return min(self.per_template, key=lambda b: b.sum_r, default=None)

    @property
    def worst_direction(self) -> Bucket | None:
        return min(self.per_template_direction, key=lambda b: b.sum_r, default=None)


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Dolní mez Wilsonova intervalu spolehlivosti pro podíl; n=0 → 0."""
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _bucket(label: str, rows: Sequence[ClosedSetup]) -> Bucket:
    return Bucket(
        label=label,
        n=len(rows),
        wins=sum(1 for row in rows if row.status == "closed_target"),
        sum_r=sum(row.outcome_r for row in rows),
    )


def aggregate(rows: Sequence[ClosedSetup]) -> SetupReport:
    """Rozpad uzavřených setupů na celek, šablony a šablona × směr."""
    by_template: dict[str, list[ClosedSetup]] = {}
    by_pair: dict[tuple[str, str], list[ClosedSetup]] = {}
    for row in rows:
        by_template.setdefault(row.template, []).append(row)
        by_pair.setdefault((row.template, row.direction), []).append(row)
    return SetupReport(
        overall=_bucket("celkem", rows),
        per_template=sorted(
            (_bucket(name, items) for name, items in by_template.items()),
            key=lambda b: b.sum_r,
        ),
        per_template_direction=sorted(
            (_bucket(f"{name} {side}", items) for (name, side), items in by_pair.items()),
            key=lambda b: b.sum_r,
        ),
    )


def degraded(report: SetupReport, params: SetupParamsStats) -> bool:
    """True = detektor za okno prodělává natolik, že to má uživatel vědět.

    Vědomě se neptáme, jestli je propad statisticky odlišitelný od nuly —
    −10 R je −10 R bez ohledu na p-hodnotu. Minimum vzorků brání jen tomu,
    aby verdikt padl z jednoho dvou obchodů.
    """
    if report.overall.n < params.min_samples:
        return False
    return report.overall.sum_r <= params.max_drawdown_r


def format_report(report: SetupReport, window_days: int) -> str:
    """Jednořádkový souhrn do logu i alertu."""
    o = report.overall
    parts = [
        f"{window_days} d: {o.n} uzavřených, {o.wins} výher "
        f"({o.hit_rate:.0%}, Wilson LB {o.hit_rate_lb:.0%}), "
        f"ΣR {o.sum_r:+.1f}, Ø R {o.avg_r:+.2f}"
    ]
    worst = report.worst_direction
    if worst is not None and worst.sum_r < 0:
        parts.append(f"nejhorší {worst.label}: {worst.n}× ΣR {worst.sum_r:+.1f}")
    return "; ".join(parts)
