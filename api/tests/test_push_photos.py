"""Fotky k alertům na Telegram (F4c #1745): výběr snímku, záloha NQ po 3 min, popisek plánu.

Rozsah rozhodl vlastník 16. 11. (#1738: 1 B, 2 A, 3 A, 4 A). Bez sítě a bez matplotlib:
kreslení nahrazuje falešný `render_png` a `scenario_chart_png`, PG je SQLite v `tmp_path`,
čas je falešný (`monotonic` a `sleep`), takže 3 min čekání trvají milisekundy. Plán se
sestaví funkcí enginu z golden vstupů 2. 11. (`engine/tests/golden/option_plan_1741.json`,
K9 A: vstup A od flipu 698,21, TP1 691,99, TP2 690,59). Čísla účtu jsou smyšlená.
"""

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from gexlens_api import option_plan_routes, push_photos
from gexlens_api.option_plan_routes import PlanImages
from gexlens_api.plan_chart import ChartRenderError
from gexlens_api.push_photos import (
    IMAGE_WAIT_S,
    SCENARIO_PHOTO_SYMBOL,
    AlertPhotos,
    plan_caption,
    rebuilt_at_reprice,
)
from gexlens_api.scenario_routes import PNG_MAGIC
from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import AccountParams, build_option_plan, plan_to_json
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.storage.option_plans_store import OptionPlanRow, OptionPlansRepository
from gexlens_engine.storage.scenarios_store import ScenarioRow, ScenariosRepository

GOLDEN = Path(__file__).parents[2] / "engine" / "tests" / "golden" / "option_plan_1741.json"
DATA: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
PLAN_TS = dt.datetime.fromisoformat(DATA["plan_ts"])
RATIO = DATA["ratio_pair"]["nq"] / DATA["ratio_pair"]["qqq"]
DAY = dt.date(2026, 11, 2)
#: Smyšlený účet: popisek fotky ho nesmí nést (ani „% účtu“)
ACCOUNT = AccountParams(account_usd=12_345.0, max_loss_usd=150.0)


@dataclasses.dataclass(frozen=True)
class _ChainRow:
    ts_min: dt.datetime
    strike: float
    right: str
    bid: float | None
    ask: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    oi: float | None


def _chain(raw: list[dict[str, Any]]) -> list[_ChainRow]:
    return [_ChainRow(**{**r, "ts_min": dt.datetime.fromisoformat(r["ts_min"])}) for r in raw]


def _main(*, with_bars: bool) -> dict[str, Any]:
    """Hlavní plán 2. 11. ve tvaru JSON enginu; bez barů = plán při vzniku (bez spouště)."""
    scenario = DATA["scenario"]
    bars = [
        Bar(dt.datetime.fromisoformat(ts), high / RATIO, low / RATIO, close / RATIO)
        for ts, high, low, close in DATA["bars_nq"]
    ]
    return plan_to_json(
        build_option_plan(
            scenario=AutoScenario(
                direction=scenario["direction"],
                entry=scenario["entry"],
                targets=tuple(scenario["targets"]),
                target_labels=tuple(scenario["target_labels"]),
                path=tuple((dt.datetime.fromisoformat(ts), p) for ts, p in scenario["path"]),
            ),
            levels=LevelInputs(**DATA["levels_nq"]),
            ratio=RATIO,
            spot=DATA["spot_nq"] / RATIO,
            chain_0dte=_chain(DATA["chain_0dte"]),
            chain_1dte=_chain(DATA["chain_1dte"]),
            expiry_1dte=dt.date.fromisoformat(DATA["expiry_1dte"]),
            account=ACCOUNT,
            now=PLAN_TS,
            bars=bars if with_bars else (),
        )
    )


def _plan_json(slot: str, main: dict[str, Any] | None, reasons: list[str]) -> dict[str, Any]:
    return {
        "slot": slot,
        "reasons": reasons,
        "levels_nq": DATA["levels_nq"],
        "main": main,
        "shadows": {"entry_b": None, "wick_test": None},
    }


MAIN = _main(with_bars=False)
FULL_PLAN = _plan_json("9:15", MAIN, [])
NO_OPTION = _plan_json("9:15", None, ["ranní snímek neúplný: chybí 659P"])


def _row(plan: dict[str, Any], **values: Any) -> OptionPlanRow:
    fields: dict[str, Any] = {
        "id": 2,
        "scenario_id": 39,
        "day": DAY,
        "created_at": PLAN_TS,
        "rules_version": 1,
        "verdict": "čeká",
        "status": "sestaven",
        "plan": plan,
        "evaluated_at": None,
        "result": None,
        "image_path": None,
    }
    return OptionPlanRow(**(fields | values))


# ── popisek plánu (3 A) ─────────────────────────────────────────────


def test_popisek_planu_pri_vzniku() -> None:
    caption = plan_caption(_row(FULL_PLAN))
    side = "SHORT → put" if MAIN["direction"] == "short" else "LONG → call"
    assert caption.splitlines() == [
        f"Plán QQQ #2 · 2. 11. · plán z 9:15 ET · {side} 1DTE",
        "Vstup A: odmítnutí od flip 698,21 (5min close)",
        "TP1 691,99 / TP2 690,59",
        "Exit 15:45 ET (21:45 Praha)",
        "SL a R:R při spoušti vstupu A",
        f"Stav: {MAIN['verdict']}",
    ]


def test_popisek_nenese_parametry_uctu() -> None:
    """Po spoušti má plán `% účtu`; popisek ho ani velikost účtu nenese (vlastník 16. 11.)."""
    triggered = _main(with_bars=True)
    assert triggered["entry"] is not None
    assert any((v.get("pricing") or {}).get("loss_at_stop_pct") for v in triggered["variants"])
    caption = plan_caption(_row(_plan_json("9:31", triggered, [])))
    assert "účtu" not in caption and "12345" not in caption and "12 345" not in caption
    assert "SL 699,66" in caption and "při spoušti" not in caption


def test_popisek_planu_bez_opce() -> None:
    assert plan_caption(_row(NO_OPTION, status="bez opce", verdict=None)) == (
        "Plán QQQ #2 · 2. 11. · plán z 9:15 ET · bez opce\nDůvod: ranní snímek neúplný: chybí 659P"
    )


# ── AlertPhotos nad SQLite ─────────────────────────────────────────


class FakeTime:
    """`monotonic` a `sleep` bez čekání; `on_sleep` simuluje, co se mezitím stane."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0
        self.on_sleep: Any = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.slept)


@dataclasses.dataclass
class Env:
    data_dir: Path
    scenarios: ScenariosRepository
    plans: OptionPlansRepository
    photos: AlertPhotos
    time: FakeTime
    renders: list[dict[str, Any]]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.db'}")
    scenarios = ScenariosRepository(engine)
    plans = OptionPlansRepository(engine)
    scenarios.ensure_schema()
    plans.ensure_schema()
    renders: list[dict[str, Any]] = []

    def fake_render(spec: dict[str, Any]) -> bytes:
        renders.append(spec)
        return PNG_MAGIC + b"plan"

    monkeypatch.setattr(option_plan_routes, "render_png", fake_render)
    data_dir = tmp_path / "data"
    fake = FakeTime()
    photos = AlertPhotos(
        lambda: scenarios,
        PlanImages(lambda: plans, data_dir, tmp_path / "derived"),
        data_dir,
        tmp_path / "derived",
        now=lambda: PLAN_TS,
        monotonic=fake.monotonic,
        sleep=fake.sleep,
    )
    return Env(data_dir, scenarios, plans, photos, fake, renders)


def _scenario(env: Env, symbol: str = "NQ") -> int:
    return env.scenarios.create(
        symbol=symbol,
        day=DAY,
        created_at=PLAN_TS,
        deadline=DAY,
        deadline_ts=PLAN_TS + dt.timedelta(hours=6),
        entry=28_800.0,
        targets=[28_600.0],
        path=[{"ts": PLAN_TS.isoformat(), "price": 28_800.0}],
        annotation_id=None,
        note=None,
        source="auto",
    )


def _plan(env: Env, plan: dict[str, Any], scenario_id: int = 39) -> int:
    plan_id = env.plans.create(
        scenario_id=scenario_id,
        day=DAY,
        created_at=PLAN_TS,
        rules_version=1,
        verdict="čeká" if plan["main"] else None,
        status="sestaven" if plan["main"] else "bez opce",
        plan=plan,
    )
    assert plan_id is not None
    return plan_id


def _alert(kind: str, **values: Any) -> dict[str, Any]:
    return {"kind": kind, "message": "zpráva zvonku", **values}


def test_heatmapa_z_aplikace_do_3_min(env: Env) -> None:
    scenario_id = _scenario(env)
    heatmap = PNG_MAGIC + b"heatmapa"

    def arrives(slept: float) -> None:
        if slept >= 60 and env.scenarios.get(scenario_id).image_path is None:  # type: ignore[union-attr]
            relative = f"scenarios/NQ/2026-11-02/{scenario_id}.png"
            (env.data_dir / relative).parent.mkdir(parents=True, exist_ok=True)
            (env.data_dir / relative).write_bytes(heatmap)
            env.scenarios.set_image(scenario_id, relative, len(heatmap))

    env.time.on_sleep = arrives
    photo = env.photos(_alert("scenario_created", symbol="NQ", scenario_id=scenario_id))
    assert photo is not None and photo.png == heatmap and photo.error is None
    assert photo.caption == f"Scénář dne NQ #{scenario_id} · heatmapa z aplikace"
    assert photo.filename == f"scenario-{scenario_id}.png"
    assert 60 <= env.time.slept < IMAGE_WAIT_S


def test_bez_heatmapy_serverovy_graf_nq_po_3_min(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_id = _scenario(env)
    plan_id = _plan(env, FULL_PLAN, scenario_id=scenario_id)
    calls: list[tuple[ScenarioRow, OptionPlanRow | None, dt.datetime]] = []

    def fake_chart(
        scenario: ScenarioRow, plan: OptionPlanRow | None, _derived: Path, now: dt.datetime
    ) -> bytes:
        calls.append((scenario, plan, now))
        return PNG_MAGIC + b"nq"

    monkeypatch.setattr(push_photos, "scenario_chart_png", fake_chart)
    photo = env.photos(_alert("scenario_created", symbol="NQ", scenario_id=scenario_id))
    assert env.time.slept == IMAGE_WAIT_S == 180.0
    assert photo is not None and photo.png == PNG_MAGIC + b"nq"
    assert photo.caption == (
        f"Scénář dne NQ #{scenario_id} · serverový graf (heatmapa z aplikace do 3 min nedorazila)"
    )
    ((scenario, plan, now),) = calls
    assert scenario.id == scenario_id and plan is not None and plan.id == plan_id
    assert now == PLAN_TS  # bary do okamžiku kreslení


def test_selhani_serveroveho_grafu_je_duvod(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_id = _scenario(env)

    def broken(*_args: Any) -> bytes:
        raise ChartRenderError("podproces kreslení skončil kódem 1: ValueError: x")

    monkeypatch.setattr(push_photos, "scenario_chart_png", broken)
    photo = env.photos(_alert("scenario_created", symbol="NQ", scenario_id=scenario_id))
    assert photo is not None and photo.png is None
    assert photo.error == (
        "serverový graf se nenakreslil: podproces kreslení skončil kódem 1: ValueError: x"
    )
    assert "serverový graf" in photo.caption


def test_scenar_es_a_vysledky_bez_fotky(env: Env) -> None:
    """Vlastník 16. 11. (4 A): fotka jen ke scénáři NQ; k výsledkům fotka nejde (2 A)."""
    assert SCENARIO_PHOTO_SYMBOL == "NQ"
    es = _scenario(env, symbol="ES")
    assert env.photos(_alert("scenario_created", symbol="ES", scenario_id=es)) is None
    assert env.photos(_alert("scenario_result", symbol="NQ", scenario_id=es)) is None
    plan_id = _plan(env, FULL_PLAN)
    assert env.photos(_alert("option_plan_result", symbol="QQQ", plan_id=plan_id)) is None
    assert env.time.slept == 0 and env.renders == []


def test_konstanta_nq_odpovida_enginu() -> None:
    from gexlens_engine.tasty.plan_chain import REFERENCE

    assert SCENARIO_PHOTO_SYMBOL == REFERENCE


def test_plan_pri_vzniku_fotka_pres_png_endpointu(env: Env) -> None:
    plan_id = _plan(env, FULL_PLAN)
    photo = env.photos(_alert("option_plan_created", symbol="QQQ", plan_id=plan_id))
    assert photo is not None and photo.png == PNG_MAGIC + b"plan" and photo.error is None
    assert photo.filename == f"plan-{plan_id}-0915.png"
    assert photo.caption == plan_caption(env.plans.get(plan_id))  # type: ignore[arg-type]
    row = env.plans.get(plan_id)
    relative = f"scenarios/QQQ/2026-11-02/plan-{plan_id}-0915.png"
    assert row is not None and (row.image_path, row.image_bytes) == (relative, len(photo.png))
    # Endpoint i další fotka čtou týž PNG z disku, nekreslí znovu
    assert env.photos(_alert("option_plan_created", symbol="QQQ", plan_id=plan_id)) is not None
    assert len(env.renders) == 1


def test_preceneni_uplneho_planu_bez_fotky(env: Env) -> None:
    plan_id = _plan(env, FULL_PLAN)
    assert env.plans.record_repricing(
        plan_id, {"slot": "9:31", "reasons": [], "chain": None, "prices": {"main": []}}
    )
    row = env.plans.get(plan_id)
    assert row is not None and not rebuilt_at_reprice(row)
    assert env.photos(_alert("option_plan_repriced", symbol="QQQ", plan_id=plan_id)) is None
    assert env.renders == []


def test_plan_sestaveny_v_931_ma_fotku(env: Env) -> None:
    plan_id = _plan(env, NO_OPTION)
    rebuilt = _plan_json("9:31", MAIN, [])
    assert env.plans.record_repricing(
        plan_id,
        {"slot": "9:31", "reasons": [], "chain": None, "prices": None, "plan": rebuilt},
        status="sestaven",
        verdict="čeká",
    )
    photo = env.photos(_alert("option_plan_repriced", symbol="QQQ", plan_id=plan_id))
    assert photo is not None and photo.png is not None
    assert photo.filename == f"plan-{plan_id}-0931.png"
    assert photo.caption.startswith(f"Plán QQQ #{plan_id} · 2. 11. · plán z 9:31 ET · ")


def test_plan_bez_opce_ma_fotku_s_duvodem(env: Env) -> None:
    plan_id = _plan(env, NO_OPTION)
    photo = env.photos(_alert("option_plan_created", symbol="QQQ", plan_id=plan_id))
    assert photo is not None and photo.png is not None
    assert photo.caption.endswith("bez opce\nDůvod: ranní snímek neúplný: chybí 659P")


def test_chyba_kresleni_planu_vrati_popisek_a_duvod(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_spec: dict[str, Any]) -> bytes:
        raise ChartRenderError("kreslení nedoběhlo do 60 s")

    monkeypatch.setattr(option_plan_routes, "render_png", broken)
    plan_id = _plan(env, FULL_PLAN)
    photo = env.photos(_alert("option_plan_created", symbol="QQQ", plan_id=plan_id))
    assert photo is not None and photo.png is None
    assert photo.error == f"Kreslení PNG plánu {plan_id} selhalo: kreslení nedoběhlo do 60 s"
    assert photo.caption.startswith(f"Plán QQQ #{plan_id} · 2. 11.")
    row = env.plans.get(plan_id)
    assert row is not None and row.image_path is None  # žádný prázdný obrázek


def test_chybejici_plan_je_duvod(env: Env) -> None:
    photo = env.photos(_alert("option_plan_created", symbol="QQQ", plan_id=999))
    assert photo is not None and photo.png is None and photo.error == "plán v databázi není"


def test_app_preda_telegramu_zdroj_fotek(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zapojení v `create_app`: Telegram dostane `AlertPhotos` nad PNG plánu endpointu."""
    from gexlens_api import main
    from gexlens_engine.config import Settings

    seen: dict[str, Any] = {}
    real = main.TelegramPush

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(main, "TelegramPush", spy)
    main.create_app(
        Settings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.db'}",
        )
    )
    assert isinstance(seen["photos"], AlertPhotos)
