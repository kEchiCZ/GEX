"""Kouč API (#933): denní review a týdenní report nad řádky deníku."""

import datetime as dt
from typing import Any

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gexlens_api.coach_routes import build_coach_router
from gexlens_engine.compute.setups import SetupParams

NOW = dt.datetime(2026, 9, 17, 18, 0, tzinfo=dt.UTC)
T0 = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)


def _row(entry_id: int, **trade: Any) -> dict[str, Any]:
    base = {
        "direction": "long",
        "planned_entry": 7600.0,
        "planned_stop": 7592.0,
        "planned_target": 7616.0,
        "actual_entry": 7600.0,
        "actual_exit": 7604.0,
        "size": 1,
        "opened_ts": T0.isoformat(),
        "closed_ts": (T0 + dt.timedelta(minutes=20)).isoformat(),
        "setup_key": "failed_break",
        "mfe": 6.0,
        "mae": 2.0,
        "net_pnl": 190.0,
    }
    base.update(trade)
    return {
        "id": entry_id,
        "symbol": "ES",
        "entry_type": "obchod",
        "ts_ref": T0.isoformat(),
        "tags": ["paper"],
        "context": {"paper_order_id": entry_id, "exit_reason": "manual", "r_multiple": 0.5},
        "trade": base,
    }


def _bars(symbol: str, day: dt.date) -> pd.DataFrame:
    stamps = [T0 + dt.timedelta(minutes=m) for m in range(0, 90)]
    return pd.DataFrame(
        {"ts_min": stamps, "high": [7600.0 + m for m in range(90)], "low": [7599.0] * 90}
    )


def test_review_a_weekly() -> None:
    rows = [_row(1), {"id": 2, "symbol": "ES", "entry_type": "pozorovani", "trade": None}]
    app = FastAPI()
    app.include_router(
        build_coach_router(lambda since, until: rows, _bars, SetupParams, now=lambda: NOW)
    )
    client = TestClient(app)
    review = client.get("/coach/review").json()
    assert review["session_day"] == "2026-09-17" and review["n"] == 1
    flags = [f["kind"] for f in review["trades"][0]["flags"]]
    assert flags == ["early_exit"] and review["score"] == 90
    assert "disciplína 90/100" in review["summary"]
    assert client.get("/coach/review", params={"symbol": "NQ"}).json()["n"] == 0
    weekly = client.get("/coach/weekly", params={"date": "2026-09-17"}).json()
    assert weekly["week_start"] == "2026-09-14" and weekly["n"] == 1
    assert weekly["rules"][0]["kind"] == "early_exit" and weekly["rules"][0]["cost_r"] == -1.5
    assert weekly["days"][3]["session_day"] == "2026-09-17"
