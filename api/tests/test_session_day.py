"""Obchodní den = Globex seance (#512, ADR-0023 bod 3).

Osa dne D = [17:00 America/Chicago dne D−1, 17:00 CT dne D). Úložiště zůstává
klíčované UTC dnem; čtecí vrstva sešívá partici D s večerem D−1 a odřezává
večer D (patří seanci D+1). Testy kryjí obě AC: pondělní osa obsahuje nedělní
večerní bary a kumulativy přes hranici partic nejsou započtené dvakrát.
"""

import base64
import datetime as dt
import io
from pathlib import Path

import pandas as pd
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient

from gexlens_api.data import session_bounds
from gexlens_api.main import create_app
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import FlowRowLike, SnapshotRow, SnapshotWriter

# Pondělí 20. 7. 2026; seance = [ne 19. 7. 22:00 UTC, po 20. 7. 22:00 UTC) — CDT (UTC−5)
MONDAY = dt.date(2026, 7, 20)
SUNDAY = dt.date(2026, 7, 19)


class _Flow:
    def __init__(self, ts_min: dt.datetime, flow_delta: float, cum_delta: float) -> None:
        self.ts_min = ts_min
        self.flow_delta = flow_delta
        self.cum_delta = cum_delta


def _snapshot(ts_min: dt.datetime, volume: float) -> SnapshotRow:
    return SnapshotRow(
        ts_min=ts_min,
        strike=7600.0,
        right="C",
        bid=10.0,
        ask=10.5,
        last=10.25,
        volume=volume,
        iv=0.15,
        delta=0.5,
        gamma=0.01,
        theta=-0.5,
        vega=1.2,
        oi=100.0,
        stale_age=0.0,
    )


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    writer = SnapshotWriter(s)
    # Nedělní partice: odpoledne PŘED openem (mimo seanci) + večer po openu.
    # Volume je kumulativ seance (IBKR daily counter) — večer navazuje do pondělka.
    writer.write_minute("ES", "20260720", SUNDAY, [_snapshot(at(SUNDAY, 21, 30), 999.0)])
    writer.write_minute("ES", "20260720", SUNDAY, [_snapshot(at(SUNDAY, 23, 0), 10.0)])
    # Pondělní partice: dopoledne v seanci + večer po 22:00 UTC (už seance úterka)
    writer.write_minute("ES", "20260720", MONDAY, [_snapshot(at(MONDAY, 15, 0), 40.0)])
    writer.write_minute("ES", "20260720", MONDAY, [_snapshot(at(MONDAY, 22, 30), 5.0)])
    flow_sunday: list[FlowRowLike] = [_Flow(at(SUNDAY, 23, 0), 5.0, 5.0)]
    flow_monday: list[FlowRowLike] = [
        _Flow(at(MONDAY, 15, 0), 3.0, 8.0),
        _Flow(at(MONDAY, 22, 30), 4.0, 12.0),
    ]
    writer.write_flow("ES", SUNDAY, flow_sunday)
    writer.write_flow("ES", MONDAY, flow_monday)
    writer.write_bars(
        "ES", SUNDAY, [Bar(ts=at(SUNDAY, 23, 0), open=1, high=1, low=1, close=1, volume=10.0)]
    )
    writer.write_bars(
        "ES",
        MONDAY,
        [
            Bar(ts=at(MONDAY, 15, 0), open=1, high=1, low=1, close=2, volume=40.0),
            Bar(ts=at(MONDAY, 22, 30), open=1, high=1, low=1, close=3, volume=5.0),
        ],
    )
    return s


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_session_bounds_respektuje_dst() -> None:
    # Červenec = CDT (UTC−5): open 22:00 UTC; leden = CST (UTC−6): open 23:00 UTC
    start, end = session_bounds(dt.date(2026, 7, 20))
    assert start == dt.datetime(2026, 7, 19, 22, 0, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 7, 20, 22, 0, tzinfo=dt.UTC)
    start_winter, _ = session_bounds(dt.date(2026, 1, 20))
    assert start_winter == dt.datetime(2026, 1, 19, 23, 0, tzinfo=dt.UTC)


def test_pondelni_osa_obsahuje_nedelni_vecer(client: TestClient) -> None:
    """AC 1: nedělní open (večer po 17:00 CT) patří pondělní ose; okraje ven."""
    payload = client.get(f"/replay/ES/20260720/{MONDAY.isoformat()}").json()
    with pyarrow.ipc.open_stream(
        io.BytesIO(base64.b64decode(payload["snapshots_arrow_base64"]))
    ) as reader:
        snapshots = reader.read_all().to_pandas()
    minutes = pd.to_datetime(snapshots["ts_min"]).dt.tz_convert("UTC").tolist()
    assert minutes == [at(SUNDAY, 23, 0), at(MONDAY, 15, 0)]  # chronologicky, sešité
    # 21:30 (před openem) i pondělních 22:30 (seance úterka) jsou venku
    assert at(SUNDAY, 21, 30) not in minutes
    assert at(MONDAY, 22, 30) not in minutes


def test_kontinuita_kumulativu_pres_hranici(client: TestClient) -> None:
    """AC 2: CumΔ a volume přes hranici partic bez dvojího započtení."""
    payload = client.get(f"/replay/ES/20260720/{MONDAY.isoformat()}").json()
    cums = [row["cum_delta"] for row in payload["flow"]]
    assert cums == [5.0, 8.0]  # večer D−1 → dopoledne D: monotónně, žádný skok ani reset
    assert all(later >= earlier for earlier, later in zip(cums, cums[1:], strict=False))
    vols = [row["volume"] for row in payload["bars"]]
    assert vols == [10.0, 40.0]
    # /flow endpoint sdílí touž osu
    flow_payload = client.get(f"/flow/ES?date={MONDAY.isoformat()}").json()
    assert [row["cum_delta"] for row in flow_payload["flow"]] == [5.0, 8.0]
    assert [row["vol"] for row in flow_payload["vol"]] == [10.0, 40.0]


def test_seance_jen_z_vecera_d_minus_1(settings: Settings) -> None:
    """Pondělní ráno před prvním zápisem: partice D neexistuje, večer D−1 stačí (200, ne 404)."""
    writer = SnapshotWriter(settings)
    tuesday = MONDAY + dt.timedelta(days=1)
    # Pondělní večer po openu = seance úterka; úterní partice zatím neexistuje
    client = TestClient(create_app(settings))
    payload = client.get(f"/replay/ES/20260720/{tuesday.isoformat()}").json()
    with pyarrow.ipc.open_stream(
        io.BytesIO(base64.b64decode(payload["snapshots_arrow_base64"]))
    ) as reader:
        snapshots = reader.read_all().to_pandas()
    minutes = pd.to_datetime(snapshots["ts_min"]).dt.tz_convert("UTC").tolist()
    assert minutes == [at(MONDAY, 22, 30)]
    assert writer is not None  # writer jen kvůli konzistentnímu vzoru fixture


def test_replay_cache_hlavicky(client: TestClient, settings: Settings) -> None:
    """#514: uzavřená seance immutable; živý den ETag + 304 při shodě."""
    payload = client.get(f"/replay/ES/20260720/{MONDAY.isoformat()}")
    assert payload.status_code == 200
    assert payload.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "etag" not in payload.headers

    # Živý den: seance ještě neskončila → no-cache + ETag; shoda → 304
    from gexlens_engine.compute.settle import trading_session_date

    now = dt.datetime.now(dt.UTC)
    live_session = trading_session_date(now)
    writer = SnapshotWriter(settings)
    writer.write_minute(
        "ES", "20260720", now.date(), [_snapshot(now - dt.timedelta(minutes=3), 70.0)]
    )
    live = client.get(f"/replay/ES/20260720/{live_session.isoformat()}")
    assert live.status_code == 200
    assert live.headers["cache-control"] == "no-cache"
    etag = live.headers["etag"]
    cached = client.get(
        f"/replay/ES/20260720/{live_session.isoformat()}", headers={"If-None-Match": etag}
    )
    assert cached.status_code == 304
