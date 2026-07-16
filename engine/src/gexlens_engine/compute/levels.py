"""GEX Levels (SPEC 4.2): zero-gamma flip, call/put wall, centroid (HVL).

- Flip: kumulativní NetGEX od nejnižšího striku, nulový průchod lineárně
  interpolovaný mezi sousedními strikes. Pokud kumulativní řada nulou
  neprojde (celý profil kladný/záporný), flip neexistuje → None.
  Při více průchodech se bere ten nejblíž spotu.
- Call Wall: argmax NetGEX(K) pro K > spot; Put Wall: argmin pro K < spot.
- Centroid (HVL): Σ(K·|NetGEX|) / Σ|NetGEX|.
"""

import itertools
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class GexLevels:
    """Levels jednoho snapshotu; None = úroveň v daném profilu neexistuje."""

    flip: float | None
    call_wall: float | None
    put_wall: float | None
    centroid: float | None
    total_gex: float


def compute_levels(net_by_strike: Mapping[float, float], spot: float) -> GexLevels:
    """Spočte levels z NetGEX profilu (výstup GexResult.net_by_strike)."""
    strikes = sorted(net_by_strike)
    nets = [net_by_strike[strike] for strike in strikes]
    return GexLevels(
        flip=_flip(strikes, nets, spot),
        call_wall=_call_wall(net_by_strike, spot),
        put_wall=_put_wall(net_by_strike, spot),
        centroid=_centroid(net_by_strike),
        total_gex=sum(nets),
    )


def _flip(strikes: list[float], nets: list[float], spot: float) -> float | None:
    if not strikes:
        return None
    cumulative = list(itertools.accumulate(nets))
    crossings: list[float] = []
    for i in range(len(strikes) - 1):
        c1, c2 = cumulative[i], cumulative[i + 1]
        if c1 == 0.0:
            crossings.append(strikes[i])
        elif (c1 < 0.0 < c2) or (c2 < 0.0 < c1):
            k1, k2 = strikes[i], strikes[i + 1]
            crossings.append(k1 + (0.0 - c1) * (k2 - k1) / (c2 - c1))
    if cumulative[-1] == 0.0:
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


def _centroid(net_by_strike: Mapping[float, float]) -> float | None:
    total_abs = sum(abs(net) for net in net_by_strike.values())
    if total_abs == 0.0:
        return None
    return sum(strike * abs(net) for strike, net in net_by_strike.items()) / total_abs
