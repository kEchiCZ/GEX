"""Živá upozornění plánu opce QQQ (F3b #1757, ADR-0062 K5 B) — přechody stavu hlavního plánu.

Každou minutu, kterou odběr plánu (F1b #1747, `tasty/plan_quotes.PlanQuoteRecorder`) zapíše
do Parquet, spočítá živý stav hlavního plánu táž čistá funkce jako večerní vyhodnocení
(`compute/option_plan.evaluate_plan` s okamžikem `now` = závěr zapsané minuty). Přechody
(`live_transitions`) jdou do zvonku a pod přepínačem „Plán opce QQQ“ na Telegram jako alert
`option_plan_state`, každý jednou:
- vstup A (SL, TP1, TP2, exit, R:R z kotace a verdikt P3 + P4 jako plán v jedné zprávě), pak
  TP1, TP2, SL, stop na vstupu, nebo exit; bez vstupu do exitu; mezera v barech QQQ;
- stíny K10 B upozornění nemají, plán bez úrovně vstupu A do 1 EM (verdikt „neobchodovat“
  od 9:15 ET) se nesleduje;
- když se žádná varianta neobchoduje (P3, P4, nenastavený rozpočet), průběh se hlásí dál
  jako hypotetický (Rozhodnuto 18. 11.: 4b B); mezera = konec sledování, výsledek dá F3 (4a A).

Idempotence: přechod se před odesláním zapíše do `option_plans.live_alerts`
(`claim_live_alert`, atomicky). Restart enginu ani souběh ho nepošlou dvakrát, selhané
odeslání se neopakuje. Konečný přechod sledování plánu ukončí.

Brány: jen obchodní den (`compute/settle.is_trading_session`) a jen v US RTH od openu do
settle; zavřený trh nic nehlásí (`compute/marketclock.is_market_closed`, AGENTS.md). Odběr
plánu minutu ohlásí jen ve dnech s plánem. Práce je úměrná dnu: partice barů QQQ od openu
a kotace seance.

Minutový cyklus na nic nečeká (#1337, #1646): kontrola je úloha na pozadí, PG i Parquet
ve vlákně; běží-li předchozí, minuta se přeskočí a stav dožene další (přechody se odvozují
z celého dne). Parametry účtu ze Settings rozhodnou verdikt varianty při spoušti (K2,
K12 A), hodnoty do zprávy ani do logu nejdou.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.option_plan import (
    LEVEL_LABELS,
    MAIN_RULES,
    MINUTE,
    AccountParams,
    LiveTransition,
    OptionExit,
    PlanResult,
    QuoteLike,
    VariantResult,
    _cz,
    evaluate_plan,
    live_transitions,
    trigger_verdict,
    tws_stop,
)
from gexlens_engine.compute.settle import ET_TZ, is_trading_session, settle_ts, us_open_ts
from gexlens_engine.compute.setups import PathBar
from gexlens_engine.option_plan_eval import _usd
from gexlens_engine.storage.option_plans_store import (
    OptionPlanRow,
    OptionPlansRepository,
    effective_plan,
)
from gexlens_engine.storage.parquet_store import read_bars, read_option_quotes
from gexlens_engine.tasty.plan_chain import PRODUCT

logger = logging.getLogger(__name__)

ALERT_KIND = "option_plan_state"
#: Čas zprávy i v Praze (vlastník čte z telefonu)
PRAGUE_TZ = ZoneInfo("Europe/Prague")
#: Nadpis přechodu na začátku zprávy: dedup Telegramu porovnává prvních 80 znaků (#1745)
HEADLINES: Mapping[str, str] = {
    "vstup": "VSTUP",
    "TP1": "TP1",
    "TP2": "TP2",
    "SL": "SL",
    "stop na vstupu": "STOP NA VSTUPU",
    "exit": "EXIT",
    "bez vstupu": "BEZ VSTUPU",
    "mezera": "MEZERA",
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _when(ts: dt.datetime) -> str:
    return f"{ts.astimezone(ET_TZ):%H:%M} ET ({ts.astimezone(PRAGUE_TZ):%H:%M} Praha)"


def _name(variant: VariantResult) -> str:
    return f"{variant.role} {'call' if variant.right == 'C' else 'put'} {variant.strike:g}"


def live_part(plan: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Hlavní plán řádku, který živý stav sleduje: platný plán s opcí a úrovní vstupu A."""
    main: Mapping[str, Any] | None = effective_plan(plan)["main"]
    if main is None or not main["variants"] or main["level_a"] is None:
        return None
    return main


def _verdicts(
    result: PlanResult, account: AccountParams
) -> list[tuple[VariantResult, str, tuple[str, ...]]]:
    return [(variant, *trigger_verdict(variant, account)) for variant in result.variants]


def hypothetical(result: PlanResult, account: AccountParams) -> str | None:
    """Žádná varianta se při spoušti neobchoduje: průběh se hlásí jen hypoteticky (4b B).

    Vrací štítek do zpráv po vstupu, None = aspoň jedna varianta je `obchodovat`, nebo `čeká`
    (kotace chybí, rozhoduje ask v TWS). Vyřazení P3 má vlastní štítek z textu vlastníka.
    """
    verdicts = _verdicts(result, account)
    if not verdicts or any(verdict != "neobchodovat" for _, verdict, _ in verdicts):
        return None
    if all(variant.kind == "vyřazeno P3" for variant, _, _ in verdicts):
        return "hypoteticky (vyřazeno P3)"
    return "hypoteticky (neobchodovat)"


def _entry_variant(variant: VariantResult, verdict: str, reasons: tuple[str, ...]) -> str:
    """Varianta ve zprávě vstupu: ask, R:R z kotace a verdikt P3 + P4 jako plán (K12 A)."""
    name = _name(variant).capitalize()
    pricing = variant.pricing
    why = f"neobchodovat — {'; '.join(reasons)}"
    if pricing is None:
        if verdict == "čeká":
            return f"{name}: kotace při vstupu chybí → R:R nejde určit, zkontroluj ask v TWS"
        return f"{name}: kotace při vstupu chybí → {why}"
    ask = f"{name}: ask {_cz(pricing.paid)}"
    if variant.kind == "vyřazeno P3":
        return f"{ask} → {why}"  # důvod P3 nese R:R z kotace
    rr = f"R:R z kotace {_cz(pricing.rr)}" if pricing.rr is not None else "R:R nejde určit"
    if verdict == "obchodovat":
        return f"{ask}, {rr} → obchodovat (v rozpočtu)"
    return f"{ask}, {rr} → {why}"


def _exit_variant(variant: VariantResult, leg: OptionExit | None) -> str:
    name = _name(variant)
    if variant.kind == "bez kotace":
        return f"{name}: bez kotace při vstupu"
    if leg is None or leg.bid is None:
        return f"{name}: kotace chybí"
    return f"{name} bid {_cz(leg.bid)} ({_usd(leg.pnl_usd)}/kontrakt)"


def live_message(
    plan_id: int,
    part: Mapping[str, Any],
    result: PlanResult,
    transition: LiveTransition,
    account: AccountParams,
) -> str:
    """Text upozornění přechodu: nadpis přechodu hned za číslem plánu, časy v ET i v Praze.

    Verdikt varianty při spoušti jde podle P3, P4 a parametrů účtu v okamžiku upozornění
    (`trigger_verdict`). Parametry účtu ani hodnota rozpočtu ve zprávě nejsou.
    """
    head = f"Plán QQQ #{plan_id} · {HEADLINES[transition.event]}"
    exit_ts = dt.datetime.fromisoformat(part["exit_ts"])
    if transition.event == "mezera":
        return (
            f"{head}: {'; '.join(result.reasons)}. Živý stav se dál nesleduje, "
            "výsledek přijde po close."
        )
    if transition.event == "bez vstupu":
        return f"{head} do exitu {_when(exit_ts)}. Plán končí."
    entry = result.entry
    if entry is None:
        raise ValueError(f"plán QQQ #{plan_id}: přechod {transition.event} bez vstupu")
    side = 1 if part["direction"] == "long" else -1
    kinds = [event.kind for event in result.events]
    index = kinds.index(transition.event)
    label = hypothetical(result, account)
    if index == 0:
        verdicts = _verdicts(result, account)
        targets = [float(value) for value in part["targets"]]
        levels = ", ".join(f"TP{i} {_cz(value)}" for i, value in enumerate(targets, start=1))
        parts = [
            f"{head} {entry.kind} {_when(entry.ts)} za {_cz(entry.price)}, odmítnutí od "
            f"{LEVEL_LABELS.get(entry.level, entry.level)} {_cz(entry.level_price)}, "
            f"SL {_cz(entry.stop)} (5min close).",
            *(f"{_entry_variant(*item)}." for item in verdicts),
            f"{levels}, exit {exit_ts.astimezone(ET_TZ):%H:%M} ET.",
        ]
        # Podmínka TWS k variantě, která se obchoduje, jinak k té bez kotace (rozhodne ask v TWS)
        trade = next(
            (
                v
                for wanted in ("obchodovat", "čeká")
                for v, verdict, _ in verdicts
                if verdict == wanted
            ),
            None,
        )
        if trade is not None:
            option = f"{'call' if trade.right == 'C' else 'put'} {trade.strike:g}"
            parts.append(f"TWS: {tws_stop(side, _cz(entry.stop), option)}.")
        elif label is not None:
            parts.append("Průběh dál jen hypoteticky.")
        if transition.final:
            parts.append("Cíl 1 leží za vstupem, plán končí.")
        return " ".join(parts)
    event = result.events[index]
    if event.kind in ("TP1", "TP2"):
        what = f"{head} {_cz(event.price)} v {_when(event.ts)}"
    elif event.kind == "exit":
        what = f"{head} {_when(event.ts)}, QQQ {_cz(event.price)}"
    else:
        what = f"{head}: 5min close {_cz(event.price)} v {_when(event.ts)}"
    if label is not None:
        what += f", {label}"
    legs = ", ".join(
        _exit_variant(variant, variant.exit_tp1 if index == 1 else variant.exit_hold)
        for variant in result.variants
    )
    tail = "Plán končí." if transition.final else f"SL posunut na vstup {_cz(entry.price)}."
    return f"{what}. {legs[:1].upper()}{legs[1:]}. {tail}"


@dataclass
class OptionPlanLiveWatcher:
    """Živý stav hlavního plánu QQQ po každé zapsané minutě odběru plánu (F3b #1757)."""

    repository: OptionPlansRepository
    derived_dir: Path
    #: Parametry účtu ze `settings` (blokující čtení PG, volá se ve vlákně)
    account: Callable[[], AccountParams]
    publisher: Any = None
    clock: Callable[[], dt.datetime] = _utcnow

    _task: asyncio.Task[None] | None = field(default=None, init=False)
    #: Plány s odeslaným konečným přechodem: partice se pro ně už nečtou
    _finished: set[int] = field(default_factory=set, init=False)

    def on_minute(self, session: dt.date, until: dt.datetime) -> None:
        """Odběr plánu zapsal minutu končící v `until`; kontrola jde do úlohy na pozadí."""
        if not is_trading_session(session):
            return  # víkend a svátek plán nemají (#1309)
        if not us_open_ts(session) < until <= settle_ts(session):
            return  # mimo US RTH nic
        if self._task is not None and not self._task.done():
            logger.info(
                "Živý stav plánu %s: předchozí kontrola běží — minuta %s UTC se přeskočí",
                PRODUCT,
                until.strftime("%H:%M"),
            )
            return
        self._task = asyncio.get_running_loop().create_task(self._check(session, until))

    async def _check(self, session: dt.date, until: dt.datetime) -> None:
        try:
            alerts = await asyncio.to_thread(self.run, session, until)
        except Exception:
            logger.exception("Živý stav plánu %s v %s UTC selhal", PRODUCT, until.strftime("%H:%M"))
            return
        if self.publisher is None:
            return
        for payload in alerts:
            try:
                await self.publisher.publish("alerts", payload)
            except Exception:
                logger.exception(
                    "Plán opce QQQ #%d: upozornění %s do zvonku neodešlo",
                    payload["plan_id"],
                    payload["event"],
                )

    def run(self, session: dt.date, until: dt.datetime) -> list[dict[str, Any]]:
        """Blokující průchod (volat přes `to_thread`): nové přechody zapíše a vrátí jako alerty."""
        now = self.clock()
        if is_market_closed(now):
            return []  # zavřený trh = žádné upozornění (AGENTS.md)
        watched: list[tuple[OptionPlanRow, Mapping[str, Any]]] = []
        for row in self.repository.plans_of_day(session):
            if row.id in self._finished or row.evaluated_at is not None:
                continue
            if any(item.get("final") for item in row.live_alerts):
                self._finished.add(row.id)
                continue
            part = live_part(row.plan)
            if part is not None:
                watched.append((row, part))
        if not watched:
            return []
        bars = read_bars(self.derived_dir, PRODUCT, us_open_ts(session) - MINUTE, until)
        quotes = read_option_quotes(self.derived_dir, PRODUCT, session)
        account = self.account()
        alerts: list[dict[str, Any]] = []
        for row, part in watched:
            alerts.extend(self._transitions(row, part, bars, quotes, account, until, now))
        return alerts

    def _transitions(
        self,
        row: OptionPlanRow,
        part: Mapping[str, Any],
        bars: Sequence[PathBar],
        quotes: Sequence[QuoteLike],
        account: AccountParams,
        until: dt.datetime,
        now: dt.datetime,
    ) -> list[dict[str, Any]]:
        result = evaluate_plan(
            part,
            rules=MAIN_RULES,
            bars=bars,
            quotes=quotes,
            max_loss_usd=account.max_loss_usd,
            now=until,
        )
        sent = {item["event"] for item in row.live_alerts}
        transitions = live_transitions(result, until)
        alerts: list[dict[str, Any]] = []
        for transition in transitions:
            if transition.event in sent:
                continue
            # Text před zápisem: chyba sestavení přechod nespotřebuje, zkusí se další minutu
            message = live_message(row.id, part, result, transition, account)
            claimed = self.repository.claim_live_alert(
                row.id,
                {
                    "event": transition.event,
                    "ts": transition.ts.isoformat(),
                    "sent_at": now.isoformat(),
                    "final": transition.final,
                },
            )
            if not claimed:
                continue
            logger.info(
                "Plán opce QQQ #%d: přechod %s (%s UTC), upozornění %.0f s po závěru minuty",
                row.id,
                transition.event,
                transition.ts.strftime("%H:%M"),
                (now - until).total_seconds(),
            )
            alerts.append(
                {
                    "kind": ALERT_KIND,
                    "symbol": PRODUCT,
                    "message": message,
                    "plan_id": row.id,
                    "scenario_id": row.scenario_id,
                    "event": transition.event,
                    "ts": transition.ts.timestamp(),
                }
            )
        if any(transition.final for transition in transitions):
            # Až po zápisu všech přechodů: chyba zápisu nechá plán ke sledování další minutu
            self._finished.add(row.id)
        return alerts
