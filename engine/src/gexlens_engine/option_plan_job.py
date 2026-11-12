"""Plán opce QQQ se scénářem dne NQ: vznik v 9:15 ET a přecenění v 9:31 ET (ADR-0062 K4 C).

Vznik (F2b #1742) spojí dva vstupy, které přicházejí nezávisle a v libovolném pořadí:
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

Přecenění (F2c #1769) spustí snímek řetězu 9:31 ET a běží v samostatné úloze. Výsledek
se zapíše jednou do klíče `plan.repricing` (`OptionPlansRepository.record_repricing`):
- **úplný plán** (`sestaven`): kontrakty z 9:15 se přecení (`reprice_variants`), hlavní
  plán i oba stíny. Strike, úrovně, poměr, vstup A, EM ani stav se nemění (K4 C bod 2);
- **plán bez opce:** sestaví se celý z 9:31 (K4 bod 5). Scénář je z řádku `scenarios`,
  flip, zdi a těžiště z úrovní NQ minuty 9:31 (`on_levels`), ONH/ONL a PDH/PDL/PDC z barů
  v 9:31 (`day_levels`, táž funkce jako scénář). Sloupce `status` a `verdict` pak popíšou
  plán z 9:31 (vlastník 11. 11., #1738). Zpráva začíná „ZMĚNA“, když stav přejde na
  `čeká` (K4 bod 4);
- **neúplný snímek 9:31** nebo kontrakt plánu bez kotace: ceny se nezapíšou a do plánu jde
  důvod (K4 bod 6). Když snímek 9:31 v běhu enginu nedorazí (chyba, tasty, start po 9:31),
  zapíše důvod hlídač (`_watch_repricing`, spouští ho první minuta úrovní NQ od 9:31).
Alert `option_plan_repriced`. R:R, P3, P4 a rozpočet se ani v 9:31 nepočítají (K12 A).

Parametry účtu (`option_plan_account_usd`, `option_plan_max_loss_usd` v `settings`, K2 A) se
čtou v okamžiku plánu. Jejich hodnoty do logu ani do zprávy nejdou.
"""

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import (
    ACCOUNT_KEY,
    CHAIN_SOURCE,
    EXIT_BEFORE_SETTLE,
    MAIN_RULES,
    MAX_LOSS_KEY,
    RULES_VERSION,
    SHADOW_B,
    SHADOW_WICK,
    AccountParams,
    OptionPlan,
    PlanRules,
    VariantKey,
    _cz,
    account_value,
    build_option_plan,
    jsonable,
    plan_to_json,
    reprice_variants,
)
from gexlens_engine.compute.settle import ET_TZ, is_trading_session, settle_ts, trading_session_date
from gexlens_engine.scenario_auto import CreatedScenario, VerdictContext
from gexlens_engine.storage.option_plans_store import (
    REPRICING_KEY,
    OptionPlanRow,
    OptionPlansRepository,
)
from gexlens_engine.storage.parquet_store import LevelsRow
from gexlens_engine.storage.scenarios_store import ScenarioRow, ScenariosRepository
from gexlens_engine.tasty.plan_chain import (
    PLAN_SLOT,
    PRODUCT,
    QUOTE_TIMEOUT_S,
    REFERENCE,
    REPRICE_SLOT,
    ExpirySnapshot,
    PlanChainSnapshot,
    SnapshotSlot,
    missing_data_level,
    snapshot_boundary,
)

logger = logging.getLogger(__name__)

#: Rezerva po limitu snímku na zápis jeho partic a naplánování úloh
SNAPSHOT_RESERVE_S = 60.0
#: `option_plans.status` při vzniku
STATUS_BUILT = "sestaven"
STATUS_NO_OPTION = "bez opce"
ALERT_KIND = "option_plan_created"
REPRICE_ALERT_KIND = "option_plan_repriced"
#: Začátek zprávy přecenění, když se změní stav plánu (K4 C bod 4)
CHANGE = "ZMĚNA"
#: Stíny track recordu (K10 B) v `plan.shadows`; klíč = parametr pravidel
SHADOWS: Mapping[str, PlanRules] = {"entry_b": SHADOW_B, "wick_test": SHADOW_WICK}
#: Úrovně NQ minuty, které plán z 9:31 bere z pipeline; ONH/ONL a PD čte z barů (API)
NQ_MINUTE_LEVELS = ("flip", "call_wall", "put_wall", "centroid")
#: Na úrovně NQ minuty 9:31 plán čeká do 9:34 ET: cyklus minuty 9:31 je předá až po scénáři
#: (10. 11. scénář 68 s po štítku), přetažená minuta i později. První 5min close je v 9:35 ET,
#: takže plán z 9:31 přijde před první možnou spouští vstupu A
LEVELS_WAIT = dt.timedelta(minutes=3)
#: Strop čtení ONH/ONL a PDH/PDL/PDC z API (verdikt dne čte celý kontext ~2,4 s)
LEVELS_READ_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class _Outcome:
    """Přecenění jednoho plánu: JSON `plan.repricing`, zpráva a úroveň logu.

    `verdict` jen u plánu sestaveného celého v 9:31: sloupce `status` a `verdict` pak
    popíšou aktuální stav plánu (vlastník 11. 11., #1738). Jinak zůstanou z 9:15.
    """

    repricing: dict[str, Any]
    message: str
    level: int
    verdict: str | None = None


def snapshot_deadline(session: dt.date, slot: SnapshotSlot = PLAN_SLOT) -> dt.datetime:
    """Do kdy plán čeká na snímek: hranice + limit snímku 40 s + rezerva."""
    return snapshot_boundary(session, slot) + dt.timedelta(
        seconds=QUOTE_TIMEOUT_S + SNAPSHOT_RESERVE_S
    )


def levels_deadline(session: dt.date) -> dt.datetime:
    """Do kdy plán bez opce čeká v 9:31 na úrovně NQ minuty 9:31 (`LEVELS_WAIT`)."""
    return snapshot_boundary(session, REPRICE_SLOT) + LEVELS_WAIT


def account_params(values: Mapping[str, object]) -> AccountParams:
    """Parametry účtu ze `settings`; chybějící nebo neplatná hodnota = nenastaveno (K2 A).

    Platnost posuzuje `account_value`, táž funkce jako validace `PUT /settings` v API.
    Hodnota se do logu nepíše, velikost účtu je soukromá (repo je veřejné).
    """

    def number(key: str) -> float | None:
        try:
            return account_value(values.get(key))
        except ValueError as error:
            logger.warning(
                "Plán opce QQQ: Settings %s je neplatné (%s) — bere se jako nenastavené",
                key,
                error,
            )
            return None

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
    #: Scénář dne pro plán bez opce sestavený v 9:31 ET (F2c); `CreatedScenario` je jen v paměti
    scenarios: ScenariosRepository
    #: ONH/ONL a PDH/PDL/PDC seance v okamžiku volání (`scenario_auto.gather_day_levels`)
    day_levels: Callable[[dt.date], Awaitable[VerdictContext]]
    publisher: Any = None
    clock: Callable[[], dt.datetime] = _utcnow

    _snapshots: dict[dt.date, PlanChainSnapshot] = field(default_factory=dict, init=False)
    _arrived: dict[dt.date, asyncio.Event] = field(default_factory=dict, init=False)
    #: Úrovně NQ minuty 9:31 ET podle seance (F2c) a jejich příchod
    _levels: dict[dt.date, LevelsRow] = field(default_factory=dict, init=False)
    _levels_arrived: dict[dt.date, asyncio.Event] = field(default_factory=dict, init=False)
    #: Snímek 9:31 ET dorazil do přecenění (hlídač mezery, F2c)
    _repriced: dict[dt.date, asyncio.Event] = field(default_factory=dict, init=False)
    _watched: set[dt.date] = field(default_factory=set, init=False)
    _started_at: dt.datetime = field(init=False)
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    def on_snapshot(self, snapshot: PlanChainSnapshot) -> asyncio.Task[list[int]] | None:
        """Snímek z `plan_chain_loop`: 9:15 ET pro vznik, 9:31 ET spustí přecenění (F2c).

        Přecenění běží v úloze (vrací ji), volající nečeká.
        """
        if snapshot.slot == REPRICE_SLOT:
            return self._spawn(self._reprice_task(snapshot))
        if snapshot.slot != PLAN_SLOT:
            return None
        _keep_only(self._snapshots, snapshot.session)
        _keep_only(self._arrived, snapshot.session)
        self._snapshots[snapshot.session] = snapshot
        _event(self._arrived, snapshot.session).set()
        return None

    def on_levels(self, levels: LevelsRow) -> None:
        """Úrovně NQ minuty z minutového cyklu pipeline NQ (F2c); jen uloží a nečeká (#1337).

        Plán bere jen minutu 9:31 ET, jiná minuta by ho posunula v čase (9:32 = pohled
        dopředu). První minuta od 9:31 do exitu spustí hlídač přecenění: když snímek 9:31
        v tomto běhu nedorazí (chyba snímku, tasty neběží, start enginu po 9:31), dostane
        plán důvod, ne mlčení (K4 bod 6).
        """
        session = trading_session_date(levels.ts_min)
        if not is_trading_session(session):
            return
        boundary = snapshot_boundary(session, REPRICE_SLOT)
        if not boundary <= levels.ts_min < settle_ts(session) - EXIT_BEFORE_SETTLE:
            return
        if levels.ts_min == boundary:
            _keep_only(self._levels, session)
            _keep_only(self._levels_arrived, session)
            self._levels[session] = levels
            _event(self._levels_arrived, session).set()
        if session not in self._watched:
            self._watched = {session}
            self._spawn(self._watch_repricing(session))

    def on_scenario(self, created: CreatedScenario) -> asyncio.Task[int | None]:
        """Scénář NQ po zápisu; nečeká, spojení vstupů běží v samostatné úloze (#1337)."""
        return self._spawn(self._guarded(created))

    def _spawn[T](self, work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(work)
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
        main, shadows = _build(
            created.scenario, created.levels, snapshot, account, created.created_at
        )
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
        await self._publish(ALERT_KIND, plan_id, created.scenario_id, created.created_at, message)
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
        await self._publish(ALERT_KIND, plan_id, scenario_id, created_at, message)
        return plan_id

    def _exists(self, scenario_id: int) -> None:
        logger.info("Plán opce QQQ ke scénáři #%d už existuje — nic se nemění", scenario_id)

    async def _reprice_task(self, snapshot: PlanChainSnapshot) -> list[int]:
        """Úloha přecenění; pád čtení PG nesmí zmizet v nesledované úloze."""
        try:
            return await self.reprice(snapshot)
        except Exception:
            logger.exception(
                "Přecenění plánu opce QQQ %s ET seance %s selhalo — plán nese ceny z %s ET",
                REPRICE_SLOT.label,
                snapshot.session,
                PLAN_SLOT.label,
            )
            return []

    async def reprice(self, snapshot: PlanChainSnapshot) -> list[int]:
        """Přecenění plánů seance snímkem 9:31 ET; vrací id plánů, kterým se zapsalo.

        Každý plán jednou (`plan.repricing`), restart enginu druhé přecenění nevytvoří.
        """
        session = snapshot.session
        if snapshot.slot != REPRICE_SLOT:
            raise ValueError(f"přecenění čte snímek {REPRICE_SLOT.label} ET, ne {snapshot.slot}")
        # Hlídač mezery (`_watch_repricing`): snímek dorazil, důvod za něj nepíše
        _keep_only(self._repriced, session)
        _event(self._repriced, session).set()
        if not is_trading_session(session):
            # Snímek v takový den nevzniká (`next_snapshot`); obrana proti chybě volajícího
            logger.warning("Plán opce QQQ: seance %s není obchodní — přecenění nebude", session)
            return []
        rows = await asyncio.to_thread(self.repository.plans_of_day, session)
        if not rows:
            logger.info(
                "Plán opce QQQ: k přecenění %s ET seance %s není plán (scénář dne nevznikl)",
                REPRICE_SLOT.label,
                session,
            )
            return []
        written: list[int] = []
        boundary = snapshot_boundary(session, REPRICE_SLOT)
        for row in rows:
            if REPRICING_KEY in row.plan:
                self._repriced_exists(row.id)
                continue
            try:
                outcome = await self._repricing(row, snapshot)
            except Exception as error:
                logger.exception("Přecenění plánu opce QQQ #%d selhalo", row.id)
                reasons = [f"přecenění se nesestavilo ({type(error).__name__}: {error})"]
                outcome = self._gap(row, reasons, None)
            if await self._record(row, outcome, boundary):
                written.append(row.id)
        return written

    async def _watch_repricing(self, session: dt.date) -> list[int]:
        """Snímek 9:31 v tomto běhu nedorazil do limitu: plánům seance důvod, ne mlčení.

        Pokrývá chybu snímku (`plan_chain_loop` ji jen zaloguje), tasty větev mimo provoz
        i start enginu po 9:31 (snímek se nedohání, `plan_chain.next_snapshot`). Zápis je
        idempotentní, plán s přeceněním se nemění.
        """
        try:
            boundary = snapshot_boundary(session, REPRICE_SLOT)
            deadline = snapshot_deadline(session, REPRICE_SLOT)
            started_late = self._started_at >= boundary
            if not started_late:
                await self._wait(self._repriced, session, deadline)
            if _event(self._repriced, session).is_set():
                return []
            reason = (
                f"engine startoval po {REPRICE_SLOT.label} ET — snímek přecenění se v tomto běhu "
                "nedohání"
                if started_late
                else f"snímek {REPRICE_SLOT.label} ET nedorazil do {deadline:%H:%M:%S} UTC"
            )
            rows = await asyncio.to_thread(self.repository.plans_of_day, session)
            now = self.clock()
            written: list[int] = []
            for row in rows:
                if REPRICING_KEY in row.plan:
                    continue
                if await self._record(row, self._gap(row, [reason], None), now):
                    written.append(row.id)
            return written
        except Exception:
            logger.exception("Plán opce QQQ: hlídač přecenění seance %s selhal", session)
            return []

    async def _record(self, row: OptionPlanRow, outcome: _Outcome, ts: dt.datetime) -> bool:
        """Zápis `plan.repricing` jednou, log a alert; False = už zapsané (restart, hlídač)."""
        written = await asyncio.to_thread(
            self.repository.record_repricing,
            row.id,
            outcome.repricing,
            status=STATUS_BUILT if outcome.verdict is not None else None,
            verdict=outcome.verdict,
        )
        if not written:
            self._repriced_exists(row.id)
            return False
        logger.log(outcome.level, "%s: %s", PRODUCT, outcome.message)
        await self._publish(REPRICE_ALERT_KIND, row.id, row.scenario_id, ts, outcome.message)
        return True

    async def _repricing(self, row: OptionPlanRow, snapshot: PlanChainSnapshot) -> _Outcome:
        """Přecenění jednoho plánu seance podle stavu z 9:15."""
        if row.status == STATUS_BUILT:
            return self._reprice_built(row, snapshot)
        if row.status == STATUS_NO_OPTION:
            return await self._build_at_reprice(row, snapshot)
        raise ValueError(f"plán #{row.id} má stav {row.status!r}, přecenění ho nezná")

    def _reprice_built(self, row: OptionPlanRow, snapshot: PlanChainSnapshot) -> _Outcome:
        """Úplný plán: kontrakty z 9:15 se přecení, stav se nemění (K4 C bod 2, bod 6)."""
        chain = chain_json(snapshot)
        reasons = _snapshot_gaps(snapshot)
        one = snapshot.one_dte
        if reasons or one is None:
            return self._gap(row, reasons or [f"snímek {REPRICE_SLOT.label} ET bez 1DTE"], chain)
        boundary = snapshot_boundary(snapshot.session, REPRICE_SLOT)
        expiry = dt.datetime.strptime(one.expiry, "%Y%m%d").date()
        plans = {"main": row.plan["main"], **row.plan["shadows"]}
        prices: dict[str, list[Any]] = {}
        gaps: list[str] = []
        for key, plan in plans.items():
            repriced, missing = reprice_variants(
                _variant_keys(plan),
                chain_1dte=one.rows,
                expiry_1dte=expiry,
                targets=[float(target) for target in plan["targets"]],
                target_ts=[dt.datetime.fromisoformat(ts) for ts in plan["target_ts"]],
                exit_ts=dt.datetime.fromisoformat(plan["exit_ts"]),
                now=boundary,
            )
            gaps.extend(missing)
            prices[key] = jsonable(list(repriced), f"plán.repricing.prices.{key}")
        if gaps:
            return self._gap(row, list(dict.fromkeys(gaps)), chain)
        repricing = {**_repricing_json([], chain), "prices": prices}
        return _Outcome(repricing, _repriced_message(row, prices["main"]), logging.INFO)

    async def _build_at_reprice(self, row: OptionPlanRow, snapshot: PlanChainSnapshot) -> _Outcome:
        """Plán bez opce z 9:15: celý plán z 9:31, nic z 9:15 se nemíchá (K4 bod 5).

        Úrovně jsou z 9:31 jako u K4 A a goldenu K9 A: flip, zdi a těžiště z minuty 9:31
        pipeline NQ, ONH/ONL od začátku seance do openu a PDH/PDL/PDC z barů (ADR-0062
        Odvozeno). Plán z restartu (`recover`) se tak sestaví stejně. Sloupce řádku pak
        popíšou plán z 9:31 (`sestaven`, verdikt hlavního plánu), stav z 9:15 zůstane v JSON.
        """
        chain = chain_json(snapshot)
        session = snapshot.session
        reasons = _snapshot_gaps(snapshot)
        scenario_row = await asyncio.to_thread(self.scenarios.get, row.scenario_id)
        if scenario_row is None:
            reasons.append(f"scénář dne #{row.scenario_id} v PG už není")
        if reasons or scenario_row is None:
            return self._gap(row, reasons, chain)
        deadline = levels_deadline(session)
        minute = await self._wait_levels(session, deadline)
        if minute is None:
            reasons.append(
                f"úrovně NQ minuty {REPRICE_SLOT.label} ET nedorazily do {deadline:%H:%M:%S} UTC"
            )
            return self._gap(row, reasons, chain)
        try:
            day = await asyncio.wait_for(self.day_levels(session), LEVELS_READ_TIMEOUT_S)
        except TimeoutError:
            day = VerdictContext(failures=[f"čtení nedoběhlo do {LEVELS_READ_TIMEOUT_S:g} s"])
        if day.failures:
            reasons.append("ONH/ONL a PDH/PDL/PDC z API: " + "; ".join(day.failures))
            return self._gap(row, reasons, chain)
        levels = LevelInputs(
            **{name: getattr(minute, name) for name in NQ_MINUTE_LEVELS},
            prev_high=day.prev_high,
            prev_low=day.prev_low,
            prev_close=day.prev_close,
            on_high=day.on_high,
            on_low=day.on_low,
        )
        account = await asyncio.to_thread(self.account)
        boundary = snapshot_boundary(session, REPRICE_SLOT)
        main, shadows = _build(_auto_scenario(scenario_row), levels, snapshot, account, boundary)
        plan = {
            "slot": REPRICE_SLOT.label,
            "levels_nq": jsonable(levels, "plán.repricing.plan.levels_nq"),
            "main": plan_to_json(main),
            "shadows": {key: plan_to_json(shadow) for key, shadow in shadows.items()},
        }
        repricing = {**_repricing_json([], chain), "plan": plan}
        return _Outcome(
            repricing, _rebuilt_message(row, main), logging.INFO, verdict=main.verdict_kind
        )

    def _gap(
        self, row: OptionPlanRow, reasons: list[str], chain: dict[str, Any] | None
    ) -> _Outcome:
        """Přecenění bez cen: úplný plán drží ceny z 9:15, plán bez opce zůstane bez opce."""
        if row.status == STATUS_BUILT:
            message = (
                f"Plán QQQ #{row.id}: přecenění {REPRICE_SLOT.label} ET se nezapsalo — "
                + "; ".join(reasons)
                + f". Platí ceny z {PLAN_SLOT.label} ET"
            )
        else:
            message = (
                f"Plán QQQ #{row.id} ke scénáři dne #{row.scenario_id} zůstává bez opce "
                f"({REPRICE_SLOT.label} ET): " + "; ".join(reasons)
            )
        return _Outcome(_repricing_json(reasons, chain), message, missing_data_level(self.clock()))

    def _repriced_exists(self, plan_id: int) -> None:
        logger.info("Přecenění plánu opce QQQ #%d už je zapsané — nic se nemění", plan_id)

    async def _publish(
        self, kind: str, plan_id: int, scenario_id: int, ts: dt.datetime, message: str
    ) -> None:
        if self.publisher is None:
            return
        try:
            await self.publisher.publish(
                "alerts",
                {
                    "kind": kind,
                    "symbol": PRODUCT,
                    "message": message,
                    "plan_id": plan_id,
                    "scenario_id": scenario_id,
                    "ts": ts.timestamp(),
                },
            )
        except Exception:
            logger.exception("Plán opce QQQ #%d: alert do zvonku neodešel", plan_id)

    async def _wait_snapshot(
        self, session: dt.date, deadline: dt.datetime
    ) -> PlanChainSnapshot | None:
        await self._wait(self._arrived, session, deadline)
        return self._snapshots.get(session)

    async def _wait_levels(self, session: dt.date, deadline: dt.datetime) -> LevelsRow | None:
        await self._wait(self._levels_arrived, session, deadline)
        return self._levels.get(session)

    async def _wait(
        self, events: dict[dt.date, asyncio.Event], session: dt.date, deadline: dt.datetime
    ) -> None:
        event = _event(events, session)
        if event.is_set():
            return
        timeout = (deadline - self.clock()).total_seconds()
        if timeout > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)


def _keep_only[V](items: dict[dt.date, V], session: dt.date) -> None:
    """Paměť jen pro jednu seanci (dřívější dny se zahodí)."""
    for day in [day for day in items if day != session]:
        del items[day]


def _event(events: dict[dt.date, asyncio.Event], session: dt.date) -> asyncio.Event:
    event = events.get(session)
    if event is None:
        event = events[session] = asyncio.Event()
    return event


def _snapshot_gaps(snapshot: PlanChainSnapshot) -> list[str]:
    """K4 bod 6: neúplný snímek 9:31 = důvod, přecenění se nezapíše."""
    if snapshot.complete:
        return []
    return [f"snímek {REPRICE_SLOT.label} ET neúplný: " + "; ".join(snapshot.reasons)]


def _repricing_json(reasons: list[str], chain: dict[str, Any] | None) -> dict[str, Any]:
    """`plan.repricing`: ceny (`prices`) u úplného plánu, nebo celý plán z 9:31 (`plan`)."""
    return {
        "slot": REPRICE_SLOT.label,
        "reasons": reasons,
        "chain": chain,
        "prices": None,
        "plan": None,
    }


def _variant_keys(plan: Mapping[str, Any]) -> tuple[VariantKey, ...]:
    """Kontrakty variant plánu z JSON (`plan_to_json`)."""
    return tuple(
        VariantKey(
            role=variant["role"],
            right=variant["right"],
            strike=float(variant["strike"]),
            expiry=dt.date.fromisoformat(variant["expiry"]),
        )
        for variant in plan["variants"]
    )


def _auto_scenario(row: ScenarioRow) -> AutoScenario:
    """Auto-scénář z řádku `scenarios` (směr a popisky cílů v `rationale`, #1173)."""
    rationale = row.rationale or {}
    direction = rationale.get("verdict")
    if direction not in ("long", "short"):
        raise ValueError(f"scénář #{row.id}: směr {direction!r} není long ani short")
    return AutoScenario(
        direction=direction,
        entry=row.entry,
        targets=tuple(row.targets),
        target_labels=tuple(str(label) for label in rationale.get("targets") or ()),
        path=tuple(
            (dt.datetime.fromisoformat(str(point["ts"])), float(point["price"]))
            for point in row.path
        ),
    )


def _kind(right: str) -> str:
    return "call" if right == "C" else "put"


def _repriced_message(row: OptionPlanRow, prices: list[dict[str, Any]]) -> str:
    """Přecenění úplného plánu: nové kotace vedle kotací z 9:15, stav beze změny."""
    morning = {(v["role"], v["strike"]): v for v in row.plan["main"]["variants"]}
    parts = []
    for price in prices:
        before = morning.get((price["role"], price["strike"]))
        was = f" (v {PLAN_SLOT.label} {_cz(before['bid'])}/{_cz(before['ask'])})" if before else ""
        parts.append(
            f"{price['role']} {_kind(price['right'])} {price['strike']:g} "
            f"{_cz(price['bid'])}/{_cz(price['ask'])}{was}"
        )
    return (
        f"Plán QQQ #{row.id} ke scénáři dne #{row.scenario_id} přeceněn v "
        f"{REPRICE_SLOT.label} ET: "
        + ", ".join(parts)
        + f". Stav beze změny: {row.plan['main']['verdict']}"
    )


def _rebuilt_message(row: OptionPlanRow, plan: OptionPlan) -> str:
    """Plán bez opce z 9:15 sestavený v 9:31; „ZMĚNA“, když hlavní plán není neobchodovat."""
    options = ", ".join(
        f"{v.role} {_kind(v.right)} {v.strike:g} ({v.expiry.day}. {v.expiry.month}.)"
        f" {_cz(v.bid)}/{_cz(v.ask)}"
        for v in plan.variants
    )
    before = "; ".join(row.plan.get("reasons") or ()) or "bez opce"
    # Plán bez opce = neobchodovat; „ZMĚNA“ jen při změně čeká × neobchodovat (K4 bod 4)
    changed = plan.verdict_kind != "neobchodovat"
    return (
        (f"{CHANGE}: " if changed else "")
        + f"Plán QQQ #{row.id} ke scénáři dne #{row.scenario_id} sestaven z "
        f"{REPRICE_SLOT.label} ET ({'CALL' if plan.right == 'C' else 'PUT'})"
        + (f": {options}" if options else "")
        + f". Stav: neobchodovat — bez opce ({before}) → {plan.verdict}"
    )


def _build(
    scenario: AutoScenario,
    levels: LevelInputs,
    snapshot: PlanChainSnapshot,
    account: AccountParams,
    now: dt.datetime,
) -> tuple[OptionPlan, dict[str, OptionPlan]]:
    """Hlavní plán a stíny z úplného snímku, vše z jednoho okamžiku `now` (K4 bod 5)."""
    zero, one = snapshot.zero_dte, snapshot.one_dte
    if zero is None or one is None:
        raise ValueError("úplný snímek bez expirace 0DTE nebo 1DTE")
    expiry = dt.datetime.strptime(one.expiry, "%Y%m%d").date()

    def build(rules: PlanRules) -> OptionPlan:
        return build_option_plan(
            scenario=scenario,
            levels=levels,
            ratio=snapshot.ratio,
            spot=snapshot.spot,
            chain_0dte=zero.rows,
            chain_1dte=one.rows,
            expiry_1dte=expiry,
            account=account,
            now=now,
            rules=rules,
        )

    return build(MAIN_RULES), {key: build(rules) for key, rules in SHADOWS.items()}
