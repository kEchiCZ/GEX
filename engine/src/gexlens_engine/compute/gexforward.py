"""Forward GEX (#519): modelové Dyn pole přes budoucí obchodní dny.

Sloupec = jeden budoucí obchodní den (víkend se přeskakuje). Pole dne D se
počítá ze všech kontraktů, jejichž expirace k referenčnímu času dne D ještě
žije — každý kontrakt s τ do SVÉ vlastní expirace. Mezi dnem expirace a dnem
po ní tak vzniká přirozený skok („gamma útes"), nic se nedopočítává uměle.

Konvence (schváleno uživatelem 13. 8. 2026):
- referenční čas dne = poledne US seance (12:00 CT) — 0DTE dne D v poli
  ještě žije, odpadá až sloupcem D+1; settle jako reference by 0DTE ukázal
  s τ≈0 (nesmyslná ATM špička),
- horizont = vždy do konce týdne (pátek), UI přepínač jen filtruje,
- vstupní IV: měřená z denního snímku řetězce (ranní OI průchod); kontrakt
  bez IV dostane fallback z nejbližší měřené expirace podle moneyness,
- jednotky: $/bod (Γ·OI·M) — váha P²/100 až při čtení/kreslení (#569).

Popisek útesu („po E3C −38 % gammy") se počítá z téhož pole: podíl |NetGEX|
masy odpadající expirace na celkové mase v den PŘED odpadem — stejná
sémantika jako gamma_cliff (#576), takže si čísla neodporují.
"""

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from gexlens_engine.compute.gexfield import _YEAR_S, TAU_FLOOR_S
from gexlens_engine.compute.settle import CME_TZ, session_time_utc, settle_ts

# Poledne US seance (CT) — referenční čas budoucího dne
NOON_LOCAL = dt.time(12, 0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class ForwardContract:
    """Kontrakt denního snímku řetězce vstupující do forward pole."""

    expiry: str  # YYYYMMDD
    strike: float
    right: str  # C | P
    oi: float
    iv: float | None  # None = snímek IV nedodal → moneyness fallback


@dataclass(frozen=True)
class ForwardDayBlock:
    """Jeden budoucí obchodní den forward pole."""

    date: dt.date
    values: tuple[float, ...]  # NetGEX $/bod na mřížce
    #: Expirace odpadlé mezi minulým a tímto dnem (typicky jedna)
    dropped_expiries: tuple[str, ...]
    #: Podíl |NetGEX| masy odpadlých expirací na celku PŘEDCHOZÍHO dne;
    #: None u prvního dne (není vůči čemu)
    dropped_share: float | None


@dataclass(frozen=True)
class ForwardField:
    grid_start: float
    grid_step: float
    days: tuple[ForwardDayBlock, ...]
    #: Podíl kontraktů s dopočtenou (ne měřenou) IV — poctivost modelu
    iv_fallback_share: float


def trading_days_until_friday(today: dt.date) -> list[dt.date]:
    """Dnešek + budoucí obchodní dny do konce týdne (pátek), víkend přeskočen.

    Sobota/neděle jako vstup vrací jen následující týden nezačíná — horizont
    „do konce týdne" o víkendu znamená prázdno; forward se počítá po ranním
    OI archivu, který o víkendu neběží, takže je to okrajový stav.
    """
    days: list[dt.date] = []
    day = today
    while day.weekday() < 5:
        days.append(day)
        if day.weekday() == 4:
            break
        day += dt.timedelta(days=1)
    return days


def day_reference_ts(day: dt.date) -> dt.datetime:
    """Referenční okamžik dne: 12:00 CT v UTC."""
    return session_time_utc(day, NOON_LOCAL.hour, NOON_LOCAL.minute, CME_TZ)


def fill_iv_by_moneyness(contracts: list[ForwardContract]) -> tuple[list[ForwardContract], float]:
    """Doplní chybějící IV z nejbližší měřené expirace podle strike vzdálenosti.

    Vrací (kontrakty s doplněnou IV, podíl fallbacků). Kontrakt bez IV, pro
    který neexistuje žádná měřená expirace téže strany, se vynechává — model
    bez volatility nejde poctivě spočítat.
    """
    # (expiry, right) → seřazené (strike, iv) měřených kontraktů
    measured: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for c in contracts:
        if c.iv is not None and c.iv > 0.0:
            measured.setdefault((c.expiry, c.right), []).append((c.strike, c.iv))
    for pairs in measured.values():
        pairs.sort()
    measured_expiries = sorted({expiry for expiry, _right in measured})

    def nearest_iv(c: ForwardContract) -> float | None:
        # Nejbližší měřená expirace (podle data), v ní nejbližší strike
        candidates = sorted(measured_expiries, key=lambda e: abs(int(e) - int(c.expiry)))
        for expiry in candidates:
            pairs = measured.get((expiry, c.right))
            if not pairs:
                continue
            return min(pairs, key=lambda pair: abs(pair[0] - c.strike))[1]
        return None

    out: list[ForwardContract] = []
    fallbacks = 0
    for c in contracts:
        if c.iv is not None and c.iv > 0.0:
            out.append(c)
            continue
        iv = nearest_iv(c)
        if iv is None:
            continue
        fallbacks += 1
        out.append(ForwardContract(expiry=c.expiry, strike=c.strike, right=c.right, oi=c.oi, iv=iv))
    share = fallbacks / len(out) if out else 0.0
    return out, share


def forward_field(
    contracts: list[ForwardContract],
    *,
    today: dt.date,
    grid_start: float,
    grid_stop: float,
    grid_step: float,
    multiplier: float,
) -> ForwardField | None:
    """NetGEX(S) per budoucí obchodní den; None bez použitelných vstupů.

    Vektorizace přes numpy jako `gamma_field` — mřížka × kontrakty × ≤5 dnů.
    """
    filled, fallback_share = fill_iv_by_moneyness(
        [c for c in contracts if c.oi > 0.0 and c.strike > 0.0]
    )
    days = trading_days_until_friday(today)
    if not filled or not days:
        return None

    count = max(1, int(round((grid_stop - grid_start) / grid_step)) + 1)
    grid = grid_start + grid_step * np.arange(count, dtype=np.float64)  # (G,)
    strikes = np.array([c.strike for c in filled], dtype=np.float64)
    ivs = np.array([c.iv for c in filled], dtype=np.float64)
    signed_oi = np.array(
        [(1.0 if c.right == "C" else -1.0) * c.oi for c in filled], dtype=np.float64
    )
    settles = np.array(
        [settle_ts(dt.datetime.strptime(c.expiry, "%Y%m%d").date()).timestamp() for c in filled]
    )
    expiries = [c.expiry for c in filled]
    log_ratio = np.log(grid[:, None] / strikes[None, :])  # (G, C)

    def day_column(ref_s: float) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
        """(NetGEX sloupec, maska živých kontraktů, |masa| per expirace)."""
        alive = settles > ref_s
        tau = np.maximum(settles - ref_s, TAU_FLOOR_S) / _YEAR_S  # (C,)
        sqrt_tau = np.sqrt(tau)
        d1 = (log_ratio + 0.5 * ivs * ivs * tau) / (ivs * sqrt_tau)  # (G, C)
        gamma = np.exp(-0.5 * d1 * d1) / (_SQRT_2PI * grid[:, None] * ivs * sqrt_tau)
        gamma = gamma * alive[None, :]
        net = (gamma @ signed_oi) * multiplier  # (G,)
        # |masa| per expirace: Σ_grid |Σ_kontraktů expirace Γ·OI·M|
        masses: dict[str, float] = {}
        for expiry in sorted(set(expiries)):
            sel = np.array([e == expiry for e in expiries]) & alive
            if not sel.any():
                continue
            net_e = (gamma[:, sel] @ signed_oi[sel]) * multiplier
            masses[expiry] = float(np.abs(net_e).sum())
        return net, alive, masses

    blocks: list[ForwardDayBlock] = []
    previous_alive: set[str] | None = None
    previous_masses: dict[str, float] = {}
    for day in days:
        ref_s = day_reference_ts(day).timestamp()
        net, alive_mask, masses = day_column(ref_s)
        alive_expiries = {e for e, keep in zip(expiries, alive_mask, strict=True) if keep}
        dropped: tuple[str, ...] = ()
        share: float | None = None
        if previous_alive is not None:
            dropped = tuple(sorted(previous_alive - alive_expiries))
            total = sum(previous_masses.values())
            if dropped and total > 0.0:
                share = sum(previous_masses.get(e, 0.0) for e in dropped) / total
        blocks.append(
            ForwardDayBlock(
                date=day,
                values=tuple(round(float(v), 1) for v in net),
                dropped_expiries=dropped,
                dropped_share=round(share, 4) if share is not None else None,
            )
        )
        previous_alive = alive_expiries
        previous_masses = masses

    return ForwardField(
        grid_start=grid_start,
        grid_step=grid_step,
        days=tuple(blocks),
        iv_fallback_share=round(fallback_share, 4),
    )
