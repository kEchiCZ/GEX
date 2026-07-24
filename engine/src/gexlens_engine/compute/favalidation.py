"""Denní validace flow-adjusted OI (#232, ADR-0011): open-ratio a rank korelace.

Porovnává klasifikovaný denní volume řetězce (řez 21:00 UTC — konec trade date,
volume counter IBKR se resetuje ve 22:00 UTC) s ranním ΔOI z CME archivu.
Každý den tak přibude jeden kalibrační bod pro α (dnes 0.4, ADR-0011) bez
ručního spouštění skriptů. Tady je jen čistý výpočet nad mapami
(strike, right) → hodnota; sběr dat a uložení dělá `storage.fa_validation`.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Key = tuple[float, str]

# Minimální vzorek pro smysluplný bod — shodné prahy jako ruční validace #232
MIN_TRADED_CONTRACTS = 10
MIN_VOLUME_SUM = 100.0


@dataclass(frozen=True)
class FaValidationPoint:
    """Jeden denní kalibrační bod (jedna expirace, jeden trade date)."""

    contracts: int  # počet stran (strike, right) v porovnání
    volume_sum: float  # Σ volume obchodovaných stran
    doi_abs_sum: float  # Σ|ΔOI|
    doi_net_sum: float  # ΣΔOI (čistá změna positioningu)
    open_ratio: float  # Σ|ΔOI| / Σ volume — kolik volume „přežije" do OI (≈ α)
    spearman: float  # rank korelace volume vs. |ΔOI| — predikuje volume MÍSTO změny?
    silent_share: float  # podíl |ΔOI| na stranách bez zachyceného volume


def _ranks(values: Sequence[float]) -> list[float]:
    """Průměrné pořadí s remízami (Spearman bez scipy/pandas)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def compute_fa_validation(
    volumes: Mapping[Key, float],
    oi_before: Mapping[Key, float],
    oi_after: Mapping[Key, float],
) -> FaValidationPoint | None:
    """Metriky dne: volumes = poslední kumulativní volume stran k řezu 21:00 UTC,
    oi_before/oi_after = archivní OI ráno téhož a následujícího dne.

    Vrací None při nedostatečném vzorku (málo obchodovaných stran nebo volume) —
    takový den se neukládá a při dalším běhu se zkusí znovu.
    """
    keys = sorted(set(volumes) | set(oi_before) | set(oi_after))
    if not keys:
        return None
    vols: list[float] = []
    abs_dois: list[float] = []
    net_sum = 0.0
    silent_sum = 0.0
    volume_sum = 0.0
    traded = 0
    for key in keys:
        volume = float(volumes.get(key, 0.0))
        delta = float(oi_after.get(key, 0.0)) - float(oi_before.get(key, 0.0))
        vols.append(volume)
        abs_dois.append(abs(delta))
        net_sum += delta
        if volume > 0:
            traded += 1
            volume_sum += volume
        else:
            silent_sum += abs(delta)
    if traded < MIN_TRADED_CONTRACTS or volume_sum < MIN_VOLUME_SUM:
        return None
    doi_abs_sum = sum(abs_dois)
    return FaValidationPoint(
        contracts=len(keys),
        volume_sum=volume_sum,
        doi_abs_sum=doi_abs_sum,
        doi_net_sum=net_sum,
        open_ratio=doi_abs_sum / volume_sum,
        spearman=_pearson(_ranks(vols), _ranks(abs_dois)),
        silent_share=silent_sum / doi_abs_sum if doi_abs_sum > 0 else 0.0,
    )
