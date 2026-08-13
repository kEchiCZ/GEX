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
    assert payload["catchup"] == []  # engine běžel celý den — řada neexistuje (#518)
    # Flow-adjusted zdroj (#232): klíče drží tvar, i když řady (zatím) neexistují
    assert payload["oiest"] == []
    assert payload["gexprofilefa"] == []
    assert payload["gexfieldfa"] == []


def test_replay_bundle_oiest(settings: Settings) -> None:
    """OI odhad z toku (#232): /replay nese řadu oiest + FA Dyn GEX profil."""
    from gexlens_engine.storage.parquet_store import GexProfileRow, OiEstRow

    writer = SnapshotWriter(settings)
    writer.write_oiest(
        "ES", "20260716", DAY, [OiEstRow(ts_min=ts(1), strike=7600.0, right="C", oi_est=140.0)]
    )
    writer.write_gexprofile(
        "ES",
        "20260716",
        DAY,
        [GexProfileRow(ts_min=ts(1), grid_start=7590.0, grid_step=5.0, values=[1.0, 2.0])],
        subdir="gexprofilefa",
    )
    client = TestClient(create_app(settings))

    payload = client.get(f"/replay/ES/20260716/{DAY.isoformat()}").json()
    assert payload["oiest"] == [
        {"ts_min": ts(1).isoformat(), "strike": 7600.0, "right": "C", "oi_est": 140.0}
    ]
    assert payload["gexprofilefa"][0]["values"] == [1.0, 2.0]


def test_replay_bundle_catchup(settings: Settings) -> None:
    """Catch-up minuty (#518, ADR-0024): /replay nese minutu prvního sweepu po startu."""
    from gexlens_engine.storage.parquet_store import CatchUpRow

    writer = SnapshotWriter(settings)
    writer.write_catch_up("ES", "20260716", DAY, [CatchUpRow(ts_min=ts(1))])
    client = TestClient(create_app(settings))

    payload = client.get(f"/replay/ES/20260716/{DAY.isoformat()}").json()
    assert [row["ts_min"] for row in payload["catchup"]] == [ts(1).isoformat()]


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


def test_fa_alpha_endpoint(settings: Settings) -> None:
    """Kalibrovaná FA α (#232 fáze 2): prázdný stav drží tvar, pak per-symbol záznam."""
    from sqlalchemy import create_engine as sa_create_engine

    from gexlens_engine.compute.facalibration import AlphaCalibrationPoint
    from gexlens_engine.storage.fa_calibration import FaAlphaRepository

    client = TestClient(create_app(settings))
    assert client.get("/fa/alpha").json() == {"alphas": []}

    repo = FaAlphaRepository(sa_create_engine(settings.database_url))
    repo.ensure_schema()
    point = AlphaCalibrationPoint(samples=8, ratio_median=0.34, ratio_buy=0.4, ratio_sell=0.31)
    repo.record(
        "ES",
        DAY,
        "20260716",
        point,
        alpha_after=0.34,
        days=5,
        now=dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.UTC),
    )

    payload = client.get("/fa/alpha").json()
    assert len(payload["alphas"]) == 1
    row = payload["alphas"][0]
    assert row["symbol"] == "ES"
    assert row["alpha"] == 0.34
    assert row["days"] == 5


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


def test_gammacliff_endpoint(settings: Settings) -> None:
    """#576: /gammacliff vrací živý podíl (z levels) + historii (tabulka může chybět)."""
    from gexlens_engine.storage.parquet_store import LevelsRow

    writer = SnapshotWriter(settings)
    today = dt.datetime.now(dt.UTC)
    from gexlens_engine.compute.settle import trading_session_date

    session = trading_session_date(today)
    key = session.strftime("%Y%m%d")
    next_key = (session + dt.timedelta(days=1)).strftime("%Y%m%d")

    def row(gex: float) -> LevelsRow:
        return LevelsRow(today - dt.timedelta(minutes=5), 7600.0, 7650.0, 7550.0, 7590.0, gex)

    writer.write_levels("ES", key, session, [row(-600.0)])
    writer.write_levels("ES", next_key, session, [row(400.0)])
    client = TestClient(create_app(settings))

    payload = client.get("/gammacliff/ES").json()
    assert payload["today"] is not None
    assert payload["today"]["cliff_share"] == pytest.approx(0.6)
    assert payload["rows"] == []  # engine tabulku ještě nezaložil — tvar drží


def test_gexforward_endpoint(settings: Settings) -> None:
    """Forward GEX (#519): bloky per den z partice; NaN dropped_share → None."""
    writer = SnapshotWriter(settings)
    computed = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.UTC)
    writer.write_gexforward(
        "ES",
        DAY,
        [
            {
                "day": "2026-07-16",
                "grid_start": 7400.0,
                "grid_step": 50.0,
                "values": [1.0, 2.0, 3.0],
                "dropped_expiries": [],
                "dropped_share": float("nan"),
                "iv_fallback_share": 0.1,
                "computed_ts": computed,
            },
            {
                "day": "2026-07-17",
                "grid_start": 7400.0,
                "grid_step": 50.0,
                "values": [0.5, 1.0, 1.5],
                "dropped_expiries": ["20260716"],
                "dropped_share": 0.38,
                "iv_fallback_share": 0.1,
                "computed_ts": computed,
            },
        ],
    )
    client = TestClient(create_app(settings))
    payload = client.get("/gexforward/ES", params={"date": DAY.isoformat()}).json()
    assert payload["symbol"] == "ES"
    days = payload["days"]
    assert [d["day"] for d in days] == ["2026-07-16", "2026-07-17"]
    assert days[0]["dropped_share"] is None
    assert days[1]["dropped_share"] == 0.38
    assert days[1]["dropped_expiries"] == ["20260716"]
    assert days[1]["values"] == [0.5, 1.0, 1.5]
    # Den bez partice = prázdné days, ne chyba
    empty = client.get("/gexforward/ES", params={"date": "2020-01-01"}).json()
    assert empty["days"] == []


def test_journal_crud_a_validace(client: TestClient) -> None:
    """Deník (#673 fáze A): CRUD, filtry, zákaz ručního typu obchod."""
    entry = {
        "ts_ref": "2026-07-16T14:30:00Z",
        "symbol": "ES",
        "entry_type": "pozorovani",
        "text": "Cena respektuje flip, odrazy drží.",
        "tags": ["flip", "fade"],
    }
    created = client.post("/journal", json=entry)
    assert created.status_code == 201
    entry_id = created.json()["id"]

    listed = client.get("/journal", params={"symbol": "ES", "date": "2026-07-16"}).json()
    assert [e["id"] for e in listed["journal"]] == [entry_id]
    assert listed["journal"][0]["tags"] == ["flip", "fade"]
    # Jiný den = prázdno
    assert client.get("/journal", params={"date": "2026-07-17"}).json()["journal"] == []

    patched = client.patch(f"/journal/{entry_id}", json={"text": "Upřesnění: drží jen do 16:00."})
    assert patched.status_code == 200
    assert patched.json()["updated_ts"] is not None

    # Typ obchod zakládá až import fillů (fáze B), ruční zápis se odmítne
    assert client.post("/journal", json={**entry, "entry_type": "obchod"}).status_code == 422
    assert client.post("/journal", json={**entry, "entry_type": "blbost"}).status_code == 422
    # Symbol validace (stejné pravidlo jako persist reviver #554)
    assert client.post("/journal", json={**entry, "symbol": "../x"}).status_code == 422

    assert client.delete(f"/journal/{entry_id}").status_code == 204
    assert client.get("/journal").json()["journal"] == []


def test_bars_endpoint(client: TestClient) -> None:
    """Lehké OHLCV bary (#674/#678): JSON alternativa k /replay jen pro cenu."""
    payload = client.get(f"/bars/ES?date={DAY.isoformat()}").json()
    assert payload["symbol"] == "ES"
    assert len(payload["bars"]) == MINUTES
    first = payload["bars"][0]
    assert {"ts_min", "open", "high", "low", "close", "volume"} <= set(first)
    assert client.get("/bars/NEZNAMY?date=2026-07-16").status_code == 404


def test_oidelta_endpoint(settings: Settings) -> None:
    """ΔOI přes noc (#674): souhrn posledních dvou archivovaných dnů expirace."""
    from sqlalchemy import create_engine as sa_create_engine

    from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord

    oi_repo = OIEodRepository(sa_create_engine(settings.database_url))
    oi_repo.ensure_schema()
    previous_day = DAY - dt.timedelta(days=1)
    oi_repo.upsert_many(
        [
            OIRecord("ES", "20260716", 7600.0, "C", previous_day, 100.0),
            OIRecord("ES", "20260716", 7600.0, "P", previous_day, 200.0),
            OIRecord("ES", "20260716", 7600.0, "C", DAY, 150.0),
            OIRecord("ES", "20260716", 7600.0, "P", DAY, 180.0),
            OIRecord("ES", "20260716", 7650.0, "C", DAY, 40.0),  # nový strike bez včerejška
        ]
    )
    client = TestClient(create_app(settings))

    payload = client.get("/oidelta/ES/20260716").json()
    assert payload["days"] == {
        "current": DAY.isoformat(),
        "previous": previous_day.isoformat(),
    }
    assert payload["call_total"] == 190.0
    assert payload["put_total"] == 180.0
    assert payload["call_delta"] == 90.0  # +50 na 7600 + 40 nový strike
    assert payload["put_delta"] == -20.0
    movers = payload["movers"]
    assert movers[0]["strike"] == 7600.0 and movers[0]["right"] == "C"  # |Δ| 50 největší

    # Bez archivu drží tvar (days: None) — briefing sekci skryje
    empty = client.get("/oidelta/ES/20991231").json()
    assert empty["days"] is None


def test_profile_window_full_day_equals_daily_cumulative(client: TestClient) -> None:
    """AC #483: okno přes celý den == denní kumulativ (baseline před prvním snapshotem = 0)."""
    window = client.get(
        "/profile/ES/20260716",
        params={
            "date": DAY.isoformat(),
            "from": (ts(0) - dt.timedelta(minutes=5)).isoformat(),
            "to": ts(MINUTES - 1).isoformat(),
            "variant": "vol",
        },
    ).json()
    point = client.get(
        "/profile/ES/20260716",
        params={"date": DAY.isoformat(), "ts": ts(MINUTES - 1).isoformat(), "variant": "vol"},
    ).json()
    assert window["profile"] == point["profile"]
    assert window["oi_static"] is True
    assert window["from_ts"] is None  # nic před from → od začátku seance
    assert window["to_ts"] == ts(MINUTES - 1).isoformat()
    assert window["stale_count"] == 0


def test_profile_window_zero_length_is_zero(client: TestClient) -> None:
    """AC #483: okno nulové délky == nulové volume složky (OI zůstává statické)."""
    payload = client.get(
        "/profile/ES/20260716",
        params={
            "date": DAY.isoformat(),
            "from": ts(1).isoformat(),
            "to": ts(1).isoformat(),
            "variant": "vol",
        },
    ).json()
    for row in payload["profile"]:
        assert row["call_vol_component"] == 0.0
        assert row["put_vol_component"] == 0.0
        assert row["call_volume"] == 0.0
    # OI je statické k t2, ne nulové
    assert payload["profile"][0]["call_oi"] == 100.0


def test_profile_window_diff_values(client: TestClient) -> None:
    """Okno (t0, t2]: volume = kumulativ(t2) − kumulativ(t0) per strike a strana."""
    payload = client.get(
        "/profile/ES/20260716",
        params={
            "date": DAY.isoformat(),
            "from": ts(0).isoformat(),
            "to": ts(2).isoformat(),
            "variant": "vol",
        },
    ).json()
    row = next(r for r in payload["profile"] if r["strike"] == 7590.0)
    # vol okna = 10·(3−1)·1 = 20 na obou stranách (+5 za C se odečte)
    assert row["call_volume"] == pytest.approx(20.0)
    assert row["put_volume"] == pytest.approx(20.0)
    assert row["call_vol_component"] == pytest.approx(20.0 * 0.5)
    assert row["net"] == pytest.approx(10.0 - 8.0)
    assert payload["from_ts"] == ts(0).isoformat()


def test_profile_window_param_validation(client: TestClient) -> None:
    """ts × from/to se vylučují; from ≤ to; něco z toho musí přijít."""
    base = {"date": DAY.isoformat()}
    assert client.get("/profile/ES/20260716", params=base).status_code == 422
    assert (
        client.get(
            "/profile/ES/20260716",
            params={
                **base,
                "ts": ts(1).isoformat(),
                "from": ts(0).isoformat(),
                "to": ts(1).isoformat(),
            },
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/profile/ES/20260716",
            params={**base, "from": ts(2).isoformat(), "to": ts(0).isoformat()},
        ).status_code
        == 422
    )
    assert (
        client.get("/profile/ES/20260716", params={**base, "from": ts(0).isoformat()}).status_code
        == 422
    )


def test_flow_window_summary(client: TestClient) -> None:
    """Okno /flow (#483): CumΔ diff + součty Vol/OptVol přes minuty (t1, t2]."""
    payload = client.get(
        "/flow/ES",
        params={"date": DAY.isoformat(), "from": ts(0).isoformat(), "to": ts(2).isoformat()},
    ).json()
    window = payload["window"]
    assert window["cum_delta"] == pytest.approx(100.0)  # 150 − 50
    assert window["vol"] == pytest.approx(2000.0)  # bary minut 1 a 2
    assert window["opt_vol"] == pytest.approx(240.0)  # 120 + 120
    # Řady zůstávají v odpovědi beze změny (regresní chování)
    assert [row["cum_delta"] for row in payload["flow"]] == [50.0, 100.0, 150.0]

    assert (
        client.get(
            "/flow/ES", params={"date": DAY.isoformat(), "from": ts(2).isoformat()}
        ).status_code
        == 422
    )


def test_profile_window_pc_summary_golden(client: TestClient) -> None:
    """Golden #486: ručně spočítaný P/C souhrn okna (3 strikes) == výstup API.

    Fixture: vol(t,i,strana) = 10·(t+1)·(i+1) + 5 pro C; bid 10, ask 10.5
    → mid 10.25. Okno (t0, t2]: vol_okna = 20·(i+1) na obou stranách.
      volume/strana  = 20·(1+2+3) = 120
      premium/strana = 120 × 10.25 = 1230 (v bodech; multiplikátor řeší klient)
    """
    payload = client.get(
        "/profile/ES/20260716",
        params={
            "date": DAY.isoformat(),
            "from": ts(0).isoformat(),
            "to": ts(2).isoformat(),
            "variant": "vol",
        },
    ).json()
    summary = payload["window_summary"]
    assert summary["call_volume"] == pytest.approx(120.0)
    assert summary["put_volume"] == pytest.approx(120.0)
    assert summary["call_premium_points"] == pytest.approx(1230.0)
    assert summary["put_premium_points"] == pytest.approx(1230.0)
    assert summary["ratio_volume"] == pytest.approx(1.0)
    assert summary["ratio_premium"] == pytest.approx(1.0)
