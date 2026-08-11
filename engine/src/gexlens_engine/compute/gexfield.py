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


# ── Hranice gamma masy (#600) ──────────────────────────────────────────

# Práh okraje masy: podíl z maxima |NetGEX| profilu. Za hranicí gamma masy
# vyhasíná, takže dealerský hedging tam cenu přestává tlumit.
#
# 0,10 je PRVNÍ NÁSTŘEL, ne změřená hodnota — profil má u ATM řádově vyšší
# gammu než na křídlech, takže desetina maxima leží typicky až za posledními
# významnými striky. Kalibruje se měřením v #601 (kolik průrazů, jaký pohyb,
# kolik falešných); do té doby je to jediná konstanta, na které hranice visí.
GAMMA_EDGE_SHARE = 0.10


@dataclass(frozen=True)
class GammaEdges:
    """Krajní ceny, kde |NetGEX| ještě překročí práh — okraj gamma masy (#600).

    `up`/`dn` jsou v bodech podkladu; `None` = hranici nelze určit (prázdný
    profil, samé nuly). Hodnota na kraji mřížky znamená, že masa mřížku
    přesahuje a skutečná hranice leží dál.
    """

    up: float | None
    dn: float | None
    threshold: float


def gamma_edges(profile: GexProfile, *, share: float = GAMMA_EDGE_SHARE) -> GammaEdges:
    """Horní a dolní hranice gamma masy profilu.

    Hledá se GLOBÁLNÍ krajní bod nad prahem, ne souvislé pásmo od spotu:
    profil prochází u flipu nulou, takže „souvislé pásmo" by se o flip přeťalo
    a hranice by spadla doprostřed masy. Mezi posledním bodem nad prahem a
    prvním pod ním se lineárně interpoluje.
    """
    values = profile.values
    if not values or profile.grid_step <= 0:
        return GammaEdges(up=None, dn=None, threshold=0.0)
    peak = max(abs(value) for value in values)
    threshold = peak * max(0.0, share)
    if peak <= 0.0 or threshold <= 0.0:
        return GammaEdges(up=None, dn=None, threshold=0.0)

    above = [index for index, value in enumerate(values) if abs(value) >= threshold]
    if not above:
        return GammaEdges(up=None, dn=None, threshold=threshold)

    def price_at(index: float) -> float:
        return profile.grid_start + index * profile.grid_step

    def crossing(inner: int, outer: int) -> float:
        """Cena, kde |NetGEX| protne práh mezi sousedy `inner` (nad) a `outer` (pod)."""
        high = abs(values[inner])
        low = abs(values[outer])
        span = high - low
        if span <= 0.0:
            return price_at(inner)
        fraction = (high - threshold) / span
        return price_at(inner) + (price_at(outer) - price_at(inner)) * fraction

    top = above[-1]
    bottom = above[0]
    up = price_at(top) if top == len(values) - 1 else crossing(top, top + 1)
    dn = price_at(bottom) if bottom == 0 else crossing(bottom, bottom - 1)
    return GammaEdges(up=up, dn=dn, threshold=threshold)


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


# ── Fallback greeks z mid ceny (#547) ──────────────────────────────────
# TWS umí pro část striků trvale nedodávat modelGreeks, přestože kotace tečou
# (7. 8.: celé ATM pásmo NQ QN1 0DTE, celou seanci). Engine si pak greeks
# dopočítá sám: IV inverzí BS ceny z mid, delta/gamma/vega/theta z téže formule
# (r = q = 0). Jednotky kopírují TWS modelGreeks: vega za 1 % IV, theta za den.

_IV_LO = 1e-4
_IV_HI = 10.0
_IV_MAX_ITER = 100
_IV_REL_TOL = 1e-9


def _norm_cdf(x: float) -> float:
    """Distribuční funkce N(0,1) přes math.erf — bez závislosti na scipy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, iv: float, tau_years: float, right: str) -> float:
    """BS cena evropské opce (r = q = 0); nevalidní vstupy → 0, iv ≤ 0 → vnitřní hodnota."""
    if spot <= 0.0 or strike <= 0.0 or tau_years <= 0.0:
        return 0.0
    intrinsic = max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
    if iv <= 0.0:
        return intrinsic
    d1, d2 = _d1_d2(spot, strike, iv, tau_years)
    call = spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return call if right == "C" else call - spot + strike  # put-call parita při r = 0


def implied_vol(
    price: float, spot: float, strike: float, tau_years: float, right: str
) -> float | None:
    """IV inverzí BS ceny (#547): Newton krok jištěný bisekcí v [1e-4, 10].

    Vrací None mimo no-arbitrage pásmo (cena ≤ vnitřní hodnota, cena ≥ spot
    resp. strike) a při nekonvergenci — volající nechá strike nekompletní,
    vymyšlená IV je horší než díra.
    """
    if price <= 0.0 or spot <= 0.0 or strike <= 0.0 or tau_years <= 0.0:
        return None
    if right not in ("C", "P"):
        return None
    intrinsic = max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
    upper = spot if right == "C" else strike
    if price <= intrinsic or price >= upper:
        return None
    lo, hi = _IV_LO, _IV_HI
    if price <= bs_price(spot, strike, lo, tau_years, right):
        return None  # cena na úrovni vnitřní hodnoty — IV pod rozlišením
    if price >= bs_price(spot, strike, hi, tau_years, right):
        return None  # na cenu nestačí ani 1000 % vol
    iv = 0.5
    for _ in range(_IV_MAX_ITER):
        diff = bs_price(spot, strike, iv, tau_years, right) - price
        if abs(diff) <= _IV_REL_TOL * max(price, 1.0):
            return iv
        if diff > 0.0:
            hi = iv
        else:
            lo = iv
        # Newton krok přes vegu; mimo závorku nebo s mizivou vegou → bisekce
        d1, _ = _d1_d2(spot, strike, iv, tau_years)
        vega = spot * math.exp(-0.5 * d1 * d1) / _SQRT_2PI * math.sqrt(tau_years)
        newton = iv - diff / vega if vega > 1e-12 else None
        iv = newton if newton is not None and lo < newton < hi else 0.5 * (lo + hi)
    return None


@dataclass(frozen=True)
class FallbackGreeks:
    """Vlastní BS greeks (#547) — jednotky dle TWS: vega za 1 % IV, theta za den."""

    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


def fallback_greeks(
    *,
    spot: float,
    strike: float,
    right: str,
    mid: float,
    settle: dt.datetime,
    now: dt.datetime,
) -> FallbackGreeks | None:
    """BS greeks z mid ceny (#547): IV inverzí, zbytek z téže formule (r = q = 0).

    τ do settle s podlahou TAU_FLOOR_S (τ→0 diverguje ATM gamma). None při ceně
    mimo no-arbitrage pásmo nebo nekonvergenci — žádné vymyšlené hodnoty.
    """
    if spot <= 0.0 or strike <= 0.0 or mid <= 0.0:
        return None
    tau_years = max((settle - now).total_seconds(), TAU_FLOOR_S) / _YEAR_S
    iv = implied_vol(mid, spot, strike, tau_years, right)
    if iv is None:
        return None
    d1, _ = _d1_d2(spot, strike, iv, tau_years)
    phi = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
    sqrt_tau = math.sqrt(tau_years)
    delta = _norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1.0
    # Theta pro r = 0 je pro call i put identická (jen časový rozpad φ)
    theta = -(spot * phi * iv) / (2.0 * sqrt_tau) / _DAYS_PER_YEAR
    vega = spot * phi * sqrt_tau * _VOL_POINT
    return FallbackGreeks(
        iv=iv,
        delta=delta,
        gamma=bs_gamma(spot, strike, iv, tau_years),
        theta=theta,
        vega=vega,
    )
