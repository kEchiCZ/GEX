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
    s = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
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


def test_days_listing(settings: Settings) -> None:
    """Daily pohled: seznam uložených dnů s expirací per den, seřazený dle data."""
    writer = SnapshotWriter(settings)
    other_day = dt.date(2026, 7, 17)
    writer.write_minute("ES", "20260717", other_day, snapshot_rows(0))
    # Duplicitní den ve druhé expiraci — vyhrává nejbližší (nejmenší) expirace
    writer.write_minute("ES", "20260718", other_day, snapshot_rows(0))
    client = TestClient(create_app(settings))

    assert client.get("/instruments/ES/days").json() == {
        "days": [
            {"date": "2026-07-16", "expiry": "20260716"},
            {"date": "2026-07-17", "expiry": "20260717"},
        ]
    }
    assert client.get("/instruments/SPY/days").status_code == 404


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
    assert payload["oi_prev"] == []  # bez archivu předchozího dne balík drží tvar


def test_replay_bundle_oi_prev(settings: Settings) -> None:
    """ΔOI vs. včera: /replay nese OI téže expirace z předchozího archivovaného dne."""
    from sqlalchemy import create_engine as sa_create_engine

    from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord

    oi_repo = OIEodRepository(sa_create_engine(settings.database_url))
    oi_repo.ensure_schema()
    previous_day = DAY - dt.timedelta(days=1)
    oi_repo.upsert_many(
        [
            OIRecord("ES", "20260716", 7600.0, "P", previous_day, 1234.0),
            OIRecord("ES", "20260716", 7600.0, "C", previous_day, 456.0),
        ]
    )
    client = TestClient(create_app(settings))

    payload = client.get(f"/replay/ES/20260716/{DAY.isoformat()}").json()
    assert payload["oi_prev_date"] == previous_day.isoformat()
    by_key = {(row["strike"], row["right"]): row["oi"] for row in payload["oi_prev"]}
    assert by_key[(7600.0, "P")] == 1234.0
    assert by_key[(7600.0, "C")] == 456.0


def test_profile_aggregate_sums_expiries(settings: Settings) -> None:
    """Σ profil: OI/volume se sčítají přes všechny expirace dne per strike a strana."""
    writer = SnapshotWriter(settings)
    writer.write_minute("ES", "20260717", DAY, snapshot_rows(0))  # druhá expirace, stejné hodnoty
    client = TestClient(create_app(settings))

    payload = client.get(f"/profile/ES/aggregate?date={DAY.isoformat()}").json()
    assert sorted(payload["expiries"]) == ["20260716", "20260717"]
    rows = {row["strike"]: row for row in payload["rows"]}
    # Fixture: strike 7590 (i=0) má call OI 100/expiraci; poslední minuta obou expirací
    # se liší (základní má 3 minuty, druhá 1) — OI je konstantní, volume z poslední minuty
    assert rows[7590.0]["callOi"] == 200.0  # 100 + 100 přes dvě expirace
    assert rows[7590.0]["putOi"] == 300.0  # 150 + 150
    # Volume: základní expirace poslední minuta (m=2): 10*3*1+5=35; druhá (m=0): 10*1*1+5=15
    assert rows[7590.0]["callVolume"] == 50.0


def test_setups_list_and_review(settings: Settings) -> None:
    """Setupy (ADR-0004): výpis historie a ruční hodnocení; predikce je jinak neměnná."""
    from sqlalchemy import create_engine as sa_create_engine

    from gexlens_engine.storage.setups_store import SetupsRepository

    repo = SetupsRepository(sa_create_engine(settings.database_url))
    repo.ensure_schema()
    setup_id = repo.create(
        symbol="ES",
        expiry="20260716",
        template="failed_break",
        direction="long",
        created_ts=dt.datetime.combine(DAY, dt.time(15, 0), tzinfo=dt.UTC),
        entry=7501.0,
        target=7515.0,
        stop=7472.0,
        confidence=55,
        reason="Neúspěšný průraz 7500 dolů a reclaim — spring.",
        context={"level": 7500.0},
    )
    client = TestClient(create_app(settings))

    payload = client.get(f"/setups/ES?date={DAY.isoformat()}").json()
    assert len(payload["setups"]) == 1
    row = payload["setups"][0]
    assert row["id"] == setup_id
    assert row["template"] == "failed_break"
    assert row["status"] == "active"
    assert client.get("/setups/ES?status=closed_target").json()["setups"] == []
    assert client.get("/setups/NQ").json()["setups"] == []

    assert (
        client.patch(
            f"/setups/ES/{setup_id}/review", json={"rating": 1, "note": "vyšlo dle predikce"}
        ).status_code
        == 200
    )
    reviewed = client.get("/setups/ES").json()["setups"][0]
    assert reviewed["user_rating"] == 1
    assert reviewed["user_note"] == "vyšlo dle predikce"

    assert client.patch("/setups/ES/99999/review", json={"rating": 1}).status_code == 404
    assert client.patch(f"/setups/ES/{setup_id}/review", json={"rating": 5}).status_code == 422


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


def test_chain_endpoint_last_minute_with_delta_oi(settings: Settings) -> None:
    """#202: řetěz z poslední minuty, strany C/P per strike, ΔOI vs. archiv."""
    from sqlalchemy import create_engine

    from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord

    repo = OIEodRepository(create_engine(settings.database_url))
    repo.ensure_schema()
    previous = DAY - dt.timedelta(days=1)
    repo.upsert_many(
        [OIRecord("ES", "20260716", strike, "C", previous, 80.0) for strike in STRIKES]
    )
    client = TestClient(create_app(settings))

    payload = client.get(f"/chain/ES/20260716?date={DAY.isoformat()}").json()

    assert payload["ts"] == ts(MINUTES - 1).isoformat()
    rows = payload["rows"]
    assert [row["strike"] for row in rows] == sorted(STRIKES)
    first = rows[0]
    assert first["call"]["bid"] == 10.0 and first["put"]["ask"] == 10.5
    assert first["call"]["delta"] == 0.5 and first["put"]["delta"] == -0.4
    assert first["call"]["oi"] == 100.0  # z fixture: 100·(i+1)
    assert first["call"]["oi_change"] == 20.0  # 100 − 80 z archivu
    assert first["put"]["oi_change"] is None  # put v archivu není
    assert first["call"]["stale"] is False


def test_chain_endpoint_missing_day_404(client: TestClient) -> None:
    response = client.get("/chain/ES/20260716?date=2026-07-01")
    assert response.status_code == 404


def test_replay_transport_f32_and_gzip(client: TestClient) -> None:
    """#247: snapshot matice jde po drátě jako float32, odpovědi se gzipují."""
    import pyarrow as pyarrow_types

    payload = client.get(f"/replay/ES/20260716/{DAY.isoformat()}").json()
    frame = read_arrow(base64.b64decode(payload["snapshots_arrow_base64"]))
    # Hodnoty sedí (f32 stačí na tick 0,25 exaktně)…
    assert float(frame[frame["right"] == "C"]["bid"].iloc[0]) == 10.0
    # …a typ na drátě je float32 (poloviční přenos)
    table = pyarrow.ipc.open_stream(
        io.BytesIO(base64.b64decode(payload["snapshots_arrow_base64"]))
    ).read_all()
    assert table.schema.field("bid").type == pyarrow_types.float32()
    assert table.schema.field("oi").type == pyarrow_types.float32()

    # GZip middleware: velká odpověď s Accept-Encoding chodí komprimovaná
    response = client.get(
        f"/replay/ES/20260716/{DAY.isoformat()}", headers={"accept-encoding": "gzip"}
    )
    assert response.headers.get("content-encoding") == "gzip"
