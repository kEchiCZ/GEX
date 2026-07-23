"""Vol leadeři (#208): detekce neobvyklé koncentrace volume na jednom striku.

Alanův event-workflow: před after-close eventem se čte, kde se nejvíc obchoduje
na příští expiraci — jeden dominantní strike je úroveň, kde se trh zajišťuje.
Detektor hlásí stranu (strike × C/P), jejíž volume výrazně převyšuje medián
top-N stran téže expirace. Čistá funkce — orchestraci (anti-spam, alert)
dělá InstrumentPipeline.
"""

import statistics
from collections.abc import Mapping
from dataclasses import dataclass

# Medián se počítá z top-N stran (vč. leadera — konzervativnější poměr)
TOP_N = 10
# Pod tolika obchodovanými stranami nemá medián vypovídací hodnotu
MIN_SIDES = 3


@dataclass(frozen=True)
class VolConcentration:
    """Dominantní strana a její poměr k mediánu top-N."""

    strike: float
    right: str
    volume: float
    median_top: float
    ratio: float


def detect_concentration(
    volumes: Mapping[tuple[float, str], float],
    *,
    ratio: float,
    min_volume: float,
) -> VolConcentration | None:
    """Vrátí dominantní stranu, když leader ≥ ratio × medián top-N; jinak None.

    `min_volume` je absolutní podlaha leadera — brzy ráno by pár kontraktů
    snadno přestřelilo poměr, aniž by šlo o skutečnou koncentraci.
    """
    ranked = sorted(
        ((key, volume) for key, volume in volumes.items() if volume > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    top = ranked[:TOP_N]
    if len(top) < MIN_SIDES:
        return None
    (leader_strike, leader_right), leader_volume = top[0]
    if leader_volume < min_volume:
        return None
    median_top = statistics.median(volume for _, volume in top)
    if median_top <= 0 or leader_volume < ratio * median_top:
        return None
    return VolConcentration(
        strike=leader_strike,
        right=leader_right,
        volume=leader_volume,
        median_top=median_top,
        ratio=leader_volume / median_top,
    )
