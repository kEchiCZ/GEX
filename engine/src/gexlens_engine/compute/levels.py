"""GEX Levels (SPEC 4.2): zero-gamma flip, call/put wall, centroid (HVL).

- Flip: kumulativní NetGEX od nejnižšího striku, nulový průchod lineárně
  interpolovaný mezi sousedními strikes. Pokud kumulativní řada nulou
  neprojde (celý profil kladný/záporný), flip neexistuje → None.
  Při více průchodech se bere ten nejblíž spotu.
- Call Wall: argmax NetGEX(K) pro K > spot; Put Wall: argmin pro K < spot.
- Sekundární zdi (ADR-0008, #92): druhé nejsilnější LOKÁLNÍ maximum téže
  strany se sílou >= SECONDARY_WALL_RATIO × primární — dvě rovnocenné
  koncentrace, mezi kterými primární zeď minutu po minutě přeskakuje.
- Centroid (HVL): Σ(K·|NetGEX|) / Σ|NetGEX|.
"""

import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field

# Sekundární zeď se hlásí, jen když má aspoň tento podíl síly primární —
# slabší koncentrace nejsou „rovnocenná zeď", jen šum (ADR-0008)
SECONDARY_WALL_RATIO = 0.7


@dataclass(frozen=True)
class GexLevels:
    """Levels jednoho snapshotu; None = úroveň v daném profilu neexistuje."""

    flip: float | None
    call_wall: float | None
    put_wall: float | None
    centroid: float | None
    total_gex: float
    # Sekundární zdi (ADR-0008) — kw_only kvůli zpětné kompatibilitě konstrukce
    call_wall_2: float | None = field(default=None, kw_only=True)
    put_wall_2: float | None = field(default=None, kw_only=True)


def compute_levels(net_by_strike: Mapping[float, float], spot: float) -> GexLevels:
    """Spočte levels z NetGEX profilu (výstup GexResult.net_by_strike)."""
    strikes = sorted(net_by_strike)
    nets = [net_by_strike[strike] for strike in strikes]
    call_wall = _call_wall(net_by_strike, spot)
    put_wall = _put_wall(net_by_strike, spot)
    return GexLevels(
        flip=_flip(strikes, nets, spot),
        call_wall=call_wall,
        put_wall=put_wall,
        centroid=_centroid(net_by_strike),
        total_gex=sum(nets),
        call_wall_2=_secondary_wall(net_by_strike, spot, call_wall, side="call"),
        put_wall_2=_secondary_wall(net_by_strike, spot, put_wall, side="put"),
    )


def _flip(strikes: list[float], nets: list[float], spot: float) -> float | None:
    if not strikes:
        return None
    cumulative = list(itertools.accumulate(nets))
    # Vodicí nuly NEJSOU průchod: okrajové strikes pásma bývají prázdné, takže
    # kumulativní řada začíná na nule — starý kód to bral jako nulový průchod
    # a flip pak skákal na okraj pásma (#197). Průchody se hledají až od první
    # nenulové hodnoty; celý nulový profil flip nemá.
    first_nonzero = next((i for i, value in enumerate(cumulative) if value != 0.0), None)
    if first_nonzero is None:
        return None
    crossings: list[float] = []
    for i in range(first_nonzero, len(strikes) - 1):
        c1, c2 = cumulative[i], cumulative[i + 1]
        if c1 == 0.0:
            crossings.append(strikes[i])
        elif (c1 < 0.0 < c2) or (c2 < 0.0 < c1):
            k1, k2 = strikes[i], strikes[i + 1]
            crossings.append(k1 + (0.0 - c1) * (k2 - k1) / (c2 - c1))
    if cumulative[-1] == 0.0 and len(strikes) - 1 > first_nonzero:
        crossings.append(strikes[-1])
    if not crossings:
        return None
    return min(crossings, key=lambda strike: abs(strike - spot))


def _call_wall(net_by_strike: Mapping[float, float], spot: float) -> float | None:
    above = {strike: net for strike, net in net_by_strike.items() if strike > spot}
    if not above:
        return None
    return max(above, key=lambda strike: above[strike])


def _put_wall(net_by_strike: Mapping[float, float], spot: float) -> float | None:
    below = {strike: net for strike, net in net_by_strike.items() if strike < spot}
    if not below:
        return None
    return min(below, key=lambda strike: below[strike])


def _secondary_wall(
    net_by_strike: Mapping[float, float],
    spot: float,
    primary: float | None,
    side: str,
) -> float | None:
    """Druhé nejsilnější lokální maximum strany zdi (ADR-0008, #92).

    Kandidát musí být LOKÁLNÍ vrchol profilu (soused primární koncentrace není
    druhá zeď, jen její rameno) a mít aspoň SECONDARY_WALL_RATIO síly primární.
    """
    if primary is None:
        return None
    if side == "call":
        region = {k: v for k, v in net_by_strike.items() if k > spot}
    else:
        # Put wall je argmin (nejzápornější NetGEX) — síla = převrácené znaménko
        region = {k: -v for k, v in net_by_strike.items() if k < spot}
    strikes = sorted(region)
    values = [region[k] for k in strikes]
    primary_strength = region.get(primary, 0.0)
    if primary_strength <= 0.0:
        return None
    best: float | None = None
    best_value = 0.0
    for i, value in enumerate(values):
        if strikes[i] == primary or value <= 0.0:
            continue
        left = values[i - 1] if i > 0 else float("-inf")
        right = values[i + 1] if i < len(values) - 1 else float("-inf")
        # Lokální vrchol (plató drží první bod zleva): ostře nad levým sousedem,
        # aspoň roven pravému — rameno primární koncentrace neprojde
        if not (value > left and value >= right):
            continue
        if value > best_value:
            best_value = value
            best = strikes[i]
    if best is None or best_value < SECONDARY_WALL_RATIO * primary_strength:
        return None
    return best


def _centroid(net_by_strike: Mapping[float, float]) -> float | None:
    total_abs = sum(abs(net) for net in net_by_strike.values())
    if total_abs == 0.0:
        return None
    return sum(strike * abs(net) for strike, net in net_by_strike.items()) / total_abs
