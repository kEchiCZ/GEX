"""Vyhodnocení plánu opce QQQ po close (F3 #1748, ADR-0062 K3 a K10 B); vzor `scenarios.py`.

Jednou po settle každé obchodní seance (+ `SETTLE_GRACE`) projde plány bez výsledku ze seancí
do dnešní včetně. Víkend a svátek settle nemají (`compute/settle.is_trading_session`, #1309).
Každý plán vyhodnotí čistá funkce `compute/option_plan.evaluate_option_plan`:
- platný plán řádku vybírá `effective_plan` (K4 bod 10): hlavní plán i stíny K10 B;
- 1m bary QQQ z `derived/QQQ/bars` za celou seanci plánu od otevření Globexu (17:00 CT
  předchozího dne), tedy i z UTC partice D−1 (AGENTS.md „Obchodní den“). Funkce si z nich
  vezme RTH;
- kotace kontraktů z `derived/QQQ/option_quotes/{seance}.parquet` (F1b #1747, klíč = seance).

Chybějící bary nebo kotace jsou ve výsledku mezera, ne odhad. Výsledek jde jednou do
`option_plans.result` (`record_result`, druhý běh nic nezmění) a alertem `option_plan_result`
do zvonku. Vyhodnotí se každý plán. Track record počítá až od `TRACK_RECORD_SINCE`
(Rozhodnuto 16. 11.: A), zpráva plán mimo něj označí.

Rozpočet P4 ze Settings (`option_plan_max_loss_usd`) se čte v okamžiku vyhodnocení jen pro
třídu varianty `v rozpočtu` / `nad rozpočtem` / `nenastaven` (Rozhodnuto 16. 11.). Hodnota
se neukládá a do logu ani do zprávy nejde. Velikost účtu vyhodnocení nečte.

Minutový cyklus na nic nečeká (#1337, #1646): volá se z úloh po settle pipeline NQ (#1701)
a čtení partic i PG běží ve vlákně.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gexlens_engine.compute.option_plan import (
    TRACK_RECORD_SINCE,
    AccountParams,
    _cz,
    evaluate_option_plan,
)
from gexlens_engine.compute.settle import (
    ET_TZ,
    is_trading_session,
    session_bounds,
    settle_ts,
    trading_session_date,
)
from gexlens_engine.storage.option_plans_store import (
    OptionPlanRow,
    OptionPlansRepository,
    effective_plan,
)
from gexlens_engine.storage.parquet_store import read_bars, read_option_quotes
from gexlens_engine.tasty.plan_chain import PRODUCT

logger = logging.getLogger(__name__)

#: Rezerva po settle jako u scénářů: poslední minuty kotací a barů jsou zapsané
SETTLE_GRACE = dt.timedelta(minutes=15)
ALERT_KIND = "option_plan_result"


def _et(value: str) -> str:
    return dt.datetime.fromisoformat(value).astimezone(ET_TZ).strftime("%H:%M")


def _usd(value: float | None) -> str:
    return "bez kotace" if value is None else f"{'+' if value >= 0 else '−'}{_cz(abs(value), 0)} $"


def _variant_text(variant: Mapping[str, Any]) -> str:
    name = f"{variant['role']} {'call' if variant['right'] == 'C' else 'put'} {variant['strike']:g}"
    if variant["kind"] == "bez kotace":
        return f"{name}: bez kotace při vstupu"
    value = variant["pricing"]["rr"]
    rr = f"R:R {_cz(value)}" if value is not None else "R:R nejde určit"
    first, hold = variant["exit_tp1"], variant["exit_hold"]
    if first is None or hold is None:
        legs = ""
    elif first == hold:
        legs = f", {first['event']} {_usd(first['pnl_usd'])}"
    else:
        legs = (
            f", výstup v TP1 {_usd(first['pnl_usd'])}, "
            f"držení do {hold['event']} {_usd(hold['pnl_usd'])}"
        )
    budget = ", nad rozpočtem" if variant["budget"] == "nad rozpočtem" else ""
    return f"{name}: {variant['kind']} ({rr}{budget}){legs}"


def result_message(row: OptionPlanRow, result: Mapping[str, Any]) -> str:
    """Zpráva do zvonku: stav hlavního plánu, průběh a P&L variant na kontrakt (časy v ET)."""
    main = result["main"]
    head = f"Plán QQQ #{row.id} ({row.day.day}. {row.day.month}.) vyhodnocen: "
    if main["status"] != "spuštěno":
        body = f"{main['status']} — {'; '.join(main['reasons'])}"
    else:
        entry = main["entry"]
        path = ", ".join(f"{event[0]} {_et(event[1])}" for event in _events(main)[1:])
        body = (
            f"vstup {entry['kind']} {_et(entry['ts'])} ET za {_cz(entry['price'])}, "
            f"SL {_cz(entry['stop'])} → {main['outcome']}"
            + (f" ({path} ET)" if path else "")
            + "; "
            + "; ".join(_variant_text(variant) for variant in main["variants"])
        )
    counted = TRACK_RECORD_SINCE is not None and row.day >= TRACK_RECORD_SINCE
    return head + body + ("" if counted else ". Do track recordu se nepočítá.")


def _events(part: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(str(event["kind"]), str(event["ts"])) for event in part["events"]]


@dataclass
class OptionPlanCollector:
    """Jednou po settle vyhodnotí plány QQQ bez výsledku (F3 #1748)."""

    repository: OptionPlansRepository
    derived_dir: Path
    #: Parametry účtu ze `settings` (blokující čtení PG, volá se ve vlákně)
    account: Callable[[], AccountParams]
    publisher: Any = None

    _evaluated_for: dt.date | None = field(default=None, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        if not is_trading_session(session):
            return  # víkend a svátek: settle není, nic se nevyhodnocuje (#1309)
        if now < settle_ts(session) + SETTLE_GRACE or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus za seanci i při chybě
        done = await asyncio.to_thread(self.run, now)
        if self.publisher is None:
            return
        for row, result in done:
            try:
                await self.publisher.publish(
                    "alerts",
                    {
                        "kind": ALERT_KIND,
                        "symbol": PRODUCT,
                        "message": result_message(row, result),
                        "plan_id": row.id,
                        "scenario_id": row.scenario_id,
                        "ts": now.timestamp(),
                    },
                )
            except Exception:
                logger.exception("Plán opce QQQ #%d: alert výsledku do zvonku neodešel", row.id)

    def evaluate(self, row: OptionPlanRow, max_loss_usd: float | None) -> dict[str, Any]:
        """JSON výsledku plánu z partic seance plánu; blokující čtení Parquet."""
        effective = effective_plan(row.plan)
        start, _ = session_bounds(row.day)
        bars = read_bars(self.derived_dir, PRODUCT, start, settle_ts(row.day))
        quotes = read_option_quotes(self.derived_dir, PRODUCT, row.day)
        reasons = [*effective["reasons"], *(effective["repricing_reasons"] or [])]
        return evaluate_option_plan(
            effective["main"],
            effective["shadows"],
            reasons=reasons,
            bars=bars,
            quotes=quotes,
            max_loss_usd=max_loss_usd,
        )

    def run(self, now: dt.datetime) -> list[tuple[OptionPlanRow, dict[str, Any]]]:
        """Blokující průchod (volat přes `to_thread`). Vrací vyhodnocené plány s výsledkem."""
        session = trading_session_date(now)
        rows = [
            row
            for row in self.repository.pending(session)
            if settle_ts(row.day) + SETTLE_GRACE <= now
        ]
        if not rows:
            return []
        max_loss_usd = self.account().max_loss_usd
        done: list[tuple[OptionPlanRow, dict[str, Any]]] = []
        for row in rows:
            try:
                result = self.evaluate(row, max_loss_usd)
            except Exception:
                # Vadný JSON plánu nebo partice: viditelně v logu, výsledek se nezapíše
                # a plán se zkusí znovu po dalším close
                logger.exception("Vyhodnocení plánu opce QQQ #%d (%s) selhalo", row.id, row.day)
                continue
            if not self.repository.record_result(row.id, result, now):
                logger.info("Plán opce QQQ #%d už je vyhodnocený — nic se nemění", row.id)
                continue
            main = result["main"]
            logger.info(
                "Plán opce QQQ #%d (%s) vyhodnocen: %s%s, barů %d/%d",
                row.id,
                row.day.isoformat(),
                main["status"],
                f" ({main['outcome']})" if main["outcome"] else "",
                main["bars"],
                main["bars_expected"],
            )
            done.append((row, result))
        return done
