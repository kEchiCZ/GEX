"""Ranní kalibrace α flow-adjusted odhadu (#232, ADR-0011 fáze 2) — čisté výpočty.

Porovnává včerejší KONEC DNE řady netflow (kumulativní klasifikovaný net per
strana) se skutečným ΔOI mezi ranními archivy. Poměr ΔOI/net na straně říká,
kolik z klasifikovaného toku se propsalo do nového positioningu — ideální α.
Medián přes strany s dostatečným |net| je robustní vůči ATM churnu (net ≈ 0,
ΔOI ≈ 0 → strana se vůbec nekvalifikuje) i jednotlivým ujetým strikům.

Buy/sell mediány se počítají zvlášť jen pro AUDIT (uložené v historii) —
aplikuje se jedna společná α, dokud data jasně neukážou asymetrii
(rozhodnutí uživatele 6. 8.). Sběr dat a uložení dělá `storage.fa_calibration`.
"""

import statistics
from collections.abc import Mapping
from dataclasses import dataclass

Key = tuple[float, str]

# Minimální |net| kontraktů, aby strana vstoupila do kalibrace — pod prahem
# poměr ΔOI/net dominuje šum (ΔOI pár kontraktů / net pár kontraktů)
MIN_ABS_NET = 25.0
# Minimální počet kvalifikovaných stran pro platný denní bod
MIN_SAMPLES = 5
# EMA váha nového denního bodu: α konverguje během ~týdne čistých dnů,
# jeden ujetý den (crash, výpadek) výsledkem necloumá
EMA_LAMBDA = 0.3
# Meze α: záporná α (tok systematicky PROTI ΔOI) znamená rozbitá data, ne
# model; nad 1 by odhad tvrdil víc otevřeného zájmu, než kolik se zobchodovalo
ALPHA_MIN = 0.0
ALPHA_MAX = 1.0


@dataclass(frozen=True)
class AlphaCalibrationPoint:
    """Jeden denní kalibrační bod (jeden symbol, jeden trade date)."""

    samples: int  # počet stran s |net| ≥ MIN_ABS_NET
    ratio_median: float  # medián ΔOI/net přes kvalifikované strany (≈ ideální α)
    ratio_buy: float | None  # medián přes strany s net > 0 (audit asymetrie)
    ratio_sell: float | None  # medián přes strany s net < 0 (audit asymetrie)


def calibrate_alpha(
    netflow: Mapping[Key, float],
    doi: Mapping[Key, float],
    *,
    min_abs_net: float = MIN_ABS_NET,
    min_samples: int = MIN_SAMPLES,
) -> AlphaCalibrationPoint | None:
    """Denní bod z map (strike, right) → net konec dne a → skutečné ΔOI.

    Poměr se počítá se znaménkem: net nákup s poklesem OI dává záporný poměr
    a medián ho po právu stáhne dolů. Vrací None při nedostatečném vzorku —
    takový den se do kalibrace nezapočítá.
    """
    ratios: list[float] = []
    buy_ratios: list[float] = []
    sell_ratios: list[float] = []
    for key, net in netflow.items():
        if abs(net) < min_abs_net:
            continue
        ratio = float(doi.get(key, 0.0)) / float(net)
        ratios.append(ratio)
        if net > 0:
            buy_ratios.append(ratio)
        else:
            sell_ratios.append(ratio)
    if len(ratios) < min_samples:
        return None
    return AlphaCalibrationPoint(
        samples=len(ratios),
        ratio_median=statistics.median(ratios),
        ratio_buy=statistics.median(buy_ratios) if buy_ratios else None,
        ratio_sell=statistics.median(sell_ratios) if sell_ratios else None,
    )


def update_alpha(
    previous: float | None, ratio_median: float, *, ema_lambda: float = EMA_LAMBDA
) -> float:
    """EMA aktualizace α: nový denní bod sevřený do [0, 1], první bod přímo."""
    target = min(max(ratio_median, ALPHA_MIN), ALPHA_MAX)
    if previous is None:
        return target
    return previous + ema_lambda * (target - previous)
