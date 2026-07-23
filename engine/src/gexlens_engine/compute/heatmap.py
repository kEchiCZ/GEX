"""Heatmap metriky (SPEC 4.3): 7 módů + škály + normalizace.

Vše jsou čisté funkce nad snapshot maticí (bez I/O) — přepnutí módu/škály
v UI je jen přepočet v paměti (AC issue #15, latence SPEC kap. 8).

Módy vrací vrstvy jako slovník `{"call": {...}, "put": {...}}` nebo
`{"signed": {...}}` (znaménko → barva). Konvence OTM: call K > S, put K < S;
ITM je doplněk (call K ≤ S, put K ≥ S — ATM buňka patří do ITM vrstvy).
"""

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Layer = dict[float, float]
Layers = dict[str, Layer]


@dataclass(frozen=True)
class HeatmapCell:
    """Jedna buňka snapshotu: kumulativní denní volume a OI kontraktu."""

    strike: float
    right: str  # C | P
    oi: float
    volume: float
    # Vega kontraktu (VEX módy, #201) — starší volající ji nedodávají
    vega: float = 0.0


class HeatmapMode(enum.Enum):
    OI = "oi"
    VOL_OTM = "vol_otm"
    VOL_ITM = "vol_itm"
    VOL_SIGNED = "vol_signed"  # Vol ±
    OI_PLUS_OTM = "oi_plus_otm"
    OI_MINUS_ITM = "oi_minus_itm"
    OI_SIGNED_ALL = "oi_signed_all"  # OI ± All
    VEX = "vex"  # vega × OI per strana (#201)
    VEX_SIGNED = "vex_signed"  # vega×OI call − put


class HeatmapScale(enum.Enum):
    LINEAR = "linear"
    SQRT = "sqrt"
    LOG = "log"  # ln(1+v)
    CBRT = "cbrt"  # v^(1/3)


def compute_mode(
    cells: Sequence[HeatmapCell],
    mode: HeatmapMode,
    spot: float,
    *,
    oi_weight: float = 0.6,
    vol_weight: float = 0.4,
) -> Layers:
    """Spočte vrstvy heatmapy pro daný mód (čistá funkce, SPEC 4.3)."""
    calls = {c.strike: c for c in cells if c.right == "C"}
    puts = {c.strike: c for c in cells if c.right == "P"}
    strikes = sorted(set(calls) | set(puts))

    def call_vol_otm(strike: float) -> float:
        return calls[strike].volume if strike in calls and strike > spot else 0.0

    def put_vol_otm(strike: float) -> float:
        return puts[strike].volume if strike in puts and strike < spot else 0.0

    def call_vol_itm(strike: float) -> float:
        return calls[strike].volume if strike in calls and strike <= spot else 0.0

    def put_vol_itm(strike: float) -> float:
        return puts[strike].volume if strike in puts and strike >= spot else 0.0

    def oi_of(side: Mapping[float, HeatmapCell], strike: float) -> float:
        cell = side.get(strike)
        return cell.oi if cell is not None else 0.0

    def vol_of(side: Mapping[float, HeatmapCell], strike: float) -> float:
        cell = side.get(strike)
        return cell.volume if cell is not None else 0.0

    if mode is HeatmapMode.OI:
        return {
            "call": {k: oi_of(calls, k) for k in strikes},
            "put": {k: oi_of(puts, k) for k in strikes},
        }
    if mode is HeatmapMode.VOL_OTM:
        return {
            "call": {k: call_vol_otm(k) for k in strikes},
            "put": {k: put_vol_otm(k) for k in strikes},
        }
    if mode is HeatmapMode.VOL_ITM:
        return {
            "call": {k: call_vol_itm(k) for k in strikes},
            "put": {k: put_vol_itm(k) for k in strikes},
        }
    if mode is HeatmapMode.VOL_SIGNED:
        return {"signed": {k: vol_of(calls, k) - vol_of(puts, k) for k in strikes}}
    if mode is HeatmapMode.OI_PLUS_OTM:
        # Složky se normalizují na společné maximum, aby byly váhy srovnatelné
        max_oi = max((oi_of(s, k) for s in (calls, puts) for k in strikes), default=0.0)
        max_otm = max(
            [call_vol_otm(k) for k in strikes] + [put_vol_otm(k) for k in strikes],
            default=0.0,
        )

        def blend(oi: float, otm: float) -> float:
            oi_part = oi / max_oi if max_oi > 0 else 0.0
            otm_part = otm / max_otm if max_otm > 0 else 0.0
            return oi_weight * oi_part + vol_weight * otm_part

        return {
            "call": {k: blend(oi_of(calls, k), call_vol_otm(k)) for k in strikes},
            "put": {k: blend(oi_of(puts, k), put_vol_otm(k)) for k in strikes},
        }
    if mode is HeatmapMode.OI_MINUS_ITM:
        return {
            "call": {k: oi_of(calls, k) - call_vol_itm(k) for k in strikes},
            "put": {k: oi_of(puts, k) - put_vol_itm(k) for k in strikes},
        }
    if mode is HeatmapMode.OI_SIGNED_ALL:
        return {"signed": {k: oi_of(calls, k) - oi_of(puts, k) for k in strikes}}

    def vex_of(side: Mapping[float, HeatmapCell], strike: float) -> float:
        """Vega Exposure strany (#201): vega × OI — $ přecenění na 1 bod IV."""
        cell = side.get(strike)
        return cell.vega * cell.oi if cell is not None else 0.0

    if mode is HeatmapMode.VEX:
        return {
            "call": {k: vex_of(calls, k) for k in strikes},
            "put": {k: vex_of(puts, k) for k in strikes},
        }
    if mode is HeatmapMode.VEX_SIGNED:
        return {"signed": {k: vex_of(calls, k) - vex_of(puts, k) for k in strikes}}
    raise ValueError(f"Neznámý heatmap mód: {mode!r}")


def apply_scale(layer: Mapping[float, float], scale: HeatmapScale) -> Layer:
    """Škálová transformace hodnot se zachováním znaménka (SPEC 4.3)."""
    if scale is HeatmapScale.LINEAR:
        return dict(layer)
    if scale is HeatmapScale.SQRT:
        return {k: math.copysign(math.sqrt(abs(v)), v) for k, v in layer.items()}
    if scale is HeatmapScale.LOG:
        return {k: math.copysign(math.log1p(abs(v)), v) for k, v in layer.items()}
    if scale is HeatmapScale.CBRT:
        return {k: math.copysign(abs(v) ** (1.0 / 3.0), v) for k, v in layer.items()}
    raise ValueError(f"Neznámá škála: {scale!r}")


def p99_denominator(values: Sequence[float]) -> float:
    """p99 absolutních hodnot viditelného okna — robustní normalizace vůči outlierům."""
    magnitudes = sorted(abs(v) for v in values)
    if not magnitudes:
        return 0.0
    index = max(0, math.ceil(0.99 * len(magnitudes)) - 1)
    return magnitudes[index]


def normalize(layer: Mapping[float, float], denominator: float) -> Layer:
    """Normalizace vrstvy daným jmenovatelem (p99 okna nebo globální max).

    Nulový jmenovatel → nulová vrstva (prázdné okno se nesmí dělit nulou).
    """
    if denominator <= 0.0:
        return dict.fromkeys(layer, 0.0)
    return {k: v / denominator for k, v in layer.items()}
