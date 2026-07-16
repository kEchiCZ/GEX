"""Integrační testy REST API (issue #19) nad uloženým testovacím dnem."""

import base64
import datetime as dt
import io
import os
import time
from pathlib import Path

import pandas as pd
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient

from gexlens_api.main import create_app
from gexlens_engine.compute.heatmap import HeatmapCell, HeatmapMode, compute_mode
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import (
    FlowRowLike,
    LevelsRow,
    SnapshotRow,
    SnapshotWriter,
)

DAY = dt.date(2026, 7, 16)
STRIKES = [7590.0, 7600.0, 7610.0]
MINUTES = 3


class _Flow:
    """Minimální FlowRowLike pro fixture (bez závislosti na compute)."""

    def __init__(self, ts_min: dt.datetime, flow_delta: float, cum_delta: float) -> None:
        self.ts_min = ts_min
        self.flow_delta = flow_delta
        self.cum_delta = cum_delta


def ts(minute: int) -> dt.datetime:
    return dt.datetime(2026, 7, 16, 15, minute, tzinfo=dt.UTC)


def snapshot_rows(minute: int) -> list[SnapshotRow]:
    rows = []
    for i, strike in enumerate(STRIKES):
        for right in ("C", "P"):
            volume = 10.0 * (minute + 1) * (i + 1) + (5.0 if right == "C" else 0.0)
            rows.append(
                SnapshotRow(
                    ts_min=ts(minute),
                    strike=strike,
                    right=right,
                    bid=10.0,
                    ask=10.5,
                    last=10.25,
                    volume=volume,
                    iv=0.15,
                    delta=0.5 if right == "C" else -0.4,
                    gamma=0.01,
                    theta=-0.5,
                    vega=1.2,
                    oi=100.0 * (i + 1) + (50.0 if right == "P" else 0.0),
                    stale_age=0.0,
                )
            )
    return rows


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path)
    writer = SnapshotWriter(s)
    for minute in range(MINUTES):
        writer.write_minute("ES", "20260716", DAY, snapshot_rows(minute))
    levels_rows = [
        LevelsRow(ts(m), 7660.0 if m == 0 else None, 7650.0, 7500.0, 7598.2, 400.0)
        for m in range(MINUTES)
    ]
    writer.write_levels("ES", "20260716", DAY, levels_rows)
    flow_rows: list[FlowRowLike] = [_Flow(ts(m), 50.0, 50.0 * (m + 1)) for m in range(MINUTES)]
    writer.write_flow("ES", DAY, flow_rows)
    day_bars = [
        Bar(ts=ts(m), open=7600.0, high=7605.0, low=7595.0, close=7600.0 + m, volume=1000.0)
        for m in range(MINUTES)
    ]
    writer.write_bars("ES", DAY, day_bars)
    return s


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def read_arrow(payload: bytes) -> pd.DataFrame:
    with pyarrow.ipc.open_stream(io.BytesIO(payload)) as reader:
        return reader.read_all().to_pandas()


def test_openapi_schema_complete(client: TestClient) -> None:
    """AC: OpenAPI schéma kompletní — všechny SPEC kap. 6 REST cesty existují."""
    paths = client.get("/openapi.json").json()["paths"]
    for expected in (
        "/instruments",
        "/instruments/{symbol}/expiries",
        "/snapshots/{symbol}/{expiry}",
        "/levels/{symbol}/{expiry}",
        "/profile/{symbol}/{expiry}",
        "/flow/{symbol}",
        "/replay/{symbol}/{expiry}/{date}",
        "/status",
    ):
        assert expected in paths, expected


def test_instruments_and_expiries(client: TestClient) -> None:
    assert client.get("/instruments").json() == {"instruments": ["ES"]}
    assert client.get("/instruments/ES/expiries").json() == {"expiries": ["20260716"]}
    assert client.get("/instruments/SPY/expiries").status_code == 404


def test_snapshots_arrow_matrix_oi_mode(client: TestClient) -> None:
    response = client.get(
        "/snapshots/ES/20260716", params={"date": DAY.isoformat(), "mode": "oi", "norm": "max"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apache.arrow")
    frame = read_arrow(response.content)
    assert len(frame) == MINUTES
    assert "call:7590" in frame.columns
    assert "put:7610" in frame.columns
    # OI je konstantní; norm=max → maximum vrstvy = 1.0 (put 7610: 350 = max)
    assert frame["put:7610"].iloc[0] == pytest.approx(1.0)
    assert frame["call:7590"].iloc[0] == pytest.approx(100.0 / 350.0)


def test_snapshots_matches_reference_implementation(client: TestClient, settings: Settings) -> None:
    """Vektorizovaná matice se musí shodovat s referenční per-snapshot implementací."""
    # Normalizační okno = jen první minuta (from/to), aby se dalo srovnat s referencí
    response = client.get(
        "/snapshots/ES/20260716",
        params={
            "date": DAY.isoformat(),
            "mode": "vol_otm",
            "norm": "max",
            "from": ts(0).isoformat(),
            "to": ts(0).isoformat(),
        },
    )
    frame = read_arrow(response.content)

    # Referenční výpočet: compute_mode nad buňkami první minuty, spot z barů (7600.0)
    cells = [
        HeatmapCell(
            strike=row.strike,
            right=row.right,
            oi=row.oi if row.oi is not None else 0.0,
            volume=row.volume if row.volume is not None else 0.0,
        )
        for row in snapshot_rows(0)
    ]
    reference = compute_mode(cells, HeatmapMode.VOL_OTM, spot=7600.0)
    all_values = [v for layer in reference.values() for v in layer.values()]
    denominator = max(abs(v) for v in all_values)
    for strike in STRIKES:
        expected_call = reference["call"][strike] / denominator
        assert frame[f"call:{strike:g}"].iloc[0] == pytest.approx(expected_call)


def test_snapshots_invalid_mode_and_missing_day(client: TestClient) -> None:
    assert (
        client.get(
            "/snapshots/ES/20260716", params={"date": DAY.isoformat(), "mode": "nope"}
        ).status_code
        == 422
    )
    assert client.get("/snapshots/ES/20260716", params={"date": "2026-07-01"}).status_code == 404


def test_snapshots_raw_returns_full_columns(client: TestClient) -> None:
    response = client.get("/snapshots/ES/20260716", params={"date": DAY.isoformat(), "raw": "true"})
    frame = read_arrow(response.content)
    assert {"ts_min", "strike", "right", "bid", "gamma", "oi", "stale_age"} <= set(frame.columns)
    assert len(frame) == MINUTES * len(STRIKES) * 2


def test_levels_json_with_none(client: TestClient) -> None:
    payload = client.get("/levels/ES/20260716", params={"date": DAY.isoformat()}).json()
    rows = payload["levels"]
    assert len(rows) == MINUTES
    assert rows[0]["flip"] == pytest.approx(7660.0)
    assert rows[1]["flip"] is None  # NaN → None v JSON


def test_profile_endpoint(client: TestClient) -> None:
    response = client.get(
        "/profile/ES/20260716",
        params={"date": DAY.isoformat(), "ts": ts(1).isoformat(), "variant": "vol"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["spot"] == pytest.approx(7601.0)  # close baru minuty 1
    row = next(r for r in payload["profile"] if r["strike"] == 7590.0)
    # minuta 1: call vol 25 × |0.5| = 12.5; put vol 20 × |−0.4| = 8 → net 4.5
    assert row["call_vol_component"] == pytest.approx(12.5)
    assert row["net"] == pytest.approx(4.5)
    assert row["distance_from_spot"] == pytest.approx(7590.0 - 7601.0)


def test_flow_endpoint_series(client: TestClient) -> None:
    payload = client.get("/flow/ES", params={"date": DAY.isoformat()}).json()
    assert [row["cum_delta"] for row in payload["flow"]] == [50.0, 100.0, 150.0]
    opt_vol = payload["opt_vol"]
    assert len(opt_vol) == MINUTES
    assert opt_vol[0]["opt_vol"] == 0.0  # první minuta bez přírůstku
    # Minutový přírůstek: Σ volume roste o 10*(i+1) na C i P → 10+10+20+20+30+30 = 120
    assert opt_vol[1]["opt_vol"] == pytest.approx(120.0)
    assert [row["vol"] for row in payload["vol"]] == [1000.0, 1000.0, 1000.0]


def test_replay_bundle(client: TestClient) -> None:
    payload = client.get(f"/replay/ES/20260716/{DAY.isoformat()}").json()
    assert payload["date"] == DAY.isoformat()
    assert len(payload["levels"]) == MINUTES
    assert len(payload["bars"]) == MINUTES
    raw = read_arrow(base64.b64decode(payload["snapshots_arrow_base64"]))
    assert len(raw) == MINUTES * len(STRIKES) * 2  # surová data pro lokální přepínání módů


def test_status_store(client: TestClient) -> None:
    assert client.get("/status").json()["engine"] == "offline"
    client.app.state.status_store.update(engine="online", greeks_complete=350, greeks_total=360)  # type: ignore[attr-defined]
    payload = client.get("/status").json()
    assert payload["engine"] == "online"
    assert payload["greeks_complete"] == 350
    assert payload["updated_at"] is not None


@pytest.mark.skipif(bool(os.environ.get("CI")), reason="výkonnostní AC se měří lokálně")
def test_heatmap_180x1440_under_300ms(tmp_path: Path) -> None:
    """AC: heatmap odpověď pro 180 strikes × 1440 minut < 300 ms lokálně."""
    settings = Settings(data_dir=tmp_path)
    strikes = [7000.0 + 5 * i for i in range(180)]
    minutes = pd.date_range("2026-07-16 00:00", periods=1440, freq="min", tz="UTC")
    frame = pd.DataFrame(
        {
            "ts_min": [m for m in minutes for _ in strikes for _ in range(2)],
            "strike": [s for _ in minutes for s in strikes for _ in range(2)],
            "right": ["C", "P"] * (len(minutes) * len(strikes)),
            "bid": 10.0,
            "ask": 10.5,
            "last": 10.25,
            "volume": 100.0,
            "iv": 0.15,
            "delta": 0.5,
            "gamma": 0.01,
            "theta": -0.5,
            "vega": 1.2,
            "oi": 1000.0,
            "stale_age": 0.0,
        }
    )
    partition = settings.snapshots_dir / "ES" / "20260716"
    partition.mkdir(parents=True)
    frame.to_parquet(partition / "2026-07-16.parquet")

    client = TestClient(create_app(settings))
    client.get("/snapshots/ES/20260716", params={"date": "2026-07-16"})  # zahřátí importů

    start = time.perf_counter()
    response = client.get(
        "/snapshots/ES/20260716",
        params={"date": "2026-07-16", "mode": "oi_signed_all", "scale": "sqrt"},
    )
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed < 0.3, f"heatmap odpověď trvala {elapsed:.3f}s"
