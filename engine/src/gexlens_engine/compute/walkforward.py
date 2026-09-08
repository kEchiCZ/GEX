"""Walk-forward vyhodnocení kandidátních parametrů setupů (#794 fáze 3, ADR-0034).

Vstup: pro každého kandidáta (pojmenovaná sada `SetupParams`) denní řada ΣR
per obchodní seance z offline replaye (`scripts/backtest_setups.py` —
produkční detektor nad archivem, parita ověřená ve fázi 1). Tady se nad
řadami dělá jen statistika, žádné I/O:

- **Walk-forward**: okno `in_sample_days` seancí vybere kandidáta s nejlepší
  in-sample metrikou, ten se hodnotí na následujících `out_sample_days`
  seancích (out-of-sample), okno se posune. Out-of-sample řada „strategie"
  je slepenec voleb jednotlivých foldů — to je jediný poctivý odhad toho,
  co by ladění reálně dodalo; in-sample čísla se nereportují jako výkon.
- **Baseline** (platná verze parametrů) jede stejnými OOS dny; při shodě
  metriky vyhrává baseline (churn parametrů je náklad, ADR-0034).
- **Ochrana proti overfittingu** (analýza #794, sekce 2d): prostor kandidátů
  je malý a předem deklarovaný (`configs/walkforward_grid.json`), test
  rozdílu denních OOS výnosů strategie − baseline se hodnotí na Bonferroniho
  hladině `alpha / počet alternativ`, a návrh vzniká jen při minimálním počtu
  OOS seancí, minimálním zlepšení na den a stabilní volbě (kandidát vyhrál
  aspoň polovinu foldů).
- **Výstup je návrh, ne zápis** (autonomie stupeň 1): kandidát a důvod;
  verzi do parameter store zakládá člověk přes `POST /setups/params`.

Sharpe: denní ΣR, výběrová směrodatná odchylka, anualizace √252 (ADR-0030).
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DailySeries = Mapping[dt.date, float]

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class WalkForwardParams:
    """Protokol walk-forwardu — hodnoty z ADR-0034, měnit jen s ADR."""

    in_sample_days: int = 20
    out_sample_days: int = 5
    #: "sharpe" (ADR-0030, primární) nebo "sum_r" (robustní při málo dnech)
    metric: str = "sharpe"
    #: Bez tolika OOS seancí se nenavrhuje nic — SE Sharpe ≈ √252/√N
    min_oos_days: int = 20
    #: Hladina testu před Bonferroniho korekcí na počet alternativ
    alpha: float = 0.05
    #: Zlepšení Ø denní ΣR, pod které se parametry nemění (churn je náklad)
    min_improvement_r_per_day: float = 0.1
    #: Kandidát musí vyhrát aspoň tento podíl foldů, jinak je volba nestabilní
    min_chosen_share: float = 0.5


#: Protokol ADR-0034 — sdílený singleton (ruff B008: žádné volání v defaultu)
DEFAULT_WALK_FORWARD_PARAMS = WalkForwardParams()


@dataclass(frozen=True)
class Fold:
    in_start: dt.date
    in_end: dt.date
    out_start: dt.date
    out_end: dt.date
    chosen: str
    in_metric: float | None
    out_sum_r: float
    baseline_out_sum_r: float


@dataclass(frozen=True)
class WalkForwardResult:
    baseline: str
    candidates: tuple[str, ...]
    folds: tuple[Fold, ...]
    #: OOS řada strategie (slepenec voleb) a baseline na týchž dnech
    oos: dict[dt.date, float] = field(default_factory=dict)
    oos_baseline: dict[dt.date, float] = field(default_factory=dict)
    sharpe_oos: float | None = None
    sharpe_baseline: float | None = None
    mean_diff_per_day: float = 0.0
    t_stat: float | None = None
    p_value: float | None = None
    alpha_bonferroni: float = 0.05
    #: Podíl foldů, ve kterých byl kandidát zvolen
    chosen_share: dict[str, float] = field(default_factory=dict)
    #: Kandidát k návrhu (None = žádná změna) a slovní verdikt
    proposal: str | None = None
    verdict: str = ""

    @property
    def oos_days(self) -> int:
        return len(self.oos)


def annualized_sharpe(values: Sequence[float]) -> float | None:
    """Anualizovaný Sharpe denních ΣR (ADR-0030); None pod 2 dny nebo při nulové σ."""
    if len(values) < 2:
        return None
    sigma = statistics.stdev(values)
    if sigma == 0.0:
        return None
    return statistics.fmean(values) / sigma * math.sqrt(TRADING_DAYS_PER_YEAR)


def metric_value(values: Sequence[float], metric: str) -> float | None:
    """Hodnota in-sample metriky; Sharpe bez rozptylu padá na Σ R (řád zůstává)."""
    if not values:
        return None
    if metric == "sum_r":
        return float(sum(values))
    if metric == "sharpe":
        sharpe = annualized_sharpe(values)
        return sharpe if sharpe is not None else float(sum(values))
    raise ValueError(f"Neznámá metrika {metric!r} (povolené: sharpe, sum_r)")


def _normal_two_sided_p(t_stat: float) -> float:
    """Dvoustranná p-hodnota z normální aproximace (n ≥ 20, bez závislosti na scipy)."""
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def paired_test(diffs: Sequence[float]) -> tuple[float | None, float | None]:
    """t-statistika a p-hodnota párového rozdílu denních výnosů (strategie − baseline).

    Nulový rozptyl s nenulovým průměrem = jistý rozdíl (t = ±∞, p = 0);
    nulový rozptyl i průměr = žádný rozdíl (t = 0, p = 1).
    """
    if len(diffs) < 2:
        return None, None
    mean = statistics.fmean(diffs)
    sigma = statistics.stdev(diffs)
    if sigma == 0.0:
        if mean == 0.0:
            return 0.0, 1.0
        return (math.inf if mean > 0 else -math.inf), 0.0
    t_stat = mean / (sigma / math.sqrt(len(diffs)))
    return t_stat, _normal_two_sided_p(t_stat)


def walk_forward(
    series: Mapping[str, DailySeries],
    *,
    baseline: str,
    params: WalkForwardParams | None = None,
) -> WalkForwardResult:
    """Walk-forward nad denními řadami kandidátů; `baseline` musí být mezi nimi.

    Dny = sjednocení dnů všech kandidátů; kandidát bez záznamu pro den má ΣR 0
    (detektor ten den nic neotevřel). Fold potřebuje plné in-sample okno a
    aspoň jeden OOS den; poslední neúplný OOS blok se použije, jak je.
    `params` None = protokol ADR-0034 (`DEFAULT_WALK_FORWARD_PARAMS`).
    """
    params = params if params is not None else DEFAULT_WALK_FORWARD_PARAMS
    if baseline not in series:
        raise ValueError(f"Baseline {baseline!r} není mezi kandidáty")
    if params.in_sample_days < 2 or params.out_sample_days < 1:
        raise ValueError("in_sample_days ≥ 2 a out_sample_days ≥ 1")
    days: list[dt.date] = sorted({day for values in series.values() for day in values})
    names = tuple(sorted(series, key=lambda name: (name != baseline, name)))

    def value(name: str, day: dt.date) -> float:
        return float(series[name].get(day, 0.0))

    folds: list[Fold] = []
    oos: dict[dt.date, float] = {}
    oos_baseline: dict[dt.date, float] = {}
    start = params.in_sample_days
    while start < len(days):
        in_days = days[start - params.in_sample_days : start]
        out_days = days[start : start + params.out_sample_days]
        best_name = baseline
        best_metric = metric_value([value(baseline, day) for day in in_days], params.metric)
        for name in names:
            if name == baseline:
                continue
            candidate_metric = metric_value([value(name, day) for day in in_days], params.metric)
            # Ostře lepší — při shodě zůstává baseline (churn je náklad)
            if candidate_metric is not None and (
                best_metric is None or candidate_metric > best_metric
            ):
                best_name, best_metric = name, candidate_metric
        out_sum = sum(value(best_name, day) for day in out_days)
        base_sum = sum(value(baseline, day) for day in out_days)
        for day in out_days:
            oos[day] = value(best_name, day)
            oos_baseline[day] = value(baseline, day)
        folds.append(
            Fold(
                in_start=in_days[0],
                in_end=in_days[-1],
                out_start=out_days[0],
                out_end=out_days[-1],
                chosen=best_name,
                in_metric=best_metric,
                out_sum_r=out_sum,
                baseline_out_sum_r=base_sum,
            )
        )
        start += params.out_sample_days

    ordered_days = sorted(oos)
    strategy_values = [oos[day] for day in ordered_days]
    baseline_values = [oos_baseline[day] for day in ordered_days]
    diffs = [s - b for s, b in zip(strategy_values, baseline_values, strict=True)]
    t_stat, p_value = paired_test(diffs)
    mean_diff = statistics.fmean(diffs) if diffs else 0.0
    alternatives = max(1, len(names) - 1)
    alpha_bonferroni = params.alpha / alternatives

    chosen_share = {
        name: (sum(1 for fold in folds if fold.chosen == name) / len(folds) if folds else 0.0)
        for name in names
    }
    proposal, verdict = _decide(
        folds=folds,
        baseline=baseline,
        oos_days=len(ordered_days),
        mean_diff=mean_diff,
        p_value=p_value,
        alpha_bonferroni=alpha_bonferroni,
        chosen_share=chosen_share,
        params=params,
    )
    return WalkForwardResult(
        baseline=baseline,
        candidates=names,
        folds=tuple(folds),
        oos=oos,
        oos_baseline=oos_baseline,
        sharpe_oos=annualized_sharpe(strategy_values),
        sharpe_baseline=annualized_sharpe(baseline_values),
        mean_diff_per_day=mean_diff,
        t_stat=t_stat,
        p_value=p_value,
        alpha_bonferroni=alpha_bonferroni,
        chosen_share=chosen_share,
        proposal=proposal,
        verdict=verdict,
    )


def _decide(
    *,
    folds: Sequence[Fold],
    baseline: str,
    oos_days: int,
    mean_diff: float,
    p_value: float | None,
    alpha_bonferroni: float,
    chosen_share: Mapping[str, float],
    params: WalkForwardParams,
) -> tuple[str | None, str]:
    """Návrh jen při splnění VŠECH zábran z ADR-0034; jinak slovně proč ne."""
    if not folds:
        return None, "málo seancí pro jediný fold — nic se nenavrhuje"
    if oos_days < params.min_oos_days:
        return None, f"jen {oos_days} OOS seancí (< {params.min_oos_days}) — nic se nenavrhuje"
    leader = max(
        (name for name in chosen_share if name != baseline),
        key=lambda name: (chosen_share[name], name),
        default=None,
    )
    if leader is None or chosen_share[leader] < params.min_chosen_share:
        share = chosen_share[leader] if leader is not None else 0.0
        return None, (
            f"žádný kandidát nevyhrál aspoň {params.min_chosen_share:.0%} foldů "
            f"(nejlepší {leader or '—'} {share:.0%}) — volba nestabilní, nic se nenavrhuje"
        )
    if mean_diff < params.min_improvement_r_per_day:
        return None, (
            f"zlepšení Ø {mean_diff:+.3f} R/den je pod prahem "
            f"{params.min_improvement_r_per_day:.2f} — nic se nenavrhuje"
        )
    if p_value is None or p_value > alpha_bonferroni:
        shown = "—" if p_value is None else f"{p_value:.3f}"
        return None, (
            f"rozdíl OOS není významný (p = {shown} > Bonferroni {alpha_bonferroni:.4f}) "
            "— nic se nenavrhuje"
        )
    return leader, (
        f"NÁVRH: {leader} — vyhrál {chosen_share[leader]:.0%} foldů, OOS zlepšení "
        f"Ø {mean_diff:+.3f} R/den, p = {p_value:.4f} ≤ {alpha_bonferroni:.4f}; "
        "zápis do parameter store až po schválení člověkem"
    )


def render_markdown(results: Mapping[str, WalkForwardResult], *, title: str) -> str:
    """Report pro člověka: souhrn per blok (symbol / portfolio) a folds."""

    def fmt(value: float | None, digits: int = 2) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    lines = [f"# {title}", ""]
    lines.append(
        "| Blok | Baseline | OOS dnů | Foldů | Sharpe OOS strategie | Sharpe OOS baseline | "
        "Ø Δ R/den | t | p | Bonferroni α | Verdikt |"
    )
    lines.append("|" + "---|" * 11)
    for block, result in results.items():
        lines.append(
            "| "
            + " | ".join(
                [
                    block,
                    result.baseline,
                    str(result.oos_days),
                    str(len(result.folds)),
                    fmt(result.sharpe_oos),
                    fmt(result.sharpe_baseline),
                    f"{result.mean_diff_per_day:+.3f}",
                    fmt(result.t_stat),
                    fmt(result.p_value, 4),
                    f"{result.alpha_bonferroni:.4f}",
                    result.verdict,
                ]
            )
            + " |"
        )
    for block, result in results.items():
        lines.append("")
        lines.append(f"## {block} — folds")
        lines.append(
            "| In-sample | Out-of-sample | Zvolen | IS metrika | OOS Σ R | Baseline OOS Σ R |"
        )
        lines.append("|---|---|---|---|---|---|")
        for fold in result.folds:
            lines.append(
                f"| {fold.in_start} → {fold.in_end} | {fold.out_start} → {fold.out_end} | "
                f"{fold.chosen} | {fmt(fold.in_metric)} | {fold.out_sum_r:+.2f} | "
                f"{fold.baseline_out_sum_r:+.2f} |"
            )
        shares = ", ".join(
            f"{name} {share:.0%}" for name, share in sorted(result.chosen_share.items())
        )
        lines.append("")
        lines.append(f"Podíl foldů: {shares}")
    return "\n".join(lines) + "\n"
