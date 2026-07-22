"""Dyn GEX profil (ADR-0009, #203): NetGEX přes cenovou mřížku z BS gammy.

Model odpovídá na „jakou gammu potkají dealeři, KDYBY spot byl na S":
NetGEX(S) = Σ_call Γ_BS(S,K,IV,τ)·OI·M − Σ_put Γ_BS(S,K,IV,τ)·OI·M
(stejný znaménkový model jako levels — NaiveDealerModel, SPEC 4.1).
Black-Scholes gamma nad uloženou IV, r = 0, q = 0; τ s podlahou 5 minut
(τ→0 dává nekonečnou ATM gammu). Kontrakty bez IV nebo OI se vynechávají.
"""

import datetime as dt
import math
from dataclasses import dataclass

# Podlaha času do expirace — pod 5 minut gamma diverguje a profil by lhal
TAU_FLOOR_S = 300.0
_YEAR_S = 365.0 * 24 * 3600
_SQRT_2PI = math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class ProfileContract:
    """Kontrakt vstupující do profilu: strike, strana, uložená IV a OI."""

    strike: float
    right: str  # C | P
    iv: float
    oi: float


@dataclass(frozen=True)
class GexProfile:
    """NetGEX profil jedné minuty přes cenovou mřížku (ADR-0009)."""

    ts_min: dt.datetime
    grid_start: float
    grid_step: float
    values: tuple[float, ...]  # NetGEX $/bod na mřížce grid_start + i·grid_step


def bs_gamma(spot: float, strike: float, iv: float, tau_years: float) -> float:
    """Black-Scholes gamma (r = 0, q = 0) — sdílená pro obě strany opce."""
    if spot <= 0.0 or strike <= 0.0 or iv <= 0.0 or tau_years <= 0.0:
        return 0.0
    sqrt_tau = math.sqrt(tau_years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * tau_years) / (iv * sqrt_tau)
    return math.exp(-0.5 * d1 * d1) / (_SQRT_2PI * spot * iv * sqrt_tau)


def gamma_profile(
    contracts: list[ProfileContract],
    *,
    ts_min: dt.datetime,
    settle: dt.datetime,
    grid_start: float,
    grid_stop: float,
    grid_step: float,
    multiplier: float,
) -> GexProfile:
    """NetGEX(S) přes mřížku [grid_start, grid_stop] s krokem grid_step."""
    tau_years = max((settle - ts_min).total_seconds(), TAU_FLOOR_S) / _YEAR_S
    usable = [c for c in contracts if c.iv > 0.0 and c.oi > 0.0]
    count = max(1, int(round((grid_stop - grid_start) / grid_step)) + 1)
    values: list[float] = []
    for i in range(count):
        spot = grid_start + i * grid_step
        net = 0.0
        for contract in usable:
            sign = 1.0 if contract.right == "C" else -1.0
            net += sign * bs_gamma(spot, contract.strike, contract.iv, tau_years) * contract.oi
        values.append(net * multiplier)
    return GexProfile(
        ts_min=ts_min,
        grid_start=grid_start,
        grid_step=grid_step,
        values=tuple(values),
    )
