"""Kouč v1 (#933): příznaky s důkazem, skóre, denní review, týdenní report."""

import datetime as dt
from typing import Any

from gexlens_engine.compute.coach import (
    CoachParams,
    Trade,
    daily_review,
    discipline_score,
    review_trade,
    trade_from_journal,
    weekly_report,
)

DAY = dt.date(2026, 9, 17)
T0 = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)  # 10:00 ET


def trade(**overrides: Any) -> Trade:
    base: dict[str, Any] = {
        "id": 1,
        "symbol": "ES",
        "direction": "long",
        "opened_ts": T0,
        "closed_ts": T0 + dt.timedelta(minutes=30),
        "planned_entry": 7600.0,
        "planned_stop": 7592.0,
        "planned_target": 7616.0,
        "actual_entry": 7600.0,
        "actual_exit": 7616.0,
        "size": 1.0,
        "setup_key": "failed_break",
        "mfe": 17.0,
        "mae": 3.0,
        "net_pnl": 790.0,
        "paper": True,
        "exit_reason": "target",
        "r_multiple": 2.0,
    }
    base.update(overrides)
    return Trade(**base)


def test_cisty_obchod_bez_priznaku_a_capture() -> None:
    review = review_trade(trade(), [])
    assert review.flags == () and review.realized_r == 2.0 and review.planned_rr == 2.0
    assert review.capture is not None and round(review.capture, 3) == round(2.0 / (17 / 8), 3)
    assert discipline_score([review]) == 100


def test_priznaky_no_stop_no_setup_low_rr_big_loss_after_brake() -> None:
    t = trade(
        planned_stop=None, setup_key=None, r_multiple=-1.6, exit_reason="stop", day_r_at_entry=-3.5
    )
    review = review_trade(t, [])
    kinds = [f.kind for f in review.flags]
    assert kinds == ["no_stop", "no_setup", "big_loss", "after_brake"]
    assert discipline_score([review]) == 100 - 25 - 10 - 15 - 25
    low = review_trade(trade(planned_target=7608.0, r_multiple=1.0), [])
    assert [f.kind for f in low.flags] == ["low_rr"]
    assert low.flags[0].detail.startswith("plánované RRR 1.0")


def test_revenge_a_overtrading() -> None:
    stopped = trade(
        id=1, exit_reason="stop", r_multiple=-1.0, closed_ts=T0 + dt.timedelta(minutes=10)
    )
    quick = trade(
        id=2, opened_ts=T0 + dt.timedelta(minutes=13), r_multiple=-1.0, exit_reason="stop"
    )
    review = review_trade(quick, [stopped])
    assert [f.kind for f in review.flags] == ["revenge"]
    assert review.flags[0].detail == "vstup 3 min po stopu #1" and review.flags[0].cost_r == -1.0
    # Po 6 minutách už ne; jiný symbol ne
    later = trade(id=3, opened_ts=T0 + dt.timedelta(minutes=16))
    assert review_trade(later, [stopped]).flags == ()
    other = trade(id=4, symbol="NQ", opened_ts=T0 + dt.timedelta(minutes=12))
    assert review_trade(other, [stopped]).flags == ()
    fifth = review_trade(trade(id=5), [], trades_same_session=5)
    assert [f.kind for f in fifth.flags] == ["overtrading"]


def test_early_exit_z_baru_po_vystupu() -> None:
    manual = trade(exit_reason="manual", actual_exit=7604.0, r_multiple=0.5)
    bars_hit = [(T0 + dt.timedelta(minutes=m), 7600.0 + m, 7599.0) for m in range(0, 60)]

    def bars_after(symbol: str, day: dt.date) -> list[tuple[dt.datetime, float, float]]:
        return bars_hit

    review = review_trade(manual, [], bars_after=bars_after)
    assert [f.kind for f in review.flags] == ["early_exit"]
    assert review.flags[0].cost_r == -1.5  # 0,5 R realizováno, cíl 2 R
    # Cena po výstupu na cíl nedošla → bez příznaku
    flat = [(T0 + dt.timedelta(minutes=m), 7605.0, 7599.0) for m in range(0, 60)]
    assert review_trade(manual, [], bars_after=lambda s, d: flat).flags == ()
    # Výstup na cíl (target) se nehodnotí
    assert review_trade(trade(), [], bars_after=bars_after).flags == ()


def test_denni_review_a_tydenni_report() -> None:
    trades = [
        trade(id=1),
        trade(
            id=2,
            opened_ts=T0 + dt.timedelta(minutes=40),
            closed_ts=T0 + dt.timedelta(minutes=50),
            exit_reason="stop",
            r_multiple=-1.0,
            actual_exit=7592.0,
            net_pnl=-410.0,
        ),
        trade(
            id=3,
            opened_ts=T0 + dt.timedelta(minutes=52),
            closed_ts=T0 + dt.timedelta(minutes=70),
            exit_reason="stop",
            r_multiple=-1.0,
            actual_exit=7592.0,
            net_pnl=-410.0,
            setup_key=None,
        ),
        trade(id=4, opened_ts=T0 + dt.timedelta(days=1)),  # zítra — do dneška nepatří
    ]
    review = daily_review(trades, DAY)
    assert review.reviews[0].trade.id == 1 and len(review.reviews) == 3
    assert review.total_r == 0.0
    kinds = [[f.kind for f in r.flags] for r in review.reviews]
    assert kinds == [[], [], ["no_setup", "revenge"]]
    assert review.score == 100 - 10 - 15 and review.flagged_cost_r == -2.0
    payload = review.as_dict()
    assert payload["n"] == 3 and "disciplína 75/100" in payload["summary"]
    week = weekly_report(trades, DAY).as_dict()
    assert week["week_start"] == "2026-09-14" and week["n"] == 4 and week["total_r"] == 2.0
    assert week["flags"]["revenge"] == {"n": 1, "cost_r": -1.0}
    assert week["rules"][0]["kind"] in ("revenge", "no_setup") and len(week["rules"]) == 2
    assert week["by_hour_utc"]["14"]["n"] == 4
    # Parametry: přísnější práh RRR označí i čistý obchod
    strict = daily_review(trades[:1], DAY, params=CoachParams(min_rr=2.5))
    assert [f.kind for f in strict.reviews[0].flags] == ["low_rr"]


def test_trade_from_journal_radek() -> None:
    row = {
        "id": 9,
        "symbol": "NQ",
        "ts_ref": "2026-09-17T14:00:00+00:00",
        "tags": ["paper"],
        "context": {
            "paper_order_id": 3,
            "exit_reason": "manual",
            "r_multiple": 0.4,
            "day_r_at_entry": -1.0,
        },
        "trade": {
            "direction": "short",
            "planned_entry": 29000.0,
            "planned_stop": 29025.0,
            "planned_target": 28950.0,
            "actual_entry": 29000.0,
            "actual_exit": 28990.0,
            "size": 1,
            "opened_ts": "2026-09-17T14:01:00+00:00",
            "closed_ts": None,
            "setup_key": "wall_bounce",
            "mfe": 30.0,
            "mae": 5.0,
            "net_pnl": 190.0,
        },
    }
    t = trade_from_journal(row)
    assert t is not None and t.paper and t.exit_reason == "manual" and t.r_multiple == 0.4
    assert t.opened_ts == dt.datetime(2026, 9, 17, 14, 1, tzinfo=dt.UTC) and t.closed_ts is None
    assert t.planned_rr == 2.0 and t.sign == -1.0
    assert trade_from_journal({"id": 1, "trade": None}) is None


def test_stop_moved_z_historie_paper_orderu() -> None:
    moved = review_trade(trade(stop_widened_points=5.0, r_multiple=-1.3, exit_reason="stop"), [])
    kinds = [f.kind for f in moved.flags]
    assert "stop_moved" in kinds and "big_loss" in kinds
    assert next(f for f in moved.flags if f.kind == "stop_moved").cost_r == -1.3
    assert review_trade(trade(stop_widened_points=0.0), []).flags == ()
