"""Testy CumΔ (issue #17): golden tick stream, bar větev zvlášť, reset, persistence."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from gexlens_engine.compute.cumdelta import (
    ClassifiedTrade,
    CumDeltaTracker,
    FlowRow,
    TradeSide,
    midpoint_sign,
)
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.parquet_store import SnapshotWriter

GOLDEN_PATH = Path(__file__).parent / "golden" / "cumdelta_basic.json"


def load_golden() -> dict[str, object]:
    data: dict[str, object] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def spec(right: str, strike: float = 7600.0) -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260716",
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E3D",
        multiplier="50",
    )


def trade(right: str, size: float, side: str, ts: float = 1.0) -> ClassifiedTrade:
    return ClassifiedTrade(spec=spec(right), price=10.0, size=size, ts=ts, side=TradeSide(side))


def test_restore_net_volume_nechava_prednost_zivemu_mereni() -> None:
    """#232: navázání z partice netflow nesmí přepsat tok naměřený po restartu."""
    tracker = CumDeltaTracker(multiplier=50.0)
    tracker.add_trade(trade("C", size=10.0, side="buy"), delta=0.5)

    tracker.restore_net_volume({spec("C"): 999.0, spec("P"): -40.0})

    assert tracker.net_volume(spec("C")) == 10.0  # živé měření má přednost
    assert tracker.net_volume(spec("P")) == -40.0  # chybějící klíč se doplní
    assert tracker.net_volumes() == {spec("C"): 10.0, spec("P"): -40.0}


def test_golden_tick_stream_reproduces_cum_delta() -> None:
    """AC: syntetický tick stream se známým buy/sell rozdělením → očekávaná CumΔ."""
    golden = load_golden()
    branch = golden["tick_branch"]
    assert isinstance(branch, dict)
    tracker = CumDeltaTracker(multiplier=float(golden["multiplier"]))  # type: ignore[arg-type]

    for item in branch["trades"]:
        flow = tracker.add_trade(
            trade(item["right"], item["size"], item["side"]), delta=item["delta"]
        )
        assert flow == pytest.approx(item["expected_flow"])

    assert tracker.cum_delta == pytest.approx(branch["expected_cum_delta"])


def test_golden_bar_branch_midpoint_test() -> None:
    """AC: bar-based větev (ΔVol × midpoint test) testována zvlášť."""
    golden = load_golden()
    branch = golden["bar_branch"]
    assert isinstance(branch, dict)
    tracker = CumDeltaTracker(multiplier=float(golden["multiplier"]))  # type: ignore[arg-type]
    contract = spec("C")

    for bar in branch["bars"]:
        flow = tracker.add_bar(
            contract,
            cumulative_volume=bar["volume"],
            last=bar["last"],
            bid=bar["bid"],
            ask=bar["ask"],
            delta=float(branch["delta"]),
        )
        assert flow == pytest.approx(bar["expected_flow"])

    assert tracker.cum_delta == pytest.approx(branch["expected_cum_delta"])


def test_both_branches_accumulate_together() -> None:
    tracker = CumDeltaTracker(multiplier=50.0)
    tracker.add_trade(trade("C", size=2.0, side="buy"), delta=0.5)  # +50
    contract = spec("P")
    tracker.add_bar(contract, 100.0, last=10.0, bid=10.0, ask=10.4, delta=-0.3)
    tracker.add_bar(contract, 120.0, last=10.0, bid=10.0, ask=10.4, delta=-0.3)
    # mid 10.2, last 10.0 < mid → -1: flow = -1*20*(-0.3)*50 = +300
    assert tracker.cum_delta == pytest.approx(350.0)


def test_midpoint_sign() -> None:
    assert midpoint_sign(10.3, 10.0, 10.4) == 1
    assert midpoint_sign(10.1, 10.0, 10.4) == -1
    assert midpoint_sign(10.2, 10.0, 10.4) == 0  # přesně na midu → bez klasifikace


def test_daily_reset_clears_state() -> None:
    tracker = CumDeltaTracker(multiplier=50.0)
    contract = spec("C")
    tracker.add_bar(contract, 100.0, 10.3, 10.0, 10.4, delta=0.5)
    tracker.add_bar(contract, 130.0, 10.3, 10.0, 10.4, delta=0.5)
    assert tracker.cum_delta != 0.0

    tracker.reset()

    assert tracker.cum_delta == 0.0
    # Po resetu je první bar zase „první" — nezapočítá starý objem přes noc
    flow = tracker.add_bar(contract, 30.0, 10.3, 10.0, 10.4, delta=0.5)
    assert flow == 0.0


def test_net_volume_per_contract() -> None:  # ADR-0011, #222
    """Čistý klasifikovaný objem (buy − sell, kontrakty) per spec, obě větve."""
    tracker = CumDeltaTracker(multiplier=50.0)
    call = spec("C")
    put = spec("P")
    # Bar větev: last nad midem (+30), pod midem (−20); první bar jen zakládá stav
    tracker.add_bar(call, 100.0, 10.3, 10.0, 10.4, delta=0.5)
    tracker.add_bar(call, 130.0, 10.3, 10.0, 10.4, delta=0.5)  # +30
    tracker.add_bar(call, 150.0, 10.0, 10.0, 10.4, delta=0.5)  # last pod midem → −20
    assert tracker.net_volume(call) == 10.0
    # Tick větev: buy 5, sell 2, unknown nepřispívá
    tracker.add_trade(trade("P", size=5.0, side="buy", ts=1.0), delta=-0.4)
    tracker.add_trade(trade("P", size=2.0, side="sell", ts=2.0), delta=-0.4)
    tracker.add_trade(trade("P", size=9.0, side="unknown", ts=3.0), delta=-0.4)
    assert tracker.net_volume(put) == 3.0
    assert tracker.net_volume(spec("C", strike=9999.0)) == 0.0  # neznámý kontrakt

    tracker.reset()
    assert tracker.net_volume(call) == 0.0
    assert tracker.net_volume(put) == 0.0


def test_volume_decrease_is_ignored_not_negative() -> None:
    tracker = CumDeltaTracker(multiplier=50.0)
    contract = spec("C")
    tracker.add_bar(contract, 100.0, 10.3, 10.0, 10.4, delta=0.5)
    flow = tracker.add_bar(contract, 80.0, 10.3, 10.0, 10.4, delta=0.5)
    assert flow == 0.0
    assert tracker.cum_delta == 0.0


def test_close_minute_series_and_persistence(tmp_path: Path) -> None:
    """SPEC 4.5/5.1: minutová řada flowΔ/CumΔ se ukládá do derived/{sym}/flow/."""
    tracker = CumDeltaTracker(multiplier=50.0)
    tracker.add_trade(trade("C", size=2.0, side="buy"), delta=0.5)  # +50
    row_1 = tracker.close_minute(dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC))
    tracker.add_trade(trade("C", size=1.0, side="sell"), delta=0.5)  # -25
    row_2 = tracker.close_minute(dt.datetime(2026, 7, 16, 15, 1, tzinfo=dt.UTC))

    assert row_1 == FlowRow(
        dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC), 50.0, 50.0, source="midpoint"
    )
    assert row_2.flow_delta == pytest.approx(-25.0)
    assert row_2.cum_delta == pytest.approx(25.0)

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    path = writer.write_flow("ES", dt.date(2026, 7, 16), [row_1, row_2])

    assert path == tmp_path / "derived" / "ES" / "flow" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    # CVD podkladu (#829) je součástí schématu; bez tasty větve zůstává NULL
    assert list(frame.columns) == [
        "ts_min",
        "flow_delta",
        "cum_delta",
        "futures_cvd_delta",
        "futures_cvd",
        "source",
    ]
    assert list(frame["cum_delta"]) == [50.0, 25.0]
    assert frame["futures_cvd"].isna().all()
    assert list(frame["source"]) == ["midpoint", "midpoint"]  # ADR-0032: zdroj znaménka v řadě


def test_roll_session_resets_only_on_boundary() -> None:
    """#638: první volání jen fixuje seanci; reset až při přechodu na nový den."""
    tracker = CumDeltaTracker(multiplier=50.0)
    tracker.add_trade(trade("C", size=2.0, side="buy"), delta=0.5)  # +50
    assert tracker.roll_session(dt.date(2026, 7, 20)) is False  # start procesu, bez resetu
    assert tracker.cum_delta == pytest.approx(50.0)
    assert tracker.roll_session(dt.date(2026, 7, 20)) is False  # táž seance
    assert tracker.cum_delta == pytest.approx(50.0)
    assert tracker.roll_session(dt.date(2026, 7, 21)) is True  # hranice → reset
    assert tracker.cum_delta == 0.0
    assert tracker.net_volumes() == {}


def test_restore_cum_navazuje_zaklad_po_restartu() -> None:
    """#638: seed z flow partice přičítá základ, tok po restartu se neztrácí."""
    tracker = CumDeltaTracker(multiplier=50.0)
    tracker.add_trade(trade("C", size=1.0, side="buy"), delta=0.5)  # +25 po restartu
    tracker.restore_cum(1000.0)
    assert tracker.cum_delta == pytest.approx(1025.0)


# ── Trade větev z dxFeed (ADR-0032, #615 fáze 3) ──────────────────────
DXFEED_GOLDEN_PATH = Path(__file__).parent / "golden" / "cumdelta_dxfeed.json"


def load_dxfeed_golden() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DXFEED_GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def run_dxfeed_golden(source: str) -> tuple[CumDeltaTracker, list[float]]:
    golden = load_dxfeed_golden()
    tracker = CumDeltaTracker(multiplier=float(golden["multiplier"]), source=source)
    contract = spec("C")
    delta = float(golden["delta"])
    bar_flows: list[float] = []
    for bar in golden["bars"]:
        for item in golden["trades"]:
            if item["minute"] == bar["minute"]:
                flow = tracker.add_dx_trade(
                    contract, size=item["size"], aggressor=item["aggressor"], delta=delta
                )
                expected = item["expected_flow_dxfeed"] if source == "dxfeed" else 0.0
                assert flow == pytest.approx(expected)
        bar_flows.append(
            tracker.add_bar(
                contract,
                cumulative_volume=bar["volume"],
                last=bar["last"],
                bid=bar["bid"],
                ask=bar["ask"],
                delta=delta,
            )
        )
    return tracker, bar_flows


@pytest.mark.parametrize("source", ["dxfeed", "midpoint"])
def test_golden_trade_vetev_dxfeed_vs_midpoint(source: str) -> None:
    """Golden (CLAUDE.md bod 3): tytéž tisky + bary, dva zdroje znaménka, CumΔ spočtené ručně."""
    golden = load_dxfeed_golden()
    expected = golden[source]
    assert isinstance(expected, dict)
    tracker, bar_flows = run_dxfeed_golden(source)

    assert bar_flows == pytest.approx(expected["expected_bar_flows"])
    assert tracker.cum_delta == pytest.approx(expected["expected_cum_delta"])
    stats = tracker.day_stats()
    assert stats["source"] == source
    for key, value in expected["expected_coverage"].items():
        assert stats[key] == pytest.approx(value), key


def test_dxfeed_tisk_bez_delty_necha_objem_baru() -> None:
    """Tisk bez delty se nepočítá a bar větev jeho objem doklasifikuje midpointem (fallback)."""
    tracker = CumDeltaTracker(multiplier=50.0, source="dxfeed")
    contract = spec("C")
    tracker.add_bar(contract, 100.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    assert tracker.add_dx_trade(contract, size=5.0, aggressor="BUY", delta=None) == 0.0
    flow = tracker.add_bar(contract, 105.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    assert flow == pytest.approx(5 * 0.4 * 50.0)  # +1 × 5 × 0.4 × 50
    stats = tracker.day_stats()
    assert stats["dropped_no_delta"] == 1
    assert stats["fallback_volume"] == 5.0


def test_dxfeed_net_volume_nese_stranu_od_burzy() -> None:
    """ADR-0011: čistý objem per kontrakt z tisků (buy − sell); midpoint jen bez tisků."""
    tracker = CumDeltaTracker(multiplier=50.0, source="dxfeed")
    contract = spec("P")
    tracker.add_bar(contract, 10.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    tracker.add_dx_trade(contract, size=4.0, aggressor="SELL", delta=-0.3)
    tracker.add_dx_trade(contract, size=1.0, aggressor="BUY", delta=-0.3)
    tracker.add_bar(contract, 15.0, last=5.0, bid=5.0, ask=5.4, delta=-0.3)
    assert tracker.net_volume(contract) == pytest.approx(-3.0)


def test_tracker_odmitne_neznamy_zdroj() -> None:
    with pytest.raises(ValueError):
        CumDeltaTracker(multiplier=50.0, source="tick")


def test_reset_vynuluje_pokryti_i_tisky_od_baru() -> None:
    tracker = CumDeltaTracker(multiplier=50.0, source="dxfeed")
    contract = spec("C")
    tracker.add_bar(contract, 100.0, last=10.3, bid=10.0, ask=10.4, delta=0.4)
    tracker.add_dx_trade(contract, size=3.0, aggressor="BUY", delta=0.4)
    tracker.reset()
    assert tracker.day_stats()["printed_volume"] == 0.0
    # po resetu první bar jen zakládá stav — tisky před resetem se nepřenesou
    assert tracker.add_bar(contract, 103.0, last=10.3, bid=10.0, ask=10.4, delta=0.4) == 0.0
