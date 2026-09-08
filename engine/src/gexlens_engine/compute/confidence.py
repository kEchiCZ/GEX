"""Kalibrovaná confidence setupů z track recordu (#794 fáze 2B, ADR-0033).

Do fáze 2B byla `confidence` konstanta šablony (45–60, ADR-0004) — nikdy
neměřená proti výsledkům. Tady se základ confidence počítá jako **Wilsonova
dolní mez 95 % intervalu úspěšnosti** (podíl `closed_target`, stejná definice
jako `setupstats.Bucket.hit_rate_lb`) nad uzavřenými setupy **aktuální
mechaniky**, v koších od nejkonkrétnějšího k nejobecnějšímu:

1. symbol × šablona × gamma režim,
2. šablona × gamma režim (všechny symboly),
3. symbol × šablona,
4. šablona.

Použije se první koš s alespoň `min_samples` uzavřenými setupy; když žádný
nestačí, zůstává konstanta šablony (`source = "constant"`). Dolní mez místo
bodové úspěšnosti proto, že při n = 30 je σ ≈ 9 p. b. — bodový odhad by
lhal stejně jako u gate SentimentLensu (SPEC 6.2).

Posun podle polohy v pásmu (#1060) se aplikuje **navrch** tohoto základu;
`context.confidence_base` nese kalibrovaný základ, `confidence_template`
původní konstantu a `confidence_source` koš, ze kterého základ vznikl.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from gexlens_engine.compute.setupstats import wilson_lower_bound

#: Výchozí minimum uzavřených setupů v koši (rozhodnutí uživatele 9. 9. 2026)
DEFAULT_MIN_SAMPLES = 30

BucketKey = tuple[str | None, str, str | None]


@dataclass(frozen=True)
class CalibrationRow:
    """Jeden uzavřený setup pro kalibraci — jen to, co koš potřebuje."""

    symbol: str
    template: str
    gex_regime: str | None
    win: bool


@dataclass(frozen=True)
class CalibrationBucket:
    n: int
    wins: int

    @property
    def lower_bound(self) -> float:
        return wilson_lower_bound(self.wins, self.n)


@dataclass(frozen=True)
class ConfidenceTable:
    """Předpočítané koše; `confidence()` je čistý lookup bez I/O."""

    buckets: dict[BucketKey, CalibrationBucket] = field(default_factory=dict)
    min_samples: int = DEFAULT_MIN_SAMPLES

    def confidence(
        self, symbol: str, template: str, gex_regime: str | None, fallback: int
    ) -> tuple[int, str]:
        """(confidence 0–100, zdroj). Zdroj = „wilson <koš> n=<n>" nebo „constant"."""
        candidates: tuple[tuple[BucketKey, str], ...] = (
            ((symbol, template, gex_regime), f"{symbol}·{template}·{gex_regime or 'bez režimu'}"),
            ((None, template, gex_regime), f"{template}·{gex_regime or 'bez režimu'}"),
            ((symbol, template, None), f"{symbol}·{template}"),
            ((None, template, None), template),
        )
        for key, label in candidates:
            bucket = self.buckets.get(key)
            if bucket is None or bucket.n < self.min_samples:
                continue
            value = round(100.0 * bucket.lower_bound)
            return max(0, min(100, value)), f"wilson {label} n={bucket.n}"
        return fallback, "constant"


def build_confidence_table(
    rows: Iterable[CalibrationRow], *, min_samples: int = DEFAULT_MIN_SAMPLES
) -> ConfidenceTable:
    """Agreguje uzavřené setupy do všech čtyř úrovní košů naráz."""
    counts: dict[BucketKey, list[int]] = {}
    for row in rows:
        keys: tuple[BucketKey, ...] = (
            (row.symbol, row.template, row.gex_regime),
            (None, row.template, row.gex_regime),
            (row.symbol, row.template, None),
            (None, row.template, None),
        )
        for key in keys:
            entry = counts.setdefault(key, [0, 0])
            entry[0] += 1
            entry[1] += 1 if row.win else 0
    return ConfidenceTable(
        buckets={key: CalibrationBucket(n=n, wins=wins) for key, (n, wins) in counts.items()},
        min_samples=min_samples,
    )
