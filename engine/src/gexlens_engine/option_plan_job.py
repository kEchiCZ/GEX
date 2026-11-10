"""Vznik plánu opce QQQ se scénářem dne NQ v 9:15 ET (ADR-0062 K4 C, F2b #1742).

Spojí dva vstupy, které přicházejí nezávisle a v libovolném pořadí:
- auto-scénář NQ po zápisu (`ScenarioGenerator.on_created`): scénář, úrovně NQ téže minuty
  a štítek minuty. 10. 11. měl scénář štítek 14:15:00 UTC a zapsal se ve 14:16:08 UTC;
- ranní snímek řetězu QQQ v 9:15 ET (`PlanChainSnapshotter`), hotový nejpozději 40 s po
  hranici. 10. 11. skončil ve 14:15:00,9 UTC, tedy před scénářem.

Minutový cyklus NQ na nic nečeká (AGENTS.md, #1337, #1646): `on_scenario` jen založí úlohu.
Úloha počká na snímek nejdéle do `snapshot_deadline` (hranice + 40 s + rezerva), čistou
funkcí `build_option_plan` sestaví hlavní plán a oba stíny (K6 B, K10 B), uloží řádek
`option_plans` (K2 A) a pošle do zvonku alert `option_plan_created`. R:R, P3, P4 a rozpočet
se při vzniku nepočítají (K12 A).

Plán stojí na jednom okamžiku (K4 bod 5). Bez opce, s důvodem v `plan.reasons`, vznikne, když:
- ranní snímek je neúplný, nebo nedorazil do limitu;
- scénář nese jiný štítek minuty než hranici snímku (pozdní scénář);
- engine restartoval po vzniku scénáře (`recover` při startu, jen do exitu seance).
Celý plán z 9:31 ET pak sestaví přecenění (F2c #1769).

Parametry účtu (`option_plan_account_usd`, `option_plan_max_loss_usd` v `settings`, K2 A) se
čtou v okamžiku plánu. Jejich hodnoty do logu ani do zprávy nejdou.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from gexlens_engine.compute.option_plan import (
    CHAIN_SOURCE,
    EXIT_BEFORE_SETTLE,
    MAIN_RULES,
    RULES_VERSION,
    SHADOW_B,
    SHADOW_WICK,
    AccountParams,
    OptionPlan,
    PlanRules,
    _cz,
    build_option_plan,
    jsonable,
    plan_to_json,
)
from gexlens_engine.compute.settle import ET_TZ, is_trading_session, settle_ts, trading_session_date
from gexlens_engine.scenario_auto import CreatedScenario
from gexlens_engine.storage.option_plans_store import OptionPlansRepository
from gexlens_engine.tasty.plan_chain import (
    PLAN_SLOT,
    PRODUCT,
    QUOTE_TIMEOUT_S,
    REFERENCE,
    ExpirySnapshot,
    PlanChainSnapshot,
    missing_data_level,
    snapshot_boundary,
)

logger = logging.getLogger(__name__)

#: Klíče parametrů účtu plánu v tabulce `settings` (ADR-0062 K2 A); výchozí hodnota v kódu není
ACCOUNT_KEY = "option_plan_account_usd"
MAX_LOSS_KEY = "option_plan_max_loss_usd"
ACCOUNT_KEYS = (ACCOUNT_KEY, MAX_LOSS_KEY)
#: Rezerva po limitu snímku na zápis jeho partic a naplánování úloh
SNAPSHOT_RESERVE_S = 60.0
#: `option_plans.status` při vzniku
STATUS_BUILT = "sestaven"
STATUS_NO_OPTION = "bez opce"
ALERT_KIND = "option_plan_created"
#: Stíny track recordu (K10 B) v `plan.shadows`; klíč = parametr pravidel
SHADOWS: Mapping[str, PlanRules] = {"entry_b": SHADOW_B, "wick_test": SHADOW_WICK}


def snapshot_deadline(session: dt.date) -> dt.datetime:
    """Do kdy plán čeká na ranní snímek: hranice 9:15 ET + limit snímku 40 s + rezerva."""
    return snapshot_boundary(session, PLAN_SLOT) + dt.timedelta(
        seconds=QUOTE_TIMEOUT_S + SNAPSHOT_RESERVE_S
    )


def account_params(values: Mapping[str, object]) -> AccountParams:
    """Parametry účtu ze `settings`; chybějící nebo nečíselná hodnota = nenastaveno (K2 A).

    Hodnota se do logu nepíše, velikost účtu je soukromá (repo je veřejné).
    """

    def number(key: str) -> float | None:
        value = values.get(key)
        if value is None:
            return None
        parsed: float | None = None
        if isinstance(value, int | float | str) and not isinstance(value, bool):
            with contextlib.suppress(ValueError):
                parsed = float(value)
        if parsed is None or not math.isfinite(parsed):
            logger.warning("Plán opce QQQ: Settings %s není číslo — bere se jako nenastavené", key)
            return None
        return parsed

    return AccountParams(account_usd=number(ACCOUNT_KEY), max_loss_usd=number(MAX_LOSS_KEY))


def _expiry_json(snap: ExpirySnapshot | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    return {
        "expiry": snap.expiry,
        "contracts": snap.contracts,
        "quoted": snap.quoted,
        "quoted_share": snap.quoted_share,
        "with_oi": snap.with_oi,
        "rows": len(snap.rows),
        "missing": {why: list(names) for why, names in snap.missing},
    }


def chain_json(snapshot: PlanChainSnapshot) -> dict[str, Any]:
    """Zdroj, čas a pokrytí snímku; podíl kotací pásma 0DTE patří k P1 (Rozhodnuto 10. 11.)."""
    data: dict[str, Any] = jsonable(
        {
            "source": CHAIN_SOURCE,
            "slot": snapshot.slot.label,
            "started_at": snapshot.started_at,
            "spot_ts": snapshot.spot_ts,
            "spot": snapshot.spot,
            "spot_nq": snapshot.spot_nq,
            "nq_source": snapshot.nq_source,
            "ratio": snapshot.ratio,
            "ts": snapshot.ts,
            "zero_dte": _expiry_json(snapshot.zero_dte),
            "one_dte": _expiry_json(snapshot.one_dte),
            "complete": snapshot.complete,
            "reasons": list(snapshot.reasons),
        },
        "plán.chain",
    )
    return data


def _et(ts: dt.datetime) -> str:
    return ts.astimezone(ET_TZ).strftime("%H:%M")


def _built_message(plan_id: int, scenario_id: int, plan: OptionPlan) -> str:
    head = (
        f"Plán QQQ #{plan_id} ke scénáři dne #{scenario_id} "
        f"({'CALL' if plan.right == 'C' else 'PUT'}, {PLAN_SLOT.label} ET)"
    )
    kind = "call" if plan.right == "C" else "put"
    options = ", ".join(
        f"{variant.role} {kind} {variant.strike:g} ({variant.expiry.day}. {variant.expiry.month}.)"
        f" {_cz(variant.bid)}/{_cz(variant.ask)}"
        for variant in plan.variants
    )
    return f"{head}: {options}. Stav: {plan.verdict}" if options else f"{head}: {plan.verdict}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class OptionPlanMaker:
    """Plán opce QQQ ke scénáři dne NQ; jedna instance na engine, vstupy jen od NQ."""

    repository: OptionPlansRepository
    #: Parametry účtu ze `settings` (blokující čtení PG, volá se ve vlákně)
    account: Callable[[], AccountParams]
    publisher: Any = None
    clock: Callable[[], dt.datetime] = _utcnow

    _snapshots: dict[dt.date, PlanChainSnapshot] = field(default_factory=dict, init=False)
    _arrived: dict[dt.date, asyncio.Event] = field(default_factory=dict, init=False)
    _tasks: set[asyncio.Task[int | None]] = field(default_factory=set, init=False)

    def on_snapshot(self, snapshot: PlanChainSnapshot) -> None:
        """Snímek z `plan_chain_loop`. Plán čte jen ranní snímek, 9:31 ET je přecenění (F2c)."""
        if snapshot.slot != PLAN_SLOT:
            return
        for day in [day for day in self._snapshots if day != snapshot.session]:
            del self._snapshots[day]
        for day in [day for day in self._arrived if day != snapshot.session]:
            del self._arrived[day]
        self._snapshots[snapshot.session] = snapshot
        self._event(snapshot.session).set()

    def on_scenario(self, created: CreatedScenario) -> asyncio.Task[int | None]:
        """Scénář NQ po zápisu; nečeká, spojení vstupů běží v samostatné úloze (#1337)."""
        task = asyncio.create_task(self._guarded(created))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _guarded(self, created: CreatedScenario) -> int | None:
        """Chyba sestavení = plán bez opce s důvodem, ať mezera je v tabulce, ne jen v logu."""
        try:
            return await self.make(created)
        except Exception as error:
            logger.exception(
                "Plán opce QQQ ke scénáři #%d selhal — ukládám plán bez opce", created.scenario_id
            )
            reason = f"plán se nesestavil ({type(error).__name__}: {error})"
            try:
                return await self._store_without_option(created, [reason], None)
            except Exception:
                logger.exception(
                    "Plán opce QQQ ke scénáři #%d: ani plán bez opce se neuložil — dnes chybí",
                    created.scenario_id,
                )
                return None

    async def make(self, created: CreatedScenario) -> int | None:
        """Plán ke scénáři: id nového řádku, None = nevznikl (už existuje, den bez seance)."""
        if not is_trading_session(created.session):
            # Scénář v takový den nevzniká (#1309); obrana proti chybě volajícího
            logger.warning(
                "Plán opce QQQ: seance %s není obchodní — plán ke scénáři #%d nevzniká",
                created.session,
                created.scenario_id,
            )
            return None
        boundary = snapshot_boundary(created.session, PLAN_SLOT)
        if created.created_at != boundary:
            reason = (
                f"pozdní scénář: štítek {_et(created.created_at)} ET, ranní snímek je z "
                f"{PLAN_SLOT.label} ET (K4 bod 5)"
            )
            return await self._store_without_option(created, [reason], None)
        deadline = snapshot_deadline(created.session)
        snapshot = await self._wait_snapshot(created.session, deadline)
        if snapshot is None:
            reason = f"ranní snímek {PLAN_SLOT.label} ET nedorazil do {deadline:%H:%M:%S} UTC"
            return await self._store_without_option(created, [reason], None)
        if not snapshot.complete:
            reason = f"ranní snímek {PLAN_SLOT.label} ET neúplný: " + "; ".join(snapshot.reasons)
            return await self._store_without_option(created, [reason], snapshot)
        account = await asyncio.to_thread(self.account)
        main, shadows = _build(created, snapshot, account)
        plan_json = {
            "slot": PLAN_SLOT.label,
            "reasons": [],
            "levels_nq": jsonable(created.levels, "plán.levels_nq"),
            "chain": chain_json(snapshot),
            "main": plan_to_json(main),
            "shadows": {key: plan_to_json(plan) for key, plan in shadows.items()},
        }
        plan_id = await asyncio.to_thread(
            self.repository.create,
            scenario_id=created.scenario_id,
            day=created.session,
            created_at=created.created_at,
            rules_version=RULES_VERSION,
            verdict=main.verdict_kind,
            status=STATUS_BUILT,
            plan=plan_json,
        )
        if plan_id is None:
            self._exists(created.scenario_id)
            return None
        message = _built_message(plan_id, created.scenario_id, main)
        logger.info("%s: %s", PRODUCT, message)
        await self._publish(plan_id, created.scenario_id, created.created_at, message)
        return plan_id

    async def recover(self, now: dt.datetime | None = None) -> int | None:
        """Start enginu: auto-scénář seance bez plánu = plán bez opce s důvodem „restart“.

        Jen do exitu seance (settle − 15 min). Později by plán nic neřekl a nasazení v pauze
        Globexu by ho založilo ke scénáři, který už je za termínem obchodu.
        """
        now = now or self.clock()
        session = trading_session_date(now)
        if not is_trading_session(session) or now >= settle_ts(session) - EXIT_BEFORE_SETTLE:
            return None
        orphan = await asyncio.to_thread(self.repository.orphan_auto_scenario, REFERENCE, session)
        if orphan is None:
            return None
        scenario_id, created_at = orphan
        reason = (
            f"engine restartoval po vzniku scénáře ({_et(created_at)} ET) — ranní snímek "
            f"{PLAN_SLOT.label} ET v tomto běhu není"
        )
        return await self._store(
            scenario_id=scenario_id,
            session=session,
            created_at=now,
            reasons=[reason],
            levels_nq=None,
            snapshot=None,
        )

    async def _store_without_option(
        self, created: CreatedScenario, reasons: list[str], snapshot: PlanChainSnapshot | None
    ) -> int | None:
        return await self._store(
            scenario_id=created.scenario_id,
            session=created.session,
            created_at=created.created_at,
            reasons=reasons,
            levels_nq=jsonable(created.levels, "plán.levels_nq"),
            snapshot=snapshot,
        )

    async def _store(
        self,
        *,
        scenario_id: int,
        session: dt.date,
        created_at: dt.datetime,
        reasons: list[str],
        levels_nq: dict[str, Any] | None,
        snapshot: PlanChainSnapshot | None,
    ) -> int | None:
        """Plán bez opce (K4 bod 5): důvody, úrovně scénáře a snímek, pokud nějaký dorazil."""
        plan_json = {
            "slot": PLAN_SLOT.label,
            "reasons": reasons,
            "levels_nq": levels_nq,
            "chain": chain_json(snapshot) if snapshot is not None else None,
            "main": None,
            "shadows": {},
        }
        plan_id = await asyncio.to_thread(
            self.repository.create,
            scenario_id=scenario_id,
            day=session,
            created_at=created_at,
            rules_version=RULES_VERSION,
            verdict=None,
            status=STATUS_NO_OPTION,
            plan=plan_json,
        )
        if plan_id is None:
            self._exists(scenario_id)
            return None
        message = (
            f"Plán QQQ #{plan_id} ke scénáři dne #{scenario_id} bez opce "
            f"({PLAN_SLOT.label} ET): " + "; ".join(reasons)
        )
        logger.log(missing_data_level(self.clock()), "%s: %s", PRODUCT, message)
        await self._publish(plan_id, scenario_id, created_at, message)
        return plan_id

    def _exists(self, scenario_id: int) -> None:
        logger.info("Plán opce QQQ ke scénáři #%d už existuje — nic se nemění", scenario_id)

    async def _publish(
        self, plan_id: int, scenario_id: int, created_at: dt.datetime, message: str
    ) -> None:
        if self.publisher is None:
            return
        try:
            await self.publisher.publish(
                "alerts",
                {
                    "kind": ALERT_KIND,
                    "symbol": PRODUCT,
                    "message": message,
                    "plan_id": plan_id,
                    "scenario_id": scenario_id,
                    "ts": created_at.timestamp(),
                },
            )
        except Exception:
            logger.exception("Plán opce QQQ #%d: alert do zvonku neodešel", plan_id)

    def _event(self, session: dt.date) -> asyncio.Event:
        event = self._arrived.get(session)
        if event is None:
            event = self._arrived[session] = asyncio.Event()
        return event

    async def _wait_snapshot(
        self, session: dt.date, deadline: dt.datetime
    ) -> PlanChainSnapshot | None:
        if session not in self._snapshots:
            timeout = (deadline - self.clock()).total_seconds()
            if timeout > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._event(session).wait(), timeout)
        return self._snapshots.get(session)


def _build(
    created: CreatedScenario, snapshot: PlanChainSnapshot, account: AccountParams
) -> tuple[OptionPlan, dict[str, OptionPlan]]:
    """Hlavní plán a stíny z úplného ranního snímku, vše z jednoho okamžiku (K4 bod 5)."""
    zero, one = snapshot.zero_dte, snapshot.one_dte
    if zero is None or one is None:
        raise ValueError("úplný snímek bez expirace 0DTE nebo 1DTE")
    expiry = dt.datetime.strptime(one.expiry, "%Y%m%d").date()

    def build(rules: PlanRules) -> OptionPlan:
        return build_option_plan(
            scenario=created.scenario,
            levels=created.levels,
            ratio=snapshot.ratio,
            spot=snapshot.spot,
            chain_0dte=zero.rows,
            chain_1dte=one.rows,
            expiry_1dte=expiry,
            account=account,
            now=created.created_at,
            rules=rules,
        )

    return build(MAIN_RULES), {key: build(rules) for key, rules in SHADOWS.items()}
