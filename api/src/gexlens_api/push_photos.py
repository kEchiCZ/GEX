"""Fotky k alertům na Telegram (F4c #1745, ADR-0062 Q2b a K1 A).

Zdroj fotek pro `push_telegram.TelegramPush` (`PhotoSource`): k alertu vrátí snímek, nebo
None, když k němu snímek nepatří. Nic nepočítá, jen vybírá uložené hodnoty a soubory.
Rozsah rozhodl vlastník 16. 11. (#1738 Rozhodnuto, „1. B, 2. A, 3. A, 4. A“):

- **Scénář dne NQ** (`scenario_created`, jen NQ, 4 A): heatmapa z aplikace
  (`PUT /scenarios/{id}/image`), když dorazí do 3 min od alertu (2. 11. dorazila za 59 s),
  jinak serverový graf NQ `plan_chart.scenario_chart_png` (F4b #1744). Heatmapa, která
  přijde až po serverovém grafu, se už neposílá.
- **Plán opce QQQ** (2 A): při vzniku (`option_plan_created`) vždy, při přecenění
  (`option_plan_repriced`) jen u plánu bez opce z 9:15 sestaveného celý v 9:31, protože
  fotka z 9:15 žádnou opci neukazuje. K výsledku (`option_plan_result`) fotka nejde, PNG stavu
  „vyhodnoceno“ zatím neukazuje průběh (#1822). PNG plánu jde přes `PlanImages`, tedy
  tutéž cestu jako `GET /option-plans/{id}/image`: nakreslí se jednou a jde do `image_bytes`.
- **Popisek plánu** (3 A) z platného plánu řádku (`effective_plan`): směr, vstup A, TP1/TP2,
  exit a „SL a R:R při spoušti“ (K12 A). Parametry účtu ani „% účtu“ nenese. Když snímek
  selže, jde popisek v textu s důvodem (`push_telegram.photo_failure_message`).
"""

import datetime as dt
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gexlens_api.option_plan_routes import PlanImageError, PlanImages
from gexlens_api.plan_chart import ChartRenderError, image_state, scenario_chart_png
from gexlens_api.push_telegram import Photo
from gexlens_engine.compute.option_plan import LEVEL_LABELS
from gexlens_engine.compute.option_plan import _cz as cz
from gexlens_engine.storage.option_plans_store import (
    REPRICING_KEY,
    OptionPlanRow,
    effective_plan,
)
from gexlens_engine.storage.scenarios_store import ScenarioRow, ScenariosRepository

logger = logging.getLogger(__name__)

#: Fotka scénáře jen k NQ (vlastník 16. 11.: 4 A) = `tasty/plan_chain.REFERENCE` (import by do
#: API tahal moduly tasty a IBKR; shodu hlídá test)
SCENARIO_PHOTO_SYMBOL = "NQ"
#: Na heatmapu z aplikace se čeká 3 min od alertu (ADR-0062 Odvozeno, #1745)
IMAGE_WAIT_S = 180.0
#: Krok čtení řádku scénáře při čekání na heatmapu
IMAGE_POLL_S = 5.0
ET_TZ = ZoneInfo("America/New_York")
PRAGUE_TZ = ZoneInfo("Europe/Prague")


def rebuilt_at_reprice(row: OptionPlanRow) -> bool:
    """Plán bez opce z 9:15 sestavený celý v 9:31 (`plan.repricing.plan`, ADR-0062 K4 bod 10)."""
    repricing = row.plan.get(REPRICING_KEY)
    return isinstance(repricing, Mapping) and repricing.get("plan") is not None


def plan_caption(row: OptionPlanRow) -> str:
    """Popisek fotky plánu z uložených hodnot (vlastník 16. 11.: 3 A); nic nepřepočítává."""
    effective = effective_plan(row.plan)
    main: Mapping[str, Any] | None = effective["main"]
    head = f"Plán QQQ #{row.id} · {row.day.day}. {row.day.month}. · plán z {effective['slot']} ET"
    if main is None:
        gaps = [*effective["reasons"], *(effective["repricing_reasons"] or ())]
        lines = [f"{head} · bez opce"]
        if gaps:
            lines.append(f"Důvod: {'; '.join(gaps)}")
        return "\n".join(lines)
    side = "SHORT" if main["direction"] == "short" else "LONG"
    right = "call" if main["right"] == "C" else "put"
    lines = [f"{head} · {side} → {right} 1DTE"]
    level_a = main.get("level_a")
    if level_a:
        name, price = str(level_a[0]), float(level_a[1])
        label = LEVEL_LABELS.get(name, name)
        lines.append(f"Vstup A: odmítnutí od {label} {cz(price)} (5min close)")
    else:
        lines.append("Vstup A: bez úrovně (viz stav)")
    targets = [float(target) for target in main.get("targets") or ()]
    if targets:
        lines.append(" / ".join(f"TP{index} {cz(t)}" for index, t in enumerate(targets, 1)))
    exit_ts = dt.datetime.fromisoformat(main["exit_ts"])
    lines.append(
        f"Exit {exit_ts.astimezone(ET_TZ):%H:%M} ET ({exit_ts.astimezone(PRAGUE_TZ):%H:%M} Praha)"
    )
    entry = main.get("entry")
    if entry:
        lines.append(
            f"Vstup {entry['kind']} za {cz(float(entry['price']))}, SL {cz(entry['stop'])}"
        )
    else:
        lines.append("SL a R:R při spoušti vstupu A")
    lines.append(f"Stav: {main['verdict']}")
    return "\n".join(lines)


class AlertPhotos:
    """Snímek k alertu pro Telegram; volá se ve vlákně odeslání a smí čekat (heatmapa).

    `scenarios` vrací úložiště scénářů (lazy, vzor routerů), `plan_images` je táž instance
    jako u endpointu PNG plánu, `data_dir` kořen snímků, `derived_dir` partice barů NQ.
    """

    def __init__(
        self,
        scenarios: Callable[[], ScenariosRepository],
        plan_images: PlanImages,
        data_dir: Path,
        derived_dir: Path,
        *,
        now: Callable[[], dt.datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wait_s: float = IMAGE_WAIT_S,
        poll_s: float = IMAGE_POLL_S,
    ) -> None:
        self._scenarios_factory = scenarios
        self._scenarios: list[ScenariosRepository] = []
        self._plan_images = plan_images
        self._data_dir = data_dir
        self._derived_dir = derived_dir
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._monotonic = monotonic
        self._sleep = sleep
        self._wait_s = wait_s
        self._poll_s = poll_s

    def __call__(self, payload: dict[str, Any]) -> Photo | None:
        kind = payload.get("kind")
        if kind == "scenario_created":
            if payload.get("symbol") != SCENARIO_PHOTO_SYMBOL:
                return None
            return self._scenario(int(payload["scenario_id"]))
        if kind == "option_plan_created":
            return self._plan(int(payload["plan_id"]), repriced=False)
        if kind == "option_plan_repriced":
            return self._plan(int(payload["plan_id"]), repriced=True)
        return None

    def _scenarios_repo(self) -> ScenariosRepository:
        if not self._scenarios:
            repository = self._scenarios_factory()
            repository.ensure_schema()
            self._scenarios.append(repository)
        return self._scenarios[0]

    # ── scénář dne NQ ─────────────────────────────────────────────
    def _scenario(self, scenario_id: int) -> Photo:
        """Heatmapa z aplikace do `wait_s` od alertu, jinak serverový graf."""
        filename = f"scenario-{scenario_id}.png"
        deadline = self._monotonic() + self._wait_s
        while True:
            row = self._scenarios_repo().get(scenario_id)
            if row is None:
                return Photo(filename, f"Scénář dne #{scenario_id}", error="scénář v databázi není")
            if row.image_path:
                return self._heatmap(row, filename)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._server_chart(row, filename)
            self._sleep(min(self._poll_s, remaining))

    def _heatmap(self, row: ScenarioRow, filename: str) -> Photo:
        caption = f"Scénář dne {row.symbol} #{row.id} · heatmapa z aplikace"
        assert row.image_path is not None
        try:
            png = (self._data_dir / row.image_path).read_bytes()
        except OSError as exc:
            return Photo(filename, caption, error=f"heatmapa nejde přečíst ({type(exc).__name__})")
        return Photo(filename, caption, png=png)

    def _server_chart(self, row: ScenarioRow, filename: str) -> Photo:
        caption = (
            f"Scénář dne {row.symbol} #{row.id} · serverový graf "
            f"(heatmapa z aplikace do {self._wait_s / 60:g} min nedorazila)"
        )
        try:
            plan = self._plan_images.repo().get_for_scenario(row.id)
            png = scenario_chart_png(row, plan, self._derived_dir, self._now())
        except ChartRenderError as exc:
            return Photo(filename, caption, error=f"serverový graf se nenakreslil: {exc}")
        except Exception as exc:
            # Čtení barů nebo plánu (vadná partice, PG): důvod do zprávy i do logu
            logger.exception("Serverový graf scénáře #%s pro Telegram selhal", row.id)
            reason = f"{type(exc).__name__}: {exc}"
            return Photo(filename, caption, error=f"serverový graf se nenakreslil: {reason}")
        return Photo(filename, caption, png=png)

    # ── plán opce QQQ ─────────────────────────────────────────────
    def _plan(self, plan_id: int, *, repriced: bool) -> Photo | None:
        row = self._plan_images.repo().get(plan_id)
        if row is None:
            return Photo(
                f"plan-{plan_id}.png", f"Plán QQQ #{plan_id}", error="plán v databázi není"
            )
        if repriced and not rebuilt_at_reprice(row):
            return None
        filename = f"plan-{row.id}-{image_state(row)}.png"
        try:
            caption = plan_caption(row)
        except Exception as exc:
            # Nečekaný tvar JSON plánu: fotka jde dál, popisek řekne proč chybí
            logger.exception("Popisek fotky plánu QQQ #%s se nesestavil", row.id)
            caption = f"Plán QQQ #{row.id} (popisek nejde sestavit: {type(exc).__name__})"
        try:
            png = self._plan_images.path(plan_id).read_bytes()
        except PlanImageError as exc:
            return Photo(filename, caption, error=str(exc))
        except OSError as exc:
            return Photo(filename, caption, error=f"PNG plánu nejde přečíst ({type(exc).__name__})")
        return Photo(filename, caption, png=png)
