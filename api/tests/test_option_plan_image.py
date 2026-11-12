"""PNG plánu QQQ a záložní graf NQ (F4b #1744, ADR-0062 K1 A).

Popis grafu se testuje bez matplotlib na golden plánu 2. 11. (K9 A: vstup A 16:35 SEČ za
697,99 od flipu 698,21, SL 699,66, TP1 691,99, TP2 690,59), plán se sestaví funkcí enginu
z golden vstupů `engine/tests/golden/option_plan_1741.json`. Skutečné kreslení v podprocesu
potřebuje matplotlib (v CI je v závislostech API); bez něj se ty případy přeskočí. Testy
běží bez sítě: podproces čte jen stdin. Čísla účtu jsou smyšlená.
"""

import dataclasses
import datetime as dt
import importlib.util
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from gexlens_api import chart_render, option_plan_routes
from gexlens_api.main import create_app
from gexlens_api.plan_chart import (
    PLAN_SYMBOL,
    ChartRenderError,
    image_state,
    plan_chart_spec,
    render_png,
    scenario_chart_spec,
)
from gexlens_api.scenario_routes import PNG_MAGIC, ImageDiskWatch
from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import (
    SHADOW_B,
    SHADOW_WICK,
    AccountParams,
    build_option_plan,
    plan_to_json,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar as UnderlyingBar
from gexlens_engine.storage.option_plans_store import OptionPlanRow, OptionPlansRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter, StoredBar
from gexlens_engine.storage.scenarios_store import ScenarioRow

GOLDEN = Path(__file__).parents[2] / "engine" / "tests" / "golden" / "option_plan_1741.json"
DATA: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
PLAN_TS = dt.datetime.fromisoformat(DATA["plan_ts"])
RATIO = DATA["ratio_pair"]["nq"] / DATA["ratio_pair"]["qqq"]
DAY = dt.date(2026, 11, 2)
HAS_MPL = importlib.util.find_spec("matplotlib") is not None
needs_mpl = pytest.mark.skipif(not HAS_MPL, reason="matplotlib chybí (v CI je v závislostech API)")


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


BARS_QQQ = [
    Bar(dt.datetime.fromisoformat(ts), high / RATIO, low / RATIO, close / RATIO)
    for ts, high, low, close in DATA["bars_nq"]
]


def _golden_plan_json() -> dict[str, Any]:
    """JSON `option_plans.plan` golden plánu 2. 11. ve tvaru enginu (plán celý z 9:31, K9 A)."""
    scenario = DATA["scenario"]
    kwargs: dict[str, Any] = {
        "scenario": AutoScenario(
            direction=scenario["direction"],
            entry=scenario["entry"],
            targets=tuple(scenario["targets"]),
            target_labels=tuple(scenario["target_labels"]),
            path=tuple((dt.datetime.fromisoformat(ts), p) for ts, p in scenario["path"]),
        ),
        "levels": LevelInputs(**DATA["levels_nq"]),
        "ratio": RATIO,
        "spot": DATA["spot_nq"] / RATIO,
        "chain_0dte": _chain(DATA["chain_0dte"]),
        "chain_1dte": _chain(DATA["chain_1dte"]),
        "expiry_1dte": dt.date.fromisoformat(DATA["expiry_1dte"]),
        "account": AccountParams(account_usd=12_345.0, max_loss_usd=150.0),
        "now": PLAN_TS,
        "bars": BARS_QQQ,
    }
    return {
        "slot": "9:31",
        "reasons": [],
        "levels_nq": DATA["levels_nq"],
        "main": plan_to_json(build_option_plan(**kwargs)),
        "shadows": {
            "entry_b": plan_to_json(build_option_plan(**kwargs, rules=SHADOW_B)),
            "wick_test": plan_to_json(build_option_plan(**kwargs, rules=SHADOW_WICK)),
        },
    }


GOLDEN_PLAN = _golden_plan_json()


def _row(plan: dict[str, Any], **values: Any) -> OptionPlanRow:
    fields: dict[str, Any] = {
        "id": 7,
        "scenario_id": 29,
        "day": DAY,
        "created_at": PLAN_TS,
        "rules_version": 1,
        "verdict": "obchodovat",
        "status": "sestaven",
        "plan": plan,
        "evaluated_at": None,
        "result": None,
        "image_path": None,
    }
    return OptionPlanRow(**(fields | values))


def _stored(bars: list[Bar]) -> list[StoredBar]:
    return [StoredBar(b.ts, b.close, b.high, b.low, b.close, 0.0, "tasty") for b in bars]


def _line(spec: dict[str, Any], role: str) -> list[tuple[float, str]]:
    return [(round(item["y"], 2), item["label"]) for item in spec["lines"] if item["role"] == role]


def _png_size(png: bytes) -> tuple[int, int]:
    """Šířka a výška z hlavičky IHDR."""
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


# --- popis grafu (bez matplotlib) -------------------------------------------------------


def test_popis_grafu_golden_2_11_z_ulozenych_hodnot() -> None:
    spec = plan_chart_spec(_row(GOLDEN_PLAN), _stored(BARS_QQQ))
    assert spec["title"] == "Plán QQQ 2. 11. · SHORT → put 1DTE · plán z 9:31 ET"
    assert spec["subtitle"] == "verdikt: obchodovat"
    assert _line(spec, "entry") == [(698.21, "vstup A od flip 698,21")]
    assert _line(spec, "stop") == [(699.66, "SL 699,66")]
    assert _line(spec, "target") == [(691.99, "TP1 = ONL 691,99"), (690.59, "TP2 = PDC 690,59")]
    assert _line(spec, "gex") == [(700.0, "GEX top 700 (208 M)")]
    levels = [label for _, label in _line(spec, "level")]
    assert "call wall 702,60" in levels and "PDL 683,42" in levels
    assert not any(label.startswith(("flip", "ONL", "PDC")) for label in levels)  # bez dvojí čáry
    entry = next(m for m in spec["markers"] if m["label"].startswith("vstup A"))
    assert entry["label"] == "vstup A 16:35 za 697,99"  # čas v zóně grafu (SEČ)
    path = spec["path"]["points"]
    assert path[0] == [GOLDEN_PLAN["main"]["created_at"], GOLDEN_PLAN["main"]["scenario_entry"]]
    assert [p[1] for p in path[1:]] == GOLDEN_PLAN["main"]["targets"]
    assert len(spec["price"]["points"]) == len(BARS_QQQ)
    notes = "\n".join(spec["notes"])
    assert "Hlavní: put 687 exp. 3. 11." in notes and "Záložní: put 686" in notes
    assert "P&L na kontrakt: SL −31 $" in notes and "R:R 4,61" in notes
    assert "−−" not in notes  # ztráta má jedno znaménko
    assert "P5: Podmíněný příkaz v TWS: když QQQ ≥ 699,66 → prodat put 687." in notes
    assert spec["x_max"] > spec["vlines"][1]["x"]  # exit je v grafu


def test_popis_bez_opce_ukaze_duvody_a_zadne_urovne() -> None:
    plan = {**GOLDEN_PLAN, "slot": "9:15", "main": None, "reasons": ["ranní snímek NEÚPLNÝ"]}
    spec = plan_chart_spec(_row(plan, status="bez opce", verdict=None), [])
    assert spec["title"].endswith("plán z 9:15 ET · bez opce")
    assert spec["lines"] == [] and spec["markers"] == [] and "path" not in spec
    assert "Mezery 9:15 ET: ranní snímek NEÚPLNÝ" in spec["notes"]
    assert spec["notes"][-1] == "Bary QQQ: v uložených partiích zatím nejsou."


def test_plan_sestaveny_v_9_31_kresli_repricing_plan_a_ceny_9_31_k_planu_9_15() -> None:
    """Platný plán vybírá `effective_plan` (K4 bod 10), popis nemá vlastní pravidlo."""
    stored = {
        **GOLDEN_PLAN,
        "slot": "9:15",
        "main": None,
        "reasons": ["ranní snímek 9:15 ET nedorazil"],
        "repricing": {
            "slot": "9:31",
            "reasons": [],
            "chain": {},
            "prices": None,
            "plan": GOLDEN_PLAN,
        },
    }
    rebuilt = plan_chart_spec(_row(stored), [])
    assert rebuilt["title"].endswith("plán z 9:31 ET") and _line(rebuilt, "stop")
    assert "Mezery 9:15 ET: ranní snímek 9:15 ET nedorazil" in rebuilt["notes"]
    main = GOLDEN_PLAN["main"]
    price = {
        **main["variants"][0],
        "bid": 1.5,
        "ask": 1.54,
        "quote_ts": "2026-11-02T14:31:00+00:00",
    }
    repriced = {
        **GOLDEN_PLAN,
        "slot": "9:15",
        "repricing": {
            "slot": "9:31",
            "reasons": [],
            "chain": {},
            "prices": {"main": [price]},
            "plan": None,
        },
    }
    notes = plan_chart_spec(_row(repriced), [])["notes"]
    assert any(n.startswith("Přecenění 15:31: hlavní put 687 · bid/ask 1,50/1,54") for n in notes)


def test_stav_png_9_15_9_31_vyhodnoceno() -> None:
    assert image_state(_row(GOLDEN_PLAN)) == "0915"
    repriced = {**GOLDEN_PLAN, "repricing": {"reasons": ["snímek 9:31 nedorazil"], "plan": None}}
    assert image_state(_row(repriced)) == "0931"
    evaluated = _row(repriced, evaluated_at=PLAN_TS + dt.timedelta(hours=8))
    assert image_state(evaluated) == "vyhodnoceno"


def test_podklad_planu_je_produkt_odberu() -> None:
    from gexlens_engine.tasty.plan_chain import PRODUCT

    assert PLAN_SYMBOL == PRODUCT


def _scenario_row() -> ScenarioRow:
    scenario = DATA["scenario"]
    return ScenarioRow(
        id=29,
        symbol="NQ",
        day=DAY,
        created_at=dt.datetime(2026, 11, 2, 14, 15, tzinfo=dt.UTC),
        deadline=DAY,
        deadline_ts=dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC),
        entry=scenario["entry"],
        targets=list(scenario["targets"]),
        path=[{"ts": ts, "price": price} for ts, price in scenario["path"]],
        annotation_id=None,
        note=None,
        image_path=None,
        image_bytes=0,
        evaluated_at=None,
        result=None,
        source="auto",
        rationale={"verdict": "short", "score": -5, "targets": list(scenario["target_labels"])},
    )


def test_zalozni_graf_nq_vstup_cile_cesta_urovne() -> None:
    spec = scenario_chart_spec(_scenario_row(), DATA["levels_nq"], [])
    assert spec["title"] == "Scénář dne NQ 2. 11. · SHORT · skóre −5"
    assert _line(spec, "entry") == [(28753.75, "vstup 28753,75")]
    assert [label for _, label in _line(spec, "target")] == [
        "cíl 1 ONL 28587,00",
        "cíl 2 PDC 28529,00",
    ]
    assert any(label.startswith("flip") for _, label in _line(spec, "level"))
    assert len(spec["path"]["points"]) == len(DATA["scenario"]["path"])
    assert spec["x_min"] == "2026-11-01T23:00:00+00:00"  # Globex 17:00 CT předchozího dne
    without = scenario_chart_spec(_scenario_row(), None, [])
    assert _line(without, "level") == []
    assert "Úrovně NQ: scénář nemá plán QQQ, úrovně nejsou uložené." in without["notes"]


# --- podproces kreslení -----------------------------------------------------------------


def test_neplatny_popis_je_chyba_s_duvodem() -> None:
    """Tvar popisu se ověří před importem matplotlib: kód 2 a důvod, i bez matplotlib."""
    with pytest.raises(
        ChartRenderError, match="kódem 2: neplatný popis grafu: lines: neznámá role"
    ):
        render_png(
            {
                **plan_chart_spec(_row(GOLDEN_PLAN), []),
                "lines": [{"y": 1, "label": "x", "role": "?"}],
            }
        )
    with pytest.raises(chart_render.SpecError, match="čas bez zóny"):
        chart_render.parse(
            {"tz": "UTC", "x_min": "2026-11-02T14:00:00", "x_max": "2026-11-02T15:00:00"}
        )


def test_casovy_limit_a_ne_png_jsou_chyba(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("chart_render", 60.0)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ChartRenderError, match="nedoběhlo do 60 s"):
        render_png({})

    def not_png(*_args: Any, **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", not_png)
    with pytest.raises(ChartRenderError, match="nevrátil PNG"):
        render_png({})


def test_popisky_urovni_se_neprekryvaji_a_drzi_poradi() -> None:
    placed = chart_render.spread_labels([698.22, 698.21, 699.66, 700.0, 691.99], 680.0, 707.0)
    gap = 27.0 * chart_render.LABEL_GAP
    ordered = sorted(placed)
    assert all(b - a >= gap - 1e-9 for a, b in zip(ordered, ordered[1:], strict=False))
    assert placed[1] < placed[0] < placed[2] < placed[3] and placed[4] == 691.99


@needs_mpl
def test_kresleni_golden_planu_a_grafu_nq_v_podprocesu() -> None:
    png = render_png(plan_chart_spec(_row(GOLDEN_PLAN), _stored(BARS_QQQ)))
    assert png.startswith(PNG_MAGIC)
    width, height = _png_size(png)
    assert width == 1200 and height >= 700
    nq = render_png(scenario_chart_spec(_scenario_row(), DATA["levels_nq"], []))
    assert nq.startswith(PNG_MAGIC) and _png_size(nq)[0] == 1200


# --- endpoint a hlídání místa -----------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.db'}"
    )


@pytest.fixture
def plans(tmp_path: Path) -> OptionPlansRepository:
    repository = OptionPlansRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.db'}"))
    repository.ensure_schema()
    return repository


@dataclasses.dataclass
class _FakeRender:
    specs: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    error: str | None = None

    def __call__(self, spec: dict[str, Any]) -> bytes:
        if self.error is not None:
            raise ChartRenderError(self.error)
        self.specs.append(spec)
        return PNG_MAGIC + b"plan" * len(self.specs)


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> _FakeRender:
    fake = _FakeRender()
    monkeypatch.setattr(option_plan_routes, "render_png", fake)
    return fake


def _create(plans: OptionPlansRepository, plan: dict[str, Any]) -> int:
    plan_id = plans.create(
        scenario_id=29,
        day=DAY,
        created_at=PLAN_TS,
        rules_version=1,
        verdict="obchodovat",
        status="sestaven",
        plan=plan,
    )
    assert plan_id is not None
    return plan_id


def test_png_se_nakresli_jednou_ke_stavu_a_po_preceneni_znovu(
    settings: Settings, plans: OptionPlansRepository, fake_render: _FakeRender
) -> None:
    SnapshotWriter(settings).write_bars_by_day(
        PLAN_SYMBOL,
        [
            UnderlyingBar(ts=b.ts, open=b.close, high=b.high, low=b.low, close=b.close, volume=0.0)
            for b in BARS_QQQ
        ],
    )
    plan_id = _create(plans, {**GOLDEN_PLAN, "slot": "9:15"})
    client = TestClient(create_app(settings))
    first = client.get(f"/option-plans/{plan_id}/image")
    assert first.status_code == 200 and first.headers["content-type"] == "image/png"
    assert client.get(f"/option-plans/{plan_id}/image").content == first.content
    assert len(fake_render.specs) == 1  # druhý dotaz čte z disku
    assert len(fake_render.specs[0]["price"]["points"]) == len(BARS_QQQ)  # uložené bary QQQ
    relative = f"scenarios/QQQ/2026-11-02/plan-{plan_id}-0915.png"
    row = plans.get(plan_id)
    assert row is not None and (row.image_path, row.image_bytes) == (relative, len(first.content))
    assert client.get("/option-plans").json()["option_plans"][0]["has_image"] is True

    assert plans.record_repricing(
        plan_id, {"slot": "9:31", "reasons": ["x"], "chain": None, "prices": None, "plan": None}
    )
    second = client.get(f"/option-plans/{plan_id}/image")
    assert second.status_code == 200 and second.content != first.content
    assert len(fake_render.specs) == 2
    assert not (settings.data_dir / relative).exists()  # PNG stavu 9:15 nahrazen
    assert (settings.data_dir / f"scenarios/QQQ/2026-11-02/plan-{plan_id}-0931.png").exists()
    disk = client.get("/scenarios/disk").json()
    assert (disk["bytes"], disk["images"]) == (len(second.content), 1)


def test_chyba_kresleni_500_s_duvodem_a_v_logu(
    settings: Settings,
    plans: OptionPlansRepository,
    fake_render: _FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_render.error = "podproces kreslení skončil kódem 1: chyba kreslení: ValueError: x"
    plan_id = _create(plans, GOLDEN_PLAN)
    client = TestClient(create_app(settings))
    with caplog.at_level(logging.ERROR):
        response = client.get(f"/option-plans/{plan_id}/image")
    assert response.status_code == 500
    assert response.json()["detail"] == f"Kreslení PNG plánu {plan_id} selhalo: {fake_render.error}"
    assert fake_render.error in caplog.text
    row = plans.get(plan_id)
    assert row is not None and row.image_path is None  # žádný prázdný obrázek
    assert client.get("/option-plans/999/image").status_code == 404


def test_rucni_uklid_smaze_i_png_planu_a_dalsi_dotaz_ho_nakresli(
    settings: Settings, plans: OptionPlansRepository, fake_render: _FakeRender
) -> None:
    plan_id = _create(plans, GOLDEN_PLAN)
    client = TestClient(create_app(settings))
    assert client.get(f"/option-plans/{plan_id}/image").status_code == 200
    cleaned = client.delete("/scenarios/images?older_than_days=1").json()
    assert (cleaned["removed"], cleaned["bytes"], cleaned["images"]) == (1, 0, 0)
    row = plans.get(plan_id)
    assert row is not None and (row.image_path, row.image_bytes) == (None, 0)
    assert client.get(f"/option-plans/{plan_id}/image").status_code == 200
    assert len(fake_render.specs) == 2


class _Store:
    def __init__(self, size: int) -> None:
        self.size = size
        self.ready = 0

    def ensure_schema(self) -> None:
        self.ready += 1

    def disk_usage(self) -> dict[str, int]:
        return {"bytes": self.size, "images": 1}

    def images_older_than(self, cutoff: dt.datetime, /) -> list[Any]:
        return []

    def set_image(self, row_id: int, image_path: str | None, image_bytes: int, /) -> None:
        return None


def test_spolecny_limit_scenaru_a_planu_alert_jednou(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gexlens_api.scenario_routes.SCENARIO_DISK_LIMIT", 100)
    alerts: list[dict[str, Any]] = []
    scenarios, option_plans = _Store(60), _Store(50)
    watch = ImageDiskWatch([lambda: scenarios, lambda: option_plans], alerts.append)
    assert watch.check() == {"bytes": 110, "images": 2}
    watch.check()
    assert len(alerts) == 1 and "2 souborů" in alerts[0]["message"]
    assert (scenarios.ready, option_plans.ready) == (1, 1)  # schéma jednou
    watch.reset()
    watch.check()
    assert len(alerts) == 2
