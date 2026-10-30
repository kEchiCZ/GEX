"""Golden brány šablon nad čistým R (#1402, kritérium A z #1334).

Očekávané hodnoty spočítal ručně skript mimo engine (vzorec v `_comment`
datasetu): hrubé R = brána do mechaniky v5, čisté R s poplatkem mikra
z parametrů (`micro_fee_usd` datasetu) a skluzem na stranu (ADR-0030) = brána
od v6. ES se stopem 2,4 b ukazuje změnu znaménka z #1326.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.paper import POINT_VALUES
from gexlens_engine.compute.risk import RealizedSetup, template_gate
from gexlens_engine.compute.setup_library import cell_evidence, gate_results
from gexlens_engine.compute.setups import NET_R_GATE, SETUP_MECHANICS_VERSION, SetupParams

DATA: dict[str, Any] = json.loads(
    (Path(__file__).parent / "golden" / "template_gate_net_1402.json").read_text(encoding="utf-8")
)
CLOSED = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)


def _rows(case: dict[str, Any]) -> list[RealizedSetup]:
    entry = 7000.0
    return [
        RealizedSetup(
            symbol=case["symbol"],
            template="failed_break",
            status="closed_target" if outcome > 0 else "closed_stop",
            outcome_r=outcome,
            closed_ts=CLOSED,
            tradeable=False,
            affordable=True,
            entry=entry,
            stop=entry - case["stop_points"],
        )
        for outcome, count in case["outcomes"]
        for _ in range(count)
    ]


def test_prepinac_cisteho_r_je_mechanika_v6() -> None:
    assert (SETUP_MECHANICS_VERSION >= 6) == NET_R_GATE
    assert len(DATA["cases"]) >= 6
    # Dataset počítá s výchozím poplatkem mikra z Nastavení (IBKR 2 × 0,61 $)
    assert DATA["micro_fee_usd"] == SetupParams().fee_per_micro_contract_usd


@pytest.mark.parametrize("case", DATA["cases"], ids=[case["name"] for case in DATA["cases"]])
@pytest.mark.parametrize("convention", ["gross", "net"])
def test_golden_brany(case: dict[str, Any], convention: str) -> None:
    rows = _rows(case)
    gate = template_gate(
        gate_results(
            rows, POINT_VALUES, net=convention == "net", micro_fee_usd=DATA["micro_fee_usd"]
        ),
        min_samples=DATA["min_samples"],
        enabled=True,
    )
    expected = case["expected"][convention]
    assert gate.verdict == expected["verdict"]
    assert gate.n == expected["n"]
    assert gate.lower_bound == pytest.approx(expected["lb"], abs=1e-9)


@pytest.mark.parametrize("case", DATA["cases"], ids=[case["name"] for case in DATA["cases"]])
def test_golden_dukaz_bunky_pod_v6(case: dict[str, Any]) -> None:
    """ØR čistě v Knihovně a pushi = týž náklad jako brána (jedna konvence, #1326)."""
    evidence = cell_evidence(
        _rows(case),
        POINT_VALUES,
        min_samples=DATA["min_samples"],
        micro_fee_usd=DATA["micro_fee_usd"],
        net=True,
    )
    assert evidence.avg_net_r == pytest.approx(case["expected"]["net"]["avg"], abs=1e-9)


def test_es_naklad_meni_znamenko() -> None:
    """#1326: hrubě projde, po nákladech v6 má záporný průměr i dolní mez."""
    (case,) = [c for c in DATA["cases"] if c["name"] == "es_stop_2_4_naklad_meni_znamenko"]
    assert case["expected"]["gross"]["verdict"] == "pass"
    assert case["expected"]["net"]["verdict"] == "block"
    assert case["expected"]["net"]["avg"] < 0 < case["expected"]["gross"]["lb"]
