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

import numpy as np

# Podlaha času do expirace — pod 5 minut gamma diverguje a profil by lhal
TAU_FLOOR_S = 300.0
_YEAR_S = 365.0 * 24 * 3600
_SQRT_2PI = math.sqrt(2.0 * math.pi)

# Modelované 2D pole (fáze 2): krok sloupců a strop horizontu — strop drží
# stejný časový úsek jako projekce frontendu (PROJECTION_MAX_MINUTES)
FIELD_COL_STEP_MIN = 10
FIELD_HORIZON_MIN = 24 * 60


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


@dataclass(frozen=True)
class GexField:
    """Modelované 2D pole (ADR-0009 fáze 2): budoucí sloupce s klesajícím τ.

    Sloupec `k` odpovídá času col_start + k·col_step_min minut; hodnoty sdílejí
    cenovou mřížku profilu (grid_start + i·grid_step).
    """

    ts_min: dt.datetime
    grid_start: float
    grid_step: float
    col_start: dt.datetime
    col_step_min: int
    values: tuple[tuple[float, ...], ...]  # values[sloupec][bod mřížky]


def gamma_field(
    contracts: list[ProfileContract],
    *,
    ts_min: dt.datetime,
    settle: dt.datetime,
    grid_start: float,
    grid_stop: float,
    grid_step: float,
    multiplier: float,
    col_step_min: int = FIELD_COL_STEP_MIN,
    horizon_min: int = FIELD_HORIZON_MIN,
) -> GexField | None:
    """Budoucí sloupce NetGEX(S, t) z posledního snapshotu (IV/OI se drží).

    Vektorizovaně přes numpy — pure-Python varianta (mřížka × kontrakty ×
    ~144 sloupců) by v to_thread blokovala jádro na sekundy každou minutu.
    Vrací None, když do settle nezbývá ani jeden sloupec nebo chybí vstupy.
    """
    usable = [c for c in contracts if c.iv > 0.0 and c.oi > 0.0]
    col_step = dt.timedelta(minutes=col_step_min)
    horizon = min(settle, ts_min + dt.timedelta(minutes=horizon_min))
    col_count = int((horizon - ts_min).total_seconds() // col_step.total_seconds())
    if not usable or col_count <= 0:
        return None

    count = max(1, int(round((grid_stop - grid_start) / grid_step)) + 1)
    grid = grid_start + grid_step * np.arange(count, dtype=np.float64)  # (G,)
    strikes = np.array([c.strike for c in usable], dtype=np.float64)  # (C,)
    ivs = np.array([c.iv for c in usable], dtype=np.float64)  # (C,)
    signed_oi = np.array(
        [(1.0 if c.right == "C" else -1.0) * c.oi for c in usable], dtype=np.float64
    )  # (C,)
    log_ratio = np.log(grid[:, None] / strikes[None, :])  # (G, C)

    columns: list[tuple[float, ...]] = []
    col_start = ts_min + col_step
    for idx in range(col_count):
        col_ts = col_start + idx * col_step
        tau = max((settle - col_ts).total_seconds(), TAU_FLOOR_S) / _YEAR_S
        sqrt_tau = math.sqrt(tau)
        d1 = (log_ratio + 0.5 * ivs * ivs * tau) / (ivs * sqrt_tau)  # (G, C)
        gamma = np.exp(-0.5 * d1 * d1) / (_SQRT_2PI * grid[:, None] * ivs * sqrt_tau)
        net = (gamma @ signed_oi) * multiplier  # (G,)
        columns.append(tuple(float(value) for value in net))
    return GexField(
        ts_min=ts_min,
        grid_start=grid_start,
        grid_step=grid_step,
        col_start=col_start,
        col_step_min=col_step_min,
        values=tuple(columns),
    )


def gamma_at_price(profile: GexProfile, price: float) -> float | None:
    """NetGEX profilu v místě ceny — lineární interpolace na mřížce (#350).

    Mimo mřížku vrací krajní hodnotu (extrapolace by si vymýšlela); prázdný
    profil → None.
    """
    if not profile.values or profile.grid_step <= 0:
        return None
    position = (price - profile.grid_start) / profile.grid_step
    if position <= 0:
        return profile.values[0]
    last = len(profile.values) - 1
    if position >= last:
        return profile.values[last]
    low = int(position)
    fraction = position - low
    return profile.values[low] * (1 - fraction) + profile.values[low + 1] * fraction


# ── Charm a Vanna plochy (#204) ────────────────────────────────────────
# Stejný dealer model jako gamma (NaiveDealerModel: call − put), jen jiná
# BS derivace. Pro r = q = 0 mají call i put IDENTICKÝ charm i vannu.
# Jednotky: charm za DEN (delta decay/den), vanna za 1 % IV — roční/celo-
# volové hodnoty by v heatmapě jen posouvaly řád, čitelné jsou tyhle.

GREEKS = ("gamma", "charm", "vanna")
_DAYS_PER_YEAR = 365.0
_VOL_POINT = 0.01


def _d1_d2(spot: float, strike: float, iv: float, tau_years: float) -> tuple[float, float]:
    sqrt_tau = math.sqrt(tau_years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * tau_years) / (iv * sqrt_tau)
    return d1, d1 - iv * sqrt_tau


def bs_charm(spot: float, strike: float, iv: float, tau_years: float) -> float:
    """Charm = dDelta/dČas za den (r = q = 0): φ(d1)·d2 / (2τ) / 365."""
    if spot <= 0.0 or strike <= 0.0 or iv <= 0.0 or tau_years <= 0.0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, iv, tau_years)
    phi = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
    return phi * d2 / (2.0 * tau_years) / _DAYS_PER_YEAR


def bs_vanna(spot: float, strike: float, iv: float, tau_years: float) -> float:
    """Vanna = dDelta/dVol za 1 % IV (r = q = 0): −φ(d1)·d2 / σ · 0,01."""
    if spot <= 0.0 or strike <= 0.0 or iv <= 0.0 or tau_years <= 0.0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, iv, tau_years)
    phi = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
    return -phi * d2 / iv * _VOL_POINT


def greek_profiles(
    contracts: list[ProfileContract],
    *,
    ts_min: dt.datetime,
    settle: dt.datetime,
    grid_start: float,
    grid_stop: float,
    grid_step: float,
    multiplier: float,
) -> dict[str, GexProfile]:
    """Gamma + charm + vanna profily jedním průchodem (sdílené d1/φ)."""
    tau_years = max((settle - ts_min).total_seconds(), TAU_FLOOR_S) / _YEAR_S
    usable = [c for c in contracts if c.iv > 0.0 and c.oi > 0.0]
    count = max(1, int(round((grid_stop - grid_start) / grid_step)) + 1)
    nets: dict[str, list[float]] = {greek: [] for greek in GREEKS}
    sqrt_tau = math.sqrt(tau_years)
    for i in range(count):
        spot = grid_start + i * grid_step
        gamma_net = charm_net = vanna_net = 0.0
        for contract in usable:
            sign_oi = (1.0 if contract.right == "C" else -1.0) * contract.oi
            d1 = (
                math.log(spot / contract.strike) + 0.5 * contract.iv * contract.iv * tau_years
            ) / (contract.iv * sqrt_tau)  # noqa: E501
            d2 = d1 - contract.iv * sqrt_tau
            phi = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
            gamma_net += sign_oi * phi / (spot * contract.iv * sqrt_tau)
            charm_net += sign_oi * phi * d2 / (2.0 * tau_years) / _DAYS_PER_YEAR
            vanna_net += sign_oi * (-phi * d2 / contract.iv) * _VOL_POINT
        nets["gamma"].append(gamma_net * multiplier)
        nets["charm"].append(charm_net * multiplier)
        nets["vanna"].append(vanna_net * multiplier)
    return {
        greek: GexProfile(
            ts_min=ts_min,
            grid_start=grid_start,
            grid_step=grid_step,
            values=tuple(values),
        )
        for greek, values in nets.items()
    }


def greek_fields(
    contracts: list[ProfileContract],
    *,
    ts_min: dt.datetime,
    settle: dt.datetime,
    grid_start: float,
    grid_stop: float,
    grid_step: float,
    multiplier: float,
    col_step_min: int = FIELD_COL_STEP_MIN,
    horizon_min: int = FIELD_HORIZON_MIN,
) -> dict[str, GexField] | None:
    """Budoucí sloupce všech tří ploch jedním numpy průchodem (sdílené d1/φ)."""
    usable = [c for c in contracts if c.iv > 0.0 and c.oi > 0.0]
    col_step = dt.timedelta(minutes=col_step_min)
    horizon = min(settle, ts_min + dt.timedelta(minutes=horizon_min))
    col_count = int((horizon - ts_min).total_seconds() // col_step.total_seconds())
    if not usable or col_count <= 0:
        return None

    count = max(1, int(round((grid_stop - grid_start) / grid_step)) + 1)
    grid = grid_start + grid_step * np.arange(count, dtype=np.float64)  # (G,)
    strikes = np.array([c.strike for c in usable], dtype=np.float64)  # (C,)
    ivs = np.array([c.iv for c in usable], dtype=np.float64)  # (C,)
    signed_oi = np.array(
        [(1.0 if c.right == "C" else -1.0) * c.oi for c in usable], dtype=np.float64
    )  # (C,)
    log_ratio = np.log(grid[:, None] / strikes[None, :])  # (G, C)

    columns: dict[str, list[tuple[float, ...]]] = {greek: [] for greek in GREEKS}
    col_start = ts_min + col_step
    for idx in range(col_count):
        col_ts = col_start + idx * col_step
        tau = max((settle - col_ts).total_seconds(), TAU_FLOOR_S) / _YEAR_S
        sqrt_tau = math.sqrt(tau)
        d1 = (log_ratio + 0.5 * ivs * ivs * tau) / (ivs * sqrt_tau)  # (G, C)
        d2 = d1 - ivs * sqrt_tau
        phi = np.exp(-0.5 * d1 * d1) / _SQRT_2PI
        gamma = phi / (grid[:, None] * ivs * sqrt_tau)
        charm = phi * d2 / (2.0 * tau) / _DAYS_PER_YEAR
        vanna = -phi * d2 / ivs * _VOL_POINT
        for greek, matrix in (("gamma", gamma), ("charm", charm), ("vanna", vanna)):
            net = (matrix @ signed_oi) * multiplier  # (G,)
            columns[greek].append(tuple(float(value) for value in net))
    return {
        greek: GexField(
            ts_min=ts_min,
            grid_start=grid_start,
            grid_step=grid_step,
            col_start=col_start,
            col_step_min=col_step_min,
            values=tuple(cols),
        )
        for greek, cols in columns.items()
    }
