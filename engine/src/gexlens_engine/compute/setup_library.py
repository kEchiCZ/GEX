"""Knihovna setupů (#1323, fáze 1): buňky ticker × šablona — čisté funkce bez I/O.

Buňka nese stádium (Auto / Stín / Zkouška z `SetupParams`), verdikt brány
spočítaný **teď** a důkaz: ØR hrubě a čistě po nákladech ADR-0030, čistý P/L
v reálných mikro dolarech při skutečném sizingu, kolik vzorků buňka potřebuje
na průkaz edge +0,2 R a za kolik seancí je při dnešním tempu nasbírá — nebo
že je okno brány při dnešním tempu nepojme. Verdikt brány počítají tytéž
funkce jako engine
(`risk.affordable_rows` + `risk.template_gate`, okno `risk.gate_window_start`)
— Knihovna je druhý volající, ne druhá implementace. Poslední uložený verdikt
by u vzácných šablon klamal stářím (ES gamma_momentum měl 1. 10. poslední
verdikt z 25. 9.).

Vstupem jsou `SetupFact` ze souhrnu (#1319); bere se jen aktuální mechanika
a setupy mimo statistiky (`SetupFact.in_stats`: vznik po settle vlastní
expirace #1324, značka `context.excluded` #1346) se vyřadí — stejně jako
čtení brzd a brány v enginu (`setups_store.realized_since`).

Hlavička Knihovny ukazuje stav brzd účtu (`library_brakes`) — tatáž
`risk.brake_state` nad týmž čtením `realized_since` jako engine; brzdy jsou
napříč symboly, takže nezávisí na tom, které tickery souhrn zobrazuje.
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gexlens_engine.compute.risk import (
    GateVerdict,
    RealizedSetup,
    TradeBlock,
    affordable_rows,
    brake_state,
    gate_window_start,
    position_size,
    samples_needed,
    template_gate,
    trial_usage,
)
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.compute.setup_summary import SetupFact, net_r, net_usd_micro
from gexlens_engine.compute.setups import (
    TEMPLATE_REGISTRY,
    SetupParams,
    UserStage,
    cell_key,
)
from gexlens_engine.ticker import symbol_root


@dataclass(frozen=True)
class CellEvidence:
    """Důkaz buňky nad vzorkem brány (setupy se stopem v rozpočtu v okně)."""

    avg_r: float | None
    #: Po nákladech ADR-0030 na 1 mikro (`setup_summary.net_r`); None, když
    #: některý řádek vzorku nejde ocenit (průměr by byl z jiného vzorku)
    avg_net_r: float | None
    #: Vzorek pro průkaz edge +0,2 R (`risk.samples_needed`); None, když ho
    #: malý vzorek neumí odhadnout (pod 2 vzorky, nulové σ pod minimem brány)
    n_needed: int | None


def cell_evidence(
    rows: Sequence[RealizedSetup], point_values: Mapping[str, float], *, min_samples: int
) -> CellEvidence:
    """ØR hrubě/čistě a potřebný vzorek z řádků `risk.affordable_rows` —
    jediný výpočet pro push nového setupu i Knihovnu. `min_samples` = minimum
    brány (`template_gate_min_samples`), pod které n potřebné neklesne."""
    if not rows:
        return CellEvidence(avg_r=None, avg_net_r=None, n_needed=None)
    results = [row.outcome_r for row in rows]
    nets = [
        net_r(row.outcome_r, abs(row.entry - row.stop), row.symbol, point_values) for row in rows
    ]
    priced = [value for value in nets if value is not None]
    return CellEvidence(
        avg_r=sum(results) / len(results),
        avg_net_r=sum(priced) / len(priced) if len(priced) == len(rows) else None,
        n_needed=samples_needed(results, min_samples=min_samples),
    )


def cell_net_usd(
    rows: Sequence[RealizedSetup],
    params: SetupParams,
    *,
    point_value_usd: float,
    point_values: Mapping[str, float],
) -> float | None:
    """Σ čistý P/L vzorku brány v reálných mikro dolarech při skutečném sizingu
    (#1323): kontrakty z kontextu setupu, starší řádky bez nich dopočte
    `position_size` stejně jako brána `affordable` (`point_value_usd` = bod
    plného kontraktu vlastního symbolu). Kladné ØR v R může v dolarech prodělat
    (krátké stopy nesou vyšší náklad a víc kontraktů). None = prázdný vzorek
    nebo řádek, který nejde ocenit (součet by byl z jiného vzorku)."""
    if not rows:
        return None
    total = 0.0
    for row in rows:
        contracts = row.contracts
        if contracts is None:
            contracts = position_size(
                row.entry,
                row.stop,
                point_value_usd=point_value_usd,
                account_equity_usd=params.account_equity_usd,
                risk_pct=params.risk_pct,
                risk_max_pct=params.risk_max_pct,
            ).contracts
        value = net_usd_micro(
            row.outcome_r, abs(row.entry - row.stop), contracts, row.symbol, point_values
        )
        if value is None:
            return None
        total += value
    return total


def realized_from_fact(fact: SetupFact) -> RealizedSetup:
    """Uzavřený SetupFact → vstup brány a čerpání zkoušky (tvar `realized_since`)."""
    assert fact.closed_ts is not None and fact.outcome_r is not None
    return RealizedSetup(
        symbol=fact.symbol,
        template=fact.template,
        status=fact.status,
        outcome_r=fact.outcome_r,
        closed_ts=fact.closed_ts,
        tradeable=fact.tradeable,
        affordable=fact.affordable,
        entry=fact.entry,
        stop=fact.stop,
        created_ts=fact.created_ts,
        gate_overridden=fact.gate_overridden,
        contracts=fact.contracts,
    )


@dataclass(frozen=True)
class TrialState:
    """Nastavená zkouška buňky a její čerpání teď (`risk.trial_usage`)."""

    #: ISO UTC — začátek nastavil server při zahájení nebo obnovení
    started_at: str
    budget_setups: int
    #: Kladná velikost dovolené ztráty (3.0 = konec na Σ R ≤ −3)
    budget_r: float
    #: Mechanika, na které zkouška začala; jiná než aktuální = zkouška skončila
    mechanics_version: int
    #: Setupy s přebitou bránou od začátku, uzavřené i otevřené
    setups: int
    #: Σ R uzavřených z nich
    sum_r: float
    #: Rozpočet vyčerpán (počet nebo ztráta)
    spent: bool


@dataclass(frozen=True)
class LibraryCell:
    """Jedna buňka Knihovny: ticker instance (ADR-0041) × aktivní šablona."""

    #: Klíč `NQ:trend_continuation` — týž jako v `shadow_cells` / `trial_cells`
    cell: str
    ticker: str
    template: str
    #: Číslo šablony z `TEMPLATE_REGISTRY` (T7 → 7)
    template_number: int
    #: Nastavené stádium (co ukazuje dialog „platí")
    stage: UserStage
    #: Co se teď uplatní: vyčerpaná zkouška nebo zkouška jiné mechaniky = auto
    #: (bez zápisu verze)
    effective_stage: UserStage
    trial: TrialState | None
    #: Verdikt brány spočítaný teď týmiž funkcemi jako engine
    gate_verdict: GateVerdict
    gate_n: int
    gate_lb: float | None
    avg_r: float | None
    avg_net_r: float | None
    #: Σ čistě v reálných mikro dolarech při skutečném sizingu (`cell_net_usd`)
    net_usd: float | None
    n_needed: int | None
    #: Seance tickeru v okně brány (dny, kdy engine na tickeru vyráběl setupy)
    sessions: int
    #: Tempo vzorku buňky za seanci (gate_n / sessions)
    per_session: float | None
    #: Kolik vzorků okno brány (`template_gate_days` seancí) při dnešním tempu
    #: pojme — vzorek brány je oknem useknutý a víc nikdy mít nebude
    window_capacity: int | None
    #: Odhad seancí do n potřebné; 0 = vzorek už stačí, None = nejde odhadnout
    #: nebo ho okno nepojme (`window_capacity` < `n_needed`)
    sessions_to_decision: int | None


def _sort_key(cell: LibraryCell) -> tuple[int, float, str, int]:
    """Podle průkaznosti (n / n potřebné) sestupně — malý vzorek s vysokým ØR
    nesmí svádět; buňky bez odhadu na konec."""
    if cell.n_needed is None:
        return (1, 0.0, cell.ticker, cell.template_number)
    return (0, -cell.gate_n / cell.n_needed, cell.ticker, cell.template_number)


def configured_tickers(params: SetupParams) -> set[str]:
    """Tickery s nastaveným stádiem (Stín nebo Zkouška) — Knihovna je ukáže
    vždy, i mimo watchlist (pinovaný `NQZ6`), ať jde zkoušku vidět a vrátit."""
    return {trial.ticker for trial in params.trial_cells} | {
        cell.partition(":")[0] for cell in params.shadow_cells
    }


def library_cells(
    facts: Sequence[SetupFact],
    params: SetupParams,
    *,
    symbols: Sequence[str],
    now: dt.datetime,
    mechanics_version: int,
    point_values: Mapping[str, float],
) -> list[LibraryCell]:
    """Buňky Knihovny (#1323), řazené podle průkaznosti.

    Tickery: ty z `symbols`, které mají setup vzniklý v okně brány, plus vždy
    ty s nastaveným stádiem (`configured_tickers`) — i mimo `symbols`, jinak
    by běžící zkouška pinovaného kontraktu mimo watchlist v Knihovně chyběla
    a nešla vrátit. `facts` proto musí nést i jejich řádky. Šablony: aktivní
    šablony `TEMPLATE_REGISTRY` bez `disabled_templates`. Hodnota bodu plného
    kontraktu pro dopočet starších řádků brány je `point_values[kořen tickeru]`
    (engine bere `runtime.multiplier`, pro ES a NQ totéž); neznámý symbol
    starší řádky do brány nepočítá.
    """
    scope = [
        fact for fact in facts if fact.mechanics_version == mechanics_version and fact.in_stats
    ]
    since = gate_window_start(now, params.template_gate_days)
    wanted = set(symbols)
    recent = [fact for fact in scope if fact.created_ts >= since]
    tickers = sorted(({fact.symbol for fact in recent} & wanted) | configured_tickers(params))
    templates = [
        (slot.number, slot.template.value)
        for slot in TEMPLATE_REGISTRY
        if slot.template is not None and slot.template.value not in params.disabled_templates
    ]
    realized = [
        realized_from_fact(fact)
        for fact in scope
        if fact.is_closed and fact.closed_ts is not None and fact.outcome_r is not None
    ]
    cells: list[LibraryCell] = []
    for ticker in tickers:
        sessions = len(
            {trading_session_date(fact.created_ts) for fact in recent if fact.symbol == ticker}
        )
        point_value = point_values.get(symbol_root(ticker), 0.0)
        for number, template in templates:
            rows = affordable_rows(
                realized,
                template,
                ticker,
                since=since,
                point_value_usd=point_value,
                account_equity_usd=params.account_equity_usd,
                risk_pct=params.risk_pct,
                risk_max_pct=params.risk_max_pct,
            )
            results = [row.outcome_r for row in rows]
            gate = template_gate(
                results,
                min_samples=params.template_gate_min_samples,
                enabled=params.template_gate_enabled,
            )
            evidence = cell_evidence(
                rows, point_values, min_samples=params.template_gate_min_samples
            )
            stage = params.stage_of(ticker, template)
            trial_state: TrialState | None = None
            effective: UserStage = stage
            trial = params.trial_of(ticker, template)
            if trial is not None:
                open_setups = sum(
                    1
                    for fact in scope
                    if fact.status == "active"
                    and fact.symbol == ticker
                    and fact.template == template
                    and fact.gate_overridden
                    and fact.created_ts >= trial.started_at
                )
                usage = trial_usage(
                    realized,
                    ticker,
                    template,
                    started_at=trial.started_at,
                    budget_setups=trial.budget_setups,
                    budget_r=trial.budget_r,
                    open_setups=open_setups,
                )
                trial_state = TrialState(
                    started_at=trial.started_at.astimezone(dt.UTC).isoformat(),
                    budget_setups=trial.budget_setups,
                    budget_r=trial.budget_r,
                    mechanics_version=trial.mechanics_version,
                    setups=usage.setups,
                    sum_r=usage.sum_r,
                    spent=usage.spent,
                )
                # Táž podmínka jako engine (`_risk_context`): zkouška platí od
                # začátku, na své mechanice a do vyčerpání rozpočtu
                if usage.spent or not trial.in_force(now, mechanics_version):
                    effective = "auto"
            per_session = gate.n / sessions if sessions else None
            # Vzorek brány je useknutý oknem: při dnešním tempu pojme nejvýš
            # tempo × N seancí; víc vzorků buňka v bráně mít nebude (#1323)
            capacity = math.floor(per_session * params.template_gate_days) if per_session else None
            to_decision: int | None = None
            if evidence.n_needed is not None and per_session:
                missing = evidence.n_needed - gate.n
                if missing <= 0:
                    to_decision = 0
                elif capacity is None or capacity >= evidence.n_needed:
                    to_decision = math.ceil(missing / per_session)
            cells.append(
                LibraryCell(
                    cell=cell_key(ticker, template),
                    ticker=ticker,
                    template=template,
                    template_number=number,
                    stage=stage,
                    effective_stage=effective,
                    trial=trial_state,
                    gate_verdict=gate.verdict,
                    gate_n=gate.n,
                    gate_lb=gate.lower_bound,
                    avg_r=evidence.avg_r,
                    avg_net_r=evidence.avg_net_r,
                    net_usd=cell_net_usd(
                        rows, params, point_value_usd=point_value, point_values=point_values
                    ),
                    n_needed=evidence.n_needed,
                    sessions=sessions,
                    per_session=per_session,
                    window_capacity=capacity,
                    sessions_to_decision=to_decision,
                )
            )
    return sorted(cells, key=_sort_key)


@dataclass(frozen=True)
class LibraryBrakes:
    """Stav brzd účtu teď (#1323, hlavička Knihovny) — `risk.brake_state`
    nad uzavřenými setupy napříč symboly, stejně jako engine u kandidáta."""

    #: Obchodní den seance (ISO), ke kterému čísla patří
    session: str
    #: Σ R obchodovatelných uzavřených v seanci / v obchodním týdnu
    day_r: float
    week_r: float
    #: Limity z platné verze parametrů (0 = brzda vypnutá)
    daily_brake_r: float
    weekly_brake_r: float
    max_template_stops_per_day: int
    #: `daily_brake` / `weekly_brake` = teď zastaví všechny obchodovatelné setupy
    block: TradeBlock | None
    #: Stopy šablony za seanci napříč symboly (jen šablony s ≥ 1 stopem);
    #: na stropu je šablona do konce seance stínová
    template_stops: dict[str, int]


def library_brakes(
    realized: Sequence[RealizedSetup], params: SetupParams, *, now: dt.datetime
) -> LibraryBrakes:
    """Brzdy účtu pro hlavičku Knihovny (#1323) — druhý volající `brake_state`.

    `realized` = `setups_repository.realized_since(week_start(seance), …)`
    aktuální mechaniky, tedy totéž čtení jako `SetupEngine._load_realized`
    (širší okno nevadí, den a týden si vymezí `brake_state`)."""
    session_day = trading_session_date(now)
    account = brake_state(
        realized,
        "",
        session_day=session_day,
        daily_brake_r=params.daily_brake_r,
        weekly_brake_r=params.weekly_brake_r,
        max_template_stops_per_day=0,
    )
    stops: dict[str, int] = {}
    for slot in TEMPLATE_REGISTRY:
        if slot.template is None:
            continue
        count = brake_state(
            realized,
            slot.template.value,
            session_day=session_day,
            daily_brake_r=0.0,
            weekly_brake_r=0.0,
            max_template_stops_per_day=0,
        ).template_stops
        if count:
            stops[slot.template.value] = count
    return LibraryBrakes(
        session=session_day.isoformat(),
        day_r=account.day_r,
        week_r=account.week_r,
        daily_brake_r=params.daily_brake_r,
        weekly_brake_r=params.weekly_brake_r,
        max_template_stops_per_day=params.max_template_stops_per_day,
        block=account.block,
        template_stops=stops,
    )
