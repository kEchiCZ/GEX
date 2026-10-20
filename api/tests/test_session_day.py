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
        # CVD podkladu (#829) — fixture běží bez tasty větve
        self.futures_cvd_delta: float | None = None
        self.futures_cvd: float | None = None
        # Zdroj znaménka (ADR-0032) — fixture jako partice před #615 fází 3
        self.source: str | None = None
        # Pokrytí trade větví (#1071) — fixture bez tasty větve = NULL
        self.printed_volume: float | None = None
        self.unknown_volume: float | None = None
        self.structured_volume: float | None = None
        self.fallback_volume: float | None = None
        self.dropped_no_delta: int | None = None


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


def test_daily_prubezne_maximum_nepretece_pres_hranici_seance(settings: Settings) -> None:
    """#1602: reference přírůstku žije jen v seanci — kumulativ 999 z doby před
    nedělním openem (21:30 UTC) ani 5 z úterní seance (pondělí 22:30 UTC) Daily
    pondělka nepotlačí ani nenafouknou; první výskyt v seanci (10) jen založí stav."""
    from gexlens_api import main as api_main

    api_main._DAILY_BUNDLE_CACHE.clear()  # uzavřený den z jiného testu by vrátila cache
    client = TestClient(create_app(settings))
    version = api_main.DAILY_BUNDLE_VERSION

    def daily(day: dt.date) -> dict[str, float]:
        url = f"/replay/ES/20260720/{day.isoformat()}?resolution=daily&v={version}"
        payload: dict[str, float] = client.get(url).json()["daily"]
        return payload

    assert daily(MONDAY)["opt_vol_call"] == pytest.approx(30.0)  # 10 → 40
    assert daily(MONDAY + dt.timedelta(days=1))["opt_vol_call"] == pytest.approx(0.0)
    flow = client.get(f"/flow/ES?date={MONDAY.isoformat()}").json()
    assert [row["opt_vol"] for row in flow["opt_vol"]] == pytest.approx([0.0, 30.0])


def test_daily_immutable_jen_s_aktualni_verzi(client: TestClient) -> None:
    """#1602: uzavřený Daily balík jde `immutable` jen s `v` = DAILY_BUNDLE_VERSION.
    Starý frontend (bez `v`) nebo návrat jen jedné služby (jiné `v`) dostane
    `no-cache`, takže si prohlížeč cizí verzi součtů nezapeče na rok."""
    from gexlens_api import main as api_main

    api_main._DAILY_BUNDLE_CACHE.clear()
    url = f"/replay/ES/20260720/{MONDAY.isoformat()}?resolution=daily"
    current = client.get(f"{url}&v={api_main.DAILY_BUNDLE_VERSION}")
    assert current.headers["cache-control"] == "public, max-age=31536000, immutable"
    for stale in (url, f"{url}&v={api_main.DAILY_BUNDLE_VERSION - 1}"):
        response = client.get(stale)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert response.json()["daily"] == current.json()["daily"]


def test_daily_verze_sedi_s_frontendem() -> None:
    """#1602: API a frontend nesou touž verzi Daily balíku — rozjetá čísla by
    vrátila buď staré součty z cache prohlížeče, nebo Daily bez cache."""
    import re

    from gexlens_api.main import DAILY_BUNDLE_VERSION

    loader = Path(__file__).parents[2] / "frontend" / "src" / "replay" / "loader.ts"
    match = re.search(
        r"^export const DAILY_BUNDLE_VERSION = (\d+)$", loader.read_text("utf-8"), re.M
    )
    assert match is not None, "DAILY_BUNDLE_VERSION ve frontend/src/replay/loader.ts chybí"
    assert int(match.group(1)) == DAILY_BUNDLE_VERSION


def test_zdroj_cumdelta_sedi_s_frontendem() -> None:
    """#1588: frontend skrývá midpointovou historii podle `source` řádků flow —
    hodnota zdroje tisků musí sedět s enginem, jinak by skryl i dny z tisků."""
    import re

    from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR

    loader = Path(__file__).parents[2] / "frontend" / "src" / "replay" / "loader.ts"
    match = re.search(
        r"^export const CUMDELTA_SOURCE_AGGRESSOR = '(\w+)'$", loader.read_text("utf-8"), re.M
    )
    assert match is not None, "CUMDELTA_SOURCE_AGGRESSOR ve frontend/src/replay/loader.ts chybí"
    assert match.group(1) == SOURCE_AGGRESSOR
