"""Testy CumΔ (issue #17): golden tick stream, bar větev zvlášť, reset, persistence."""

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.compute.cumdelta import CumDeltaTracker, FlowRow, midpoint_sign
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.hotzone import ClassifiedTrade, TradeSide
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

    assert row_1 == FlowRow(dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC), 50.0, 50.0)
    assert row_2.flow_delta == pytest.approx(-25.0)
    assert row_2.cum_delta == pytest.approx(25.0)

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    path = writer.write_flow("ES", dt.date(2026, 7, 16), [row_1, row_2])

    assert path == tmp_path / "derived" / "ES" / "flow" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    assert list(frame.columns) == ["ts_min", "flow_delta", "cum_delta"]
    assert list(frame["cum_delta"]) == [50.0, 25.0]
