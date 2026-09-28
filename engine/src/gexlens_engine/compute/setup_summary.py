"""Souhrn setupů z CELÉ historie (#1319) — čisté funkce bez I/O.

Souhrn dřív počítal prohlížeč nad stránkou tabulky `GET /setups/{symbol}`
(posledních 200 setupů podle vzniku), takže „celkem" bylo klouzavé okno:
ES v5 ukazovalo +690 $ na 1 kontrakt, celá historie −2 338 $. Teď se počítá
tady nad všemi řádky vybraných symbolů a UI jen vykresluje.

Konvence (převzaté z dosavadního UI, sémantika výsledků se nemění):

- **uzavřený** = status ≠ active s `outcome_r`; **výhra** = `outcome_r > 0`
  (i kladný timeout), prohra = zbytek;
- **1 kontrakt**: P/L = R × |entry − stop| × hodnota bodu (`paper.POINT_VALUES`),
  poplatky = počet uzavřených × poplatek za kontrakt a obchod (parametry setupů);
  hrubý − poplatky = čistý. Každý setup rovným dílem 1 kontrakt, i stínový;
- **obchodovatelné / stínové / bez pravidel** podle `context.tradeable` (#1185):
  stín se měří, ale neobchoduje; řádky před pravidly risk kontext nemají;
- **účet** = jen obchodovatelné, kontrakty ze serverového sizingu:
  R × `max_loss_usd` − `fee_usd`, drawdown chronologicky podle uzavření;
- **den** = obchodní seance (`settle.trading_session_date`): uzavřený patří do
  seance uzavření, aktivní do seance vzniku;
- **mechanika**: výchozí jen aktuální verze (#311), `all_versions` přidá starší.
"""

import datetime as dt
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.ticker import symbol_root

RiskGroup = Literal["tradeable", "shadow", "unruled"]

#: Třídy polohy a verdikty stínové brány (#1060) — jiné hodnoty = řádek bránu nenese
BAND_CLASSES = frozenset({"inside", "transition", "outside", "no_zone"})
BAND_GATES = frozenset({"pass", "block", "unknown"})

#: Mikro protějšek plného kontraktu pro USD simulaci (#679) — zrcadlo
#: `MICRO_SYMBOLS` kalkulačky pozice ve frontendu (instrument/position.ts)
MICRO_SYMBOLS: dict[str, str] = {"ES": "MES", "NQ": "MNQ", "RTY": "M2K", "YM": "MYM"}
#: Náklad round-trip na 1 mikro kontrakt (ADR-0030): komise 2 × 0,62 $ + 1 tick
#: slippage na stranu; konstanty do doby, než je nahradí měřená exekuce
ROUND_TRIP_COST_USD: dict[str, float] = {"MES": 2.49, "MNQ": 1.74}
DEFAULT_ROUND_TRIP_COST_USD = 2.5


@dataclass(frozen=True)
class SetupFact:
    """Setup zúžený na to, co souhrn čte (podmnožina řádku `setups`)."""

    id: int
    symbol: str
    template: str
    status: str
    created_ts: dt.datetime
    closed_ts: dt.datetime | None
    outcome_r: float | None
    entry: float
    stop: float
    mechanics_version: int
    #: `context.tradeable`; None = řádek vznikl před risk pravidly (#1185)
    tradeable: bool | None = None
    trade_block: str | None = None
    max_loss_usd: float = 0.0
    fee_usd: float = 0.0
    gex_regime: str | None = None
    #: Verdikty stínové brány polohy (#1060); None = řádek bránu nenese
    band_gate_simple: str | None = None
    band_gate_regime: str | None = None

    @property
    def is_closed(self) -> bool:
        return self.status != "active" and self.outcome_r is not None

    @property
    def stop_points(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def risk_group(self) -> RiskGroup:
        if self.tradeable is None:
            return "unruled"
        return "tradeable" if self.tradeable else "shadow"


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def fact_from_record(record: Mapping[str, Any]) -> SetupFact:
    """Řádek tabulky `setups` → SetupFact; výklad `context` na jediném místě.

    Risk kontext platí jen s `tradeable` (bool) a `contracts` (číslo) — stejná
    podmínka jako `riskInfo` ve frontendu; brána polohy jen s platnou třídou
    a oběma verdikty (`bandInfo`). Naivní čas (sqlite) je UTC.
    """
    raw_context = record.get("context")
    context: Mapping[str, Any] = raw_context if isinstance(raw_context, Mapping) else {}
    tradeable = context.get("tradeable")
    contracts = context.get("contracts")
    ruled = isinstance(tradeable, bool) and isinstance(contracts, int | float)
    block = context.get("trade_block")
    regime = context.get("gex_regime")
    gate_simple = context.get("band_gate_simple")
    gate_regime = context.get("band_gate_regime")
    has_gate = (
        context.get("band_class") in BAND_CLASSES
        and gate_simple in BAND_GATES
        and gate_regime in BAND_GATES
    )
    closed_ts = record.get("closed_ts")
    outcome = record.get("outcome_r")
    return SetupFact(
        id=int(record["id"]),
        symbol=str(record["symbol"]),
        template=str(record["template"]),
        status=str(record["status"]),
        created_ts=_utc(record["created_ts"]),
        closed_ts=_utc(closed_ts) if isinstance(closed_ts, dt.datetime) else None,
        outcome_r=float(outcome) if outcome is not None else None,
        entry=float(record["entry"]),
        stop=float(record["stop"]),
        mechanics_version=int(record.get("mechanics_version") or 1),
        tradeable=bool(tradeable) if ruled else None,
        trade_block=str(block) if ruled and isinstance(block, str) else None,
        max_loss_usd=_number(context.get("max_loss_usd")) if ruled else 0.0,
        fee_usd=_number(context.get("fee_usd")) if ruled else 0.0,
        gex_regime=regime if isinstance(regime, str) else None,
        band_gate_simple=str(gate_simple) if has_gate else None,
        band_gate_regime=str(gate_regime) if has_gate else None,
    )


@dataclass(frozen=True)
class Ev:
    """Expected Value na obchod (#911): WinRate × AvgWin − LossRate × AvgLoss.

    Číselně ≡ průměr výsledků; hodnota je v rozkladu (co výsledek táhne).
    `avg_loss` je kladné číslo (vzorec ho odečítá)."""

    ev: float
    win_rate: float
    loss_rate: float
    avg_win: float
    avg_loss: float
    n: int


def expected_value(values: Sequence[float]) -> Ev | None:
    if not values:
        return None
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    n = len(values)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    return Ev(
        ev=win_rate * avg_win - loss_rate * avg_loss,
        win_rate=win_rate,
        loss_rate=loss_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        n=n,
    )


@dataclass(frozen=True)
class GroupStats:
    """Bilance skupiny setupů na 1 kontrakt (hrubě, poplatky, čistě) a v R."""

    count: int
    active: int
    closed: int
    wins: int
    losses: int
    #: 0–1; None = nic uzavřeného (ne „0 %")
    win_rate: float | None
    sum_r: float
    avg_r: float | None
    gross_usd: float
    fees_usd: float
    net_usd: float
    ev_r: Ev | None
    #: EV hrubě na 1 kontrakt (před poplatky)
    ev_usd: Ev | None


@dataclass(frozen=True)
class AccountStats:
    """Co by vydělal účet: jen obchodovatelné, kontrakty ze serverového sizingu."""

    trades: int
    gross_usd: float
    fees_usd: float
    net_usd: float
    #: Čistý výsledek v % startovního účtu
    net_pct: float
    #: Nejhlubší propad čisté equity od vrcholu (≤ 0), chronologicky podle uzavření
    max_drawdown_usd: float


@dataclass(frozen=True)
class DayStats:
    """Bilance dnešní obchodní seance (#748) na 1 kontrakt + účet dne."""

    session: str
    #: Uzavřené dnes + aktivní vzniklé dnes
    trades: int
    closed: int
    active: int
    wins: int
    losses: int
    win_rate: float | None
    best_usd: float | None
    worst_usd: float | None
    gross_usd: float
    fees_usd: float
    net_usd: float
    #: Hrubý výsledek dne v % účtu
    gross_pct: float
    #: Největší riziko v jednom obchodě / součet rizik (% účtu, i aktivní)
    max_risk_pct: float
    total_risk_pct: float
    account: AccountStats | None


@dataclass(frozen=True)
class GateBucket:
    n: int
    avg_r: float
    win_rate: float


@dataclass(frozen=True)
class RegimeRow:
    """Úspěšnost šablony v gamma režimu vzniku (#402): cíl vs. stop, bez timeoutů."""

    template: str
    regime: str
    n: int
    wins: int
    win_rate: float


@dataclass(frozen=True)
class DailyPoint:
    session: str
    trades: int
    sum_r: float
    cum_r: float


@dataclass(frozen=True)
class Sharpe:
    """Anualizovaný Sharpe z denní řady: mean / std(ddof=1) × √252 (ADR-0030)."""

    sharpe: float | None
    days: int


@dataclass(frozen=True)
class SimulationInput:
    """Účet a % rizika z kalkulačky pozice (#679, Settings → Trading)."""

    account_usd: float
    risk_pct: float


@dataclass(frozen=True)
class UsdSimulation:
    """Exekuce mikro kontrakty dle kalkulačky #679 včetně nákladů."""

    traded: int
    #: Obchody s 0 kontrakty (stop dražší než rozpočet) — realita malého účtu
    skipped: int
    total_usd: float
    sharpe: Sharpe


@dataclass(frozen=True)
class Performance:
    """Výkon (#794 fáze 0, ADR-0030): denní ΣR per seance, Sharpe, drawdown."""

    daily: list[DailyPoint]
    sharpe_all: Sharpe
    sharpe_30: Sharpe
    #: Nejhlubší propad kumulativní denní ΣR od vrcholu (≤ 0)
    max_drawdown_r: float
    simulation: UsdSimulation | None


@dataclass(frozen=True)
class SetupSummary:
    mechanics_version: int
    all_versions: bool
    #: Všechny řádky vybraných symbolů (všechny verze a stavy)
    total_count: int
    #: Řádky jiné verze mechaniky než aktuální (přepínač „Včetně starší mechaniky")
    legacy_count: int
    fee_per_contract_usd: float
    account_usd: float
    #: Symboly bez známé hodnoty bodu — jejich USD se nepočítají (nic se nevymýšlí)
    unpriced_symbols: list[str]
    all: GroupStats
    tradeable: GroupStats
    shadow: GroupStats
    unruled: GroupStats
    #: Počty stínových setupů (aktivní i uzavřené) podle `trade_block`
    shadow_reasons: dict[str, int]
    #: None = žádný setup v rozsahu nenese risk kontext
    account: AccountStats | None
    today: DayStats
    #: {"simple"|"regime": {"pass"|"block": GateBucket}}; None = žádná brána
    band_gates: dict[str, dict[str, GateBucket]] | None
    regimes: list[RegimeRow]
    performance: Performance


def _point_value(fact: SetupFact, point_values: Mapping[str, float]) -> float | None:
    return point_values.get(symbol_root(fact.symbol))


def _usd(fact: SetupFact, point_values: Mapping[str, float]) -> float | None:
    """P/L uzavřeného setupu na 1 kontrakt; None = otevřený nebo neznámý bod."""
    point = _point_value(fact, point_values)
    if not fact.is_closed or point is None or fact.outcome_r is None:
        return None
    return fact.outcome_r * fact.stop_points * point


def _closed_chronological(facts: Iterable[SetupFact]) -> list[SetupFact]:
    closed = [fact for fact in facts if fact.is_closed and fact.closed_ts is not None]
    return sorted(closed, key=lambda fact: (fact.closed_ts, fact.id))


def group_stats(
    facts: Sequence[SetupFact], point_values: Mapping[str, float], fee_per_contract_usd: float
) -> GroupStats:
    closed = [fact for fact in facts if fact.is_closed]
    results = [fact.outcome_r for fact in closed if fact.outcome_r is not None]
    pnls = [usd for usd in (_usd(fact, point_values) for fact in closed) if usd is not None]
    wins = sum(1 for value in results if value > 0)
    gross = sum(pnls)
    fees = len(pnls) * fee_per_contract_usd
    return GroupStats(
        count=len(facts),
        active=sum(1 for fact in facts if fact.status == "active"),
        closed=len(closed),
        wins=wins,
        losses=len(closed) - wins,
        win_rate=wins / len(closed) if closed else None,
        sum_r=sum(results),
        avg_r=sum(results) / len(results) if results else None,
        gross_usd=gross,
        fees_usd=fees,
        net_usd=gross - fees,
        ev_r=expected_value(results),
        ev_usd=expected_value(pnls),
    )


def account_stats(facts: Sequence[SetupFact], account_usd: float) -> AccountStats | None:
    """Bilance účtu z obchodovatelných uzavřených (#1185); None bez risk kontextu."""
    if not any(fact.tradeable is not None for fact in facts):
        return None
    equity = peak = worst = gross = fees = 0.0
    trades = 0
    for fact in _closed_chronological(fact for fact in facts if fact.tradeable is True):
        assert fact.outcome_r is not None  # _closed_chronological bere jen uzavřené s R
        pnl = fact.outcome_r * fact.max_loss_usd
        trades += 1
        gross += pnl
        fees += fact.fee_usd
        equity += pnl - fact.fee_usd
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return AccountStats(
        trades=trades,
        gross_usd=gross,
        fees_usd=fees,
        net_usd=equity,
        net_pct=equity / account_usd * 100.0 if account_usd > 0 else 0.0,
        max_drawdown_usd=worst,
    )


def day_stats(
    facts: Sequence[SetupFact],
    session_day: dt.date,
    *,
    point_values: Mapping[str, float],
    fee_per_contract_usd: float,
    account_usd: float,
) -> DayStats:
    """Seance `session_day`: uzavřené podle `closed_ts`, aktivní podle vzniku (#748)."""

    def stamp(fact: SetupFact) -> dt.datetime:
        if fact.status == "active" or fact.closed_ts is None:
            return fact.created_ts
        return fact.closed_ts

    today = [fact for fact in facts if trading_session_date(stamp(fact)) == session_day]
    closed = [fact for fact in today if fact.is_closed]
    pnls = [usd for usd in (_usd(fact, point_values) for fact in closed) if usd is not None]
    wins = sum(1 for fact in closed if (fact.outcome_r or 0.0) > 0)
    risks = [
        fact.stop_points * point
        for fact in today
        if (point := _point_value(fact, point_values)) is not None
    ]
    gross = sum(pnls)
    fees = len(pnls) * fee_per_contract_usd
    base = account_usd if account_usd > 0 else math.inf
    return DayStats(
        session=session_day.isoformat(),
        trades=len(today),
        closed=len(closed),
        active=len(today) - len(closed),
        wins=wins,
        losses=len(closed) - wins,
        win_rate=wins / len(closed) if closed else None,
        best_usd=max(pnls) if pnls else None,
        worst_usd=min(pnls) if pnls else None,
        gross_usd=gross,
        fees_usd=fees,
        net_usd=gross - fees,
        gross_pct=gross / base * 100.0,
        max_risk_pct=(max(risks) / base * 100.0) if risks else 0.0,
        total_risk_pct=sum(risks) / base * 100.0,
        account=account_stats(today, account_usd),
    )


def band_gate_stats(facts: Sequence[SetupFact]) -> dict[str, dict[str, GateBucket]] | None:
    """Rozpad uzavřených setupů podle verdiktu obou stínových pravidel (#1060).

    Verdikt `unknown` nevstupuje do žádné skupiny; None = žádný uzavřený setup
    bránu nenese."""
    gated = [fact for fact in facts if fact.is_closed and fact.band_gate_simple is not None]
    if not gated:
        return None

    def bucket(rows: list[SetupFact]) -> GateBucket:
        results = [fact.outcome_r or 0.0 for fact in rows]
        n = len(results)
        return GateBucket(
            n=n,
            avg_r=sum(results) / n if n else 0.0,
            win_rate=sum(1 for value in results if value > 0) / n if n else 0.0,
        )

    def split(verdict_of: str) -> dict[str, GateBucket]:
        return {
            verdict: bucket([fact for fact in gated if getattr(fact, verdict_of) == verdict])
            for verdict in ("pass", "block")
        }

    return {"simple": split("band_gate_simple"), "regime": split("band_gate_regime")}


def regime_rows(facts: Sequence[SetupFact]) -> list[RegimeRow]:
    """Úspěšnost šablona × gamma režim (#402): jen cíl/stop, režim bez kontextu „neznámý"."""
    counts: dict[tuple[str, str], list[int]] = {}
    for fact in facts:
        if fact.status not in ("closed_target", "closed_stop"):
            continue
        key = (fact.template, fact.gex_regime or "neznámý")
        entry = counts.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += 1 if fact.status == "closed_target" else 0
    return [
        RegimeRow(template=template, regime=regime, n=n, wins=wins, win_rate=wins / n)
        for (template, regime), (n, wins) in sorted(counts.items())
    ]


def annualized_sharpe(values: Sequence[float]) -> Sharpe:
    days = len(values)
    if days < 2:
        return Sharpe(sharpe=None, days=days)
    mean = sum(values) / days
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / (days - 1))
    if std == 0:
        return Sharpe(sharpe=None, days=days)
    return Sharpe(sharpe=mean / std * math.sqrt(252), days=days)


def _daily_sums(items: Iterable[tuple[str, float]]) -> list[tuple[str, float, int]]:
    """Σ hodnot per seance, chronologicky. Seance bez obchodu se NEpřidávají —
    nula by uměle snižovala volatilitu (ADR-0030: absence pozorování ≠ výnos 0)."""
    sums: dict[str, tuple[float, int]] = {}
    for session, value in items:
        total, count = sums.get(session, (0.0, 0))
        sums[session] = (total + value, count + 1)
    return [(session, total, count) for session, (total, count) in sorted(sums.items())]


def usd_simulation(
    closed: Sequence[SetupFact], simulation: SimulationInput, point_values: Mapping[str, float]
) -> UsdSimulation:
    """Mikro sizing #679: kontrakty = ⌊účet × riziko % / (stop b × bod mikra)⌋,
    P/L = kontrakty × (R × stop × bod − round-trip náklad); 0 kontraktů = přeskočeno."""
    risk_usd = simulation.account_usd * simulation.risk_pct / 100.0
    skipped = 0
    per_trade: list[tuple[str, float]] = []
    for fact in closed:
        root = symbol_root(fact.symbol)
        micro = MICRO_SYMBOLS.get(root, root)
        point = point_values.get(micro)
        if point is None or fact.stop_points <= 0 or fact.outcome_r is None:
            skipped += 1
            continue
        contracts = math.floor(risk_usd / (fact.stop_points * point))
        if contracts <= 0:
            skipped += 1
            continue
        cost = ROUND_TRIP_COST_USD.get(micro, DEFAULT_ROUND_TRIP_COST_USD)
        pnl = contracts * fact.outcome_r * fact.stop_points * point - contracts * cost
        assert fact.closed_ts is not None
        per_trade.append((trading_session_date(fact.closed_ts).isoformat(), pnl))
    daily = _daily_sums(per_trade)
    return UsdSimulation(
        traded=len(per_trade),
        skipped=skipped,
        total_usd=sum(value for _, value, _ in daily),
        sharpe=annualized_sharpe([value for _, value, _ in daily]),
    )


def performance(
    facts: Sequence[SetupFact],
    *,
    point_values: Mapping[str, float],
    simulation: SimulationInput | None,
) -> Performance:
    closed = _closed_chronological(facts)
    sums = _daily_sums(
        (trading_session_date(fact.closed_ts).isoformat(), fact.outcome_r or 0.0)
        for fact in closed
        if fact.closed_ts is not None
    )
    daily: list[DailyPoint] = []
    equity = peak = worst = 0.0
    for session, total, trades in sums:
        equity += total
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
        daily.append(DailyPoint(session=session, trades=trades, sum_r=total, cum_r=equity))
    values = [point.sum_r for point in daily]
    return Performance(
        daily=daily,
        sharpe_all=annualized_sharpe(values),
        sharpe_30=annualized_sharpe(values[-30:]),
        max_drawdown_r=worst,
        simulation=(
            usd_simulation(closed, simulation, point_values)
            if simulation is not None and simulation.account_usd > 0 and simulation.risk_pct > 0
            else None
        ),
    )


def summarize_setups(
    facts: Sequence[SetupFact],
    *,
    mechanics_version: int,
    all_versions: bool,
    point_values: Mapping[str, float],
    fee_per_contract_usd: float,
    account_usd: float,
    session_day: dt.date,
    simulation: SimulationInput | None = None,
) -> SetupSummary:
    """Souhrn setupů `facts` (celá historie vybraných symbolů) pro UI.

    `mechanics_version` = aktuální verze detektoru; bez `all_versions` se
    počítá jen ona, starší řádky se jen spočítají do `legacy_count`.
    """
    scope = [fact for fact in facts if all_versions or fact.mechanics_version == mechanics_version]
    by_group: dict[RiskGroup, list[SetupFact]] = {"tradeable": [], "shadow": [], "unruled": []}
    for fact in scope:
        by_group[fact.risk_group].append(fact)

    def stats(rows: Sequence[SetupFact]) -> GroupStats:
        return group_stats(rows, point_values, fee_per_contract_usd)

    return SetupSummary(
        mechanics_version=mechanics_version,
        all_versions=all_versions,
        total_count=len(facts),
        legacy_count=sum(1 for fact in facts if fact.mechanics_version != mechanics_version),
        fee_per_contract_usd=fee_per_contract_usd,
        account_usd=account_usd,
        unpriced_symbols=sorted(
            {fact.symbol for fact in scope if _point_value(fact, point_values) is None}
        ),
        all=stats(scope),
        tradeable=stats(by_group["tradeable"]),
        shadow=stats(by_group["shadow"]),
        unruled=stats(by_group["unruled"]),
        shadow_reasons=dict(
            sorted(Counter(fact.trade_block or "unknown" for fact in by_group["shadow"]).items())
        ),
        account=account_stats(scope, account_usd),
        today=day_stats(
            scope,
            session_day,
            point_values=point_values,
            fee_per_contract_usd=fee_per_contract_usd,
            account_usd=account_usd,
        ),
        band_gates=band_gate_stats(scope),
        regimes=regime_rows(scope),
        performance=performance(scope, point_values=point_values, simulation=simulation),
    )
