"""Risk framework malého účtu (#1185, varianta A, rozhodnutí uživatele 15. 9. 2026).

Čisté funkce bez I/O; orchestraci (čtení track recordu, zápis do kontextu
setupu, alerty) dělá `SetupEngine`.

Účet se v aplikaci vede v jednotkách plného kontraktu (ES 50 $/b, NQ 20 $/b)
jako **50 000 $** — uživatel obchoduje MES/MNQ (1/10), takže 1 kontrakt v
aplikaci = 1 mikro reálně a body sedí 1:1. Riziko na setup 1 % = 500 $ v
aplikaci (50 $ reálně) → pro 1 kontrakt stop nejvýš 10 b ES / 25 b NQ.
Setup se stopem nad rozpočtem VZNIKÁ dál (měření, varianta A ≠ C), ale je
`unaffordable`: šedý v Setupech, bez pushe, mimo obchodovatelné statistiky.

Brzdy (v R obchodovatelných setupů napříč symboly): −3 R za seanci a −6 R za
týden zastaví nové obchodovatelné setupy do settle; třetí stop téže šablony
za seanci ji do settle zastaví. Brána šablon: obchodovatelná je jen šablona,
jejíž dolní mez očekávání (jednostranný 95% interval Ø R) za posledních N
seancí je kladná při n ≥ 30 — ostatní se dál měří, ale neobchodují.
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.compute.settle import session_bounds

#: Verze pravidel sizingu/brzd — do kontextu setupu, aby šly řádky rozlišit
RISK_RULES_VERSION = 1

TradeBlock = Literal[
    "stop_over_budget", "stop_over_cap", "daily_brake", "weekly_brake", "template_stops", "gate"
]
GateVerdict = Literal["pass", "block", "insufficient", "off"]


@dataclass(frozen=True)
class PositionSize:
    stop_points: float
    point_value_usd: float
    risk_budget_usd: float
    contracts: int
    #: Ztráta na stopu při `contracts` kontraktech (0 při neobchodovatelném)
    max_loss_usd: float
    affordable: bool
    block: TradeBlock | None


def position_size(
    entry: float,
    stop: float,
    point_value_usd: float,
    *,
    account_equity_usd: float,
    risk_pct: float,
    risk_max_pct: float,
) -> PositionSize:
    """Fixed-fractional sizing: kontrakty = ⌊rozpočet / (stop b × hodnota bodu)⌋.

    Rozpočet = účet × risk_pct; tvrdý strop účet × risk_max_pct chrání před
    zvednutím risk_pct nad strop (ztráta jednoho setupu nikdy nad 2 %).
    Stop 0 b nebo nesmyslná hodnota bodu = neobchodovatelné, ne výjimka.
    """
    stop_points = abs(entry - stop)
    budget = max(0.0, account_equity_usd * risk_pct / 100.0)
    cap = max(0.0, account_equity_usd * risk_max_pct / 100.0)
    per_contract = stop_points * point_value_usd
    if per_contract <= 0 or budget <= 0:
        return PositionSize(stop_points, point_value_usd, budget, 0, 0.0, False, "stop_over_budget")
    contracts = int(math.floor(budget / per_contract))
    if contracts < 1:
        return PositionSize(stop_points, point_value_usd, budget, 0, 0.0, False, "stop_over_budget")
    max_loss = contracts * per_contract
    if max_loss > cap:
        return PositionSize(stop_points, point_value_usd, budget, 0, 0.0, False, "stop_over_cap")
    return PositionSize(stop_points, point_value_usd, budget, contracts, max_loss, True, None)


@dataclass(frozen=True)
class RealizedSetup:
    """Uzavřený setup pro brzdy a bránu (podmnožina řádku `setups`)."""

    symbol: str
    template: str
    status: str
    outcome_r: float
    closed_ts: dt.datetime
    #: `context.tradeable` řádku; None = řádek před pravidly (#1185)
    tradeable: bool | None
    #: `context.affordable`; None = řádek před pravidly → dopočet z entry/stop
    affordable: bool | None = None
    entry: float = 0.0
    stop: float = 0.0


def week_start(session_day: dt.date) -> dt.datetime:
    """Začátek obchodního týdne = open pondělní seance (neděle 17:00 CT)."""
    monday = session_day - dt.timedelta(days=session_day.weekday())
    return session_bounds(monday)[0]


@dataclass(frozen=True)
class BrakeState:
    day_r: float
    week_r: float
    template_stops: int
    block: TradeBlock | None


def brake_state(
    realized: Sequence[RealizedSetup],
    template: str,
    *,
    session_day: dt.date,
    daily_brake_r: float,
    weekly_brake_r: float,
    max_template_stops_per_day: int,
) -> BrakeState:
    """Brzdy z obchodovatelných uzavřených setupů týdne (napříč symboly).

    Řádky bez `tradeable` (před #1185) se nepočítají — brzda je o účtu, ne o
    detektoru. Priorita: den → týden → šablona (první, která platí).
    """
    day_from, day_to = session_bounds(session_day)
    day_r = week_r = 0.0
    template_stops = 0
    for row in realized:
        if row.tradeable is not True:
            continue
        week_r += row.outcome_r
        if day_from <= row.closed_ts < day_to:
            day_r += row.outcome_r
            if row.template == template and row.status == "closed_stop":
                template_stops += 1
    block: TradeBlock | None = None
    if daily_brake_r > 0 and day_r <= -daily_brake_r:
        block = "daily_brake"
    elif weekly_brake_r > 0 and week_r <= -weekly_brake_r:
        block = "weekly_brake"
    elif max_template_stops_per_day > 0 and template_stops >= max_template_stops_per_day:
        block = "template_stops"
    return BrakeState(day_r=day_r, week_r=week_r, template_stops=template_stops, block=block)


def expectancy_lower_bound(results: Sequence[float], z: float = 1.645) -> float | None:
    """Dolní mez jednostranného 95% intervalu Ø R (t≈z, n ≥ 2). None pod 2 vzorky."""
    n = len(results)
    if n < 2:
        return None
    mean = sum(results) / n
    variance = sum((value - mean) ** 2 for value in results) / (n - 1)
    return mean - z * math.sqrt(variance / n)


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    n: int
    lower_bound: float | None


def affordable_results(
    realized: Sequence[RealizedSetup],
    template: str,
    *,
    since: dt.datetime,
    point_values: Mapping[str, float],
    account_equity_usd: float,
    risk_pct: float,
    risk_max_pct: float,
) -> list[float]:
    """Výsledky šablony pro bránu: jen setupy, které by se daly zobchodovat
    (stop v rozpočtu). Řádky před pravidly se dopočítají z entry/stop a hodnoty
    bodu symbolu; bez známé hodnoty bodu se řádek vynechá (nic se nevymýšlí)."""
    results: list[float] = []
    for row in realized:
        if row.template != template or row.closed_ts < since:
            continue
        affordable = row.affordable
        if affordable is None:
            point_value = point_values.get(row.symbol)
            if point_value is None:
                continue
            affordable = position_size(
                row.entry,
                row.stop,
                point_value,
                account_equity_usd=account_equity_usd,
                risk_pct=risk_pct,
                risk_max_pct=risk_max_pct,
            ).affordable
        if affordable:
            results.append(row.outcome_r)
    return results


def template_gate(results: Sequence[float], *, min_samples: int, enabled: bool) -> GateResult:
    """Brána šablony: pass jen s kladnou dolní mezí očekávání při n ≥ min_samples."""
    lb = expectancy_lower_bound(results)
    if not enabled:
        return GateResult("off", len(results), lb)
    if len(results) < min_samples or lb is None:
        return GateResult("insufficient", len(results), lb)
    return GateResult("pass" if lb > 0 else "block", len(results), lb)
