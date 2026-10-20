"""Trade větev CumΔ (ADR-0057 body 3 a 6, #1460) — trasy tisků a stav běhu.

Tisk dxFeed `TimeAndSale` patří kontraktu aktivního nebo sekundárního řetězu
jednoho instrumentu. Trasy (streamer symbol → spec) staví smyčka plánu
subskripcí z kontraktů obou runtime; tisk jde do trackeru toho řetězu.

Trade větev řetězu „běží" = připojený stream ∧ trasy doručují tisky do jeho
trackeru ∧ univerzum nemlčí déle než práh hlídače ticha (`SILENT_RTH_S` /
`SILENT_GLOBEX_S`, bez nové konstanty). Pod tisky trasy nesou tracker svého
řetězu (#1460, varianta C): roll v settle předá tracker sekundáru D+1 novému
aktivnímu runtime, takže mu sekundární část tras patří i před obnovou plánu
a tisky D+1 tečou bez přerušení i mezi zastavením staré a založením nové
pipeline.

Orchestrátor z toho každou minutu dává trackerům vzorek (`observe`) a v témž
okamžiku řeže jejich tisky (`seal`): interval řádku leží mezi dvěma vzorky,
takže oba jeho konce pokrývá vzorek. Verdikt minuty ze vzorků zafixuje
`begin_cycle` v `run_cycle` (`compute/cumdelta.py`).

Zdroj `midpoint` (mechanika v5) verdikt nečte a tisky jen měří pokrytí:
trasy aktivní části doručují jako dřív i po rollu, sekundární část tisky
nedoručuje (jako před #1460 — net sekundáru dává bar větev).
"""

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.marketclock import is_market_closed, outside_us_rth
from gexlens_engine.compute.settle import session_bounds, trading_session_date
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.tasty.watchdog import SILENT_GLOBEX_S, SILENT_RTH_S

#: Stav trade větve aktivního řetězu v `/status` (ADR-0057 bod 10, jen API):
#: běží · mezera (otevřený trh, větev neběží) · trh zavřený · bez tasty větve.
#: Pod midpointem (v5) CumΔ neovlivňuje, jen ukazuje, co by v6 měřila.
BRANCH_RUNNING = "running"
BRANCH_GAP = "gap"
BRANCH_CLOSED = "closed"
BRANCH_OFF = "off"


class RuntimeLike(Protocol):
    @property
    def expiry(self) -> str: ...

    @property
    def contracts(self) -> Sequence[OptionContractSpec]: ...

    @property
    def cum_delta(self) -> CumDeltaTracker | None: ...


class PipelineLike(Protocol):
    @property
    def runtime(self) -> RuntimeLike: ...

    @property
    def next_runtime(self) -> RuntimeLike | None: ...


def pending_handovers[RuntimeT: RuntimeLike](
    pipes: Mapping[str, PipelineLike], handovers: Mapping[str, RuntimeT]
) -> dict[str, RuntimeT]:
    """Sekundáry, jejichž tracker po rollu v settle čeká na novou pipeline (#1460, varianta C).

    Symbol bez běžící pipeline s předáním v rollu: jeho řetěz D+1 dál drží
    subskripci tisků (účel chain) i trasy, které tisky doručí do čekajícího
    trackeru. Symbol, který pipeline už má, nečeká — drží ho pipeline. Bez
    předání se tisky symbolu bez pipeline nedoručují nikam (jako před #1460).
    """
    return {symbol: runtime for symbol, runtime in handovers.items() if symbol not in pipes}


@dataclass(frozen=True)
class TradeRoutes:
    """Trasy tisků jednoho instrumentu: streamer symbol → spec, zvlášť podle řetězu.

    Expirace u každé části říká, kterému řetězu trasy patří — po rollu je aktivní
    část vypršelá a sekundární patří novému aktivnímu řetězu, dokud smyčka plánu
    trasy nepřestaví. Stín `cumdelta_dx` dostává jen aktivní část (ADR-0057 bod 7).
    Tracker části je ten, pro který se trasy stavěly: pod tisky do něj jde tisk
    (`route_trade`) a podle něj se pozná, komu trasy patří (`owned_universe`).
    """

    active_expiry: str
    active: dict[str, OptionContractSpec]
    next_expiry: str | None = None
    next: dict[str, OptionContractSpec] = field(default_factory=dict)
    active_tracker: CumDeltaTracker | None = None
    next_tracker: CumDeltaTracker | None = None


def build_routes(
    pipe: PipelineLike, streamer_of: Callable[[OptionContractSpec], str | None]
) -> TradeRoutes:
    """Trasy z kontraktů aktivního i sekundárního runtime; kontrakt bez streameru vypadne."""

    def part(runtime: RuntimeLike | None) -> dict[str, OptionContractSpec]:
        routes: dict[str, OptionContractSpec] = {}
        if runtime is None:
            return routes
        for spec in runtime.contracts:
            streamer = streamer_of(spec)
            if streamer is not None:
                routes[streamer] = spec
        return routes

    secondary = pipe.next_runtime
    return TradeRoutes(
        active_expiry=pipe.runtime.expiry,
        active=part(pipe.runtime),
        next_expiry=secondary.expiry if secondary is not None else None,
        next=part(secondary),
        active_tracker=pipe.runtime.cum_delta,
        next_tracker=secondary.cum_delta if secondary is not None else None,
    )


def route_trade(
    routes: TradeRoutes, pipe: PipelineLike | None, streamer: str
) -> tuple[CumDeltaTracker, OptionContractSpec] | None:
    """Tracker a kontrakt, kam patří tisk streameru (ADR-0057 bod 6), nebo None.

    Řetěz se určuje podle části tras, nikdy podle expirace kontraktu v okamžiku
    tisku. Zdroj tisků doručuje do trackeru, pro který se část stavěla, ne do
    runtime pipeline (#1460, varianta C). Tracker patří jednomu řetězu, takže
    zastaralé trasy po rollu nedoručí tisk do trackeru jiné expirace (tisky D
    do nového aktivního D+1, tisky D+1 do nového sekundárního D+2). Tisky D+1
    tečou do trackeru, který při rollu převezme nový aktivní runtime, i když
    pipeline symbolu zrovna neběží (`pipe` None). Tracker zastavené pipeline
    dostává tisky naprázdno až do přestavby tras, nikdo ho nečte.

    Midpoint (v5) doručuje jako před #1460: jen aktivní část do aktivního
    trackeru pipeline, i nad zastaralými trasami; sekundární část nedoručuje nikam.
    """
    spec = routes.active.get(streamer)
    if spec is not None:
        tracker = routes.active_tracker
        if tracker is not None and tracker.prints:
            return tracker, spec
        if pipe is None or pipe.runtime.cum_delta is None:
            return None
        return pipe.runtime.cum_delta, spec
    spec = routes.next.get(streamer)
    tracker = routes.next_tracker
    if spec is None or tracker is None or not tracker.prints:
        return None
    return tracker, spec


def owned_universe(
    routes: TradeRoutes | None, runtime: RuntimeLike, index: int
) -> dict[str, OptionContractSpec]:
    """Univerzum tras, které doručují tisky do trackeru runtime; prázdné = trasy mu nepatří.

    Pod tisky rozhoduje identita trackeru jako v `route_trade`: po rollu
    s předáním patří trackeru nového aktivního runtime sekundární část
    zastaralých tras, nový sekundár D+2 do přestavby tras nedostává nic.
    Midpoint (v5) jako před #1460: část podle pozice (`index` 0 = aktivní)
    a expirace musí sedět na běžící řetěz.
    """
    if routes is None:
        return {}
    tracker = runtime.cum_delta
    if tracker is not None and tracker.prints:
        if tracker is routes.active_tracker:
            return routes.active
        if tracker is routes.next_tracker:
            return routes.next
        return {}
    if index == 0:
        return routes.active if runtime.expiry == routes.active_expiry else {}
    return routes.next if runtime.expiry == routes.next_expiry else {}


def trade_branch_running(
    now: dt.datetime,
    *,
    connected: bool,
    owned: bool,
    last_event_at: dt.datetime | None,
) -> bool:
    """Běží trade větev řetězu v okamžiku `now` (ADR-0057 bod 3)?

    Připojený stream ∧ trasy patří běžícímu řetězu ∧ poslední event univerza
    (jakýkoli typ, mezera až když mlčí celé univerzum) není starší než práh
    hlídače ticha — 180 s v US RTH, 600 s v Globexu. Ticho se počítá nejdřív od
    openu seance: v 17:00 CT, v nedělním openu i po svátku by poslední event
    z pauzy, pátku nebo svátku jinak dal mezeru hned v první minutě. Zavřený
    trh řeší volající (`observe(market_closed=…)`), tady se nerozhoduje.
    Vědomý limit (ADR): mlčící stream je do prahu „běžící", minuty jsou tok 0.
    """
    if not (connected and owned):
        return False
    session_open = session_bounds(trading_session_date(now))[0]
    reference = session_open if last_event_at is None else max(last_event_at, session_open)
    limit = SILENT_GLOBEX_S if outside_us_rth(now) else SILENT_RTH_S
    return (now - reference).total_seconds() < limit


class TradeBranchMonitor:
    """Minutové vzorky trade větve pro trackery aktivního i sekundárního řetězu.

    Vzorkuje se každý tracker každou iterací hlavní smyčky (i sekundár s cyklem
    po N minutách — tracker drží AND přes celé své okno) a hned po vzorku se
    jeho tisky řežou (`seal`): řádek M nese tisky (vzorek M−1, vzorek M]. Pád
    spojení mezi vzorky pozná z počítadla reconnectů (roste s každým pádem
    streamu): tisky z té doby chybí, i když stream teď zase běží — okno dostane
    vzorek „neběží" a teprve pak stav v okamžiku vzorku. Řez leží v témž
    okamžiku jako vzorek, takže mezerou je jen řádek s výpadkem, ne i ten
    následující. Globální počítadlo stačí: všechny trackery se řežou najednou.
    """

    def __init__(self) -> None:
        self._reconnects: int | None = None

    def sample(
        self,
        now: dt.datetime,
        pipes: Mapping[str, PipelineLike],
        routes: Mapping[str, TradeRoutes],
        *,
        connected: bool,
        reconnects: int,
        last_event_among: Callable[[Iterable[str]], dt.datetime | None],
    ) -> dict[str, str]:
        """Dá vzorek všem trackerům a vrátí stav trade větve aktivního řetězu per symbol."""
        dropped = self._reconnects is not None and reconnects != self._reconnects
        self._reconnects = reconnects
        market_closed = is_market_closed(now)
        states: dict[str, str] = {}
        for symbol, pipe in pipes.items():
            symbol_routes = routes.get(symbol)
            primary_running = False
            for index, runtime in enumerate((pipe.runtime, pipe.next_runtime)):
                if runtime is None:
                    continue
                universe = owned_universe(symbol_routes, runtime, index)
                owned = bool(universe)
                running = trade_branch_running(
                    now,
                    connected=connected,
                    owned=owned,
                    last_event_at=last_event_among(universe) if owned else None,
                )
                if index == 0:
                    primary_running = running and not dropped
                tracker = runtime.cum_delta
                if tracker is None:
                    continue
                if dropped:
                    tracker.observe(running=False, market_closed=market_closed)
                tracker.observe(running=running, market_closed=market_closed)
                tracker.seal()
            if market_closed:
                states[symbol] = BRANCH_CLOSED
            else:
                states[symbol] = BRANCH_RUNNING if primary_running else BRANCH_GAP
        return states


def observe_without_branch(now: dt.datetime, pipes: Mapping[str, PipelineLike]) -> dict[str, str]:
    """Bez tasty větve trade větev neběží: za otevřeného trhu mezera, za zavřeného nula.

    Řez (`seal`) i tady: bez něj by `begin_cycle` minutu nepotvrdil ani za
    zavřeného trhu.
    """
    market_closed = is_market_closed(now)
    for pipe in pipes.values():
        for runtime in (pipe.runtime, pipe.next_runtime):
            if runtime is not None and runtime.cum_delta is not None:
                runtime.cum_delta.observe(running=False, market_closed=market_closed)
                runtime.cum_delta.seal()
    return dict.fromkeys(pipes, BRANCH_OFF)
