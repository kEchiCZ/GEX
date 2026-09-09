"""Svíčky vyšších timeframů (#1089): seance přes půlnoc UTC, koše od otevření, týden."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from gexlens_api.candles import (
    TIMEFRAMES,
    build_candles,
    calendar_days_needed,
    session_dates,
)
from gexlens_api.main import create_app
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import SnapshotWriter

# Pondělí 2026-07-13 … pátek 2026-07-17 (letní čas: otevření Globex 22:00 UTC)
WEEK_DAYS = [dt.date(2026, 7, 13) + dt.timedelta(days=i) for i in range(5)]


def _session_bars(day: dt.date, base: float, step_minutes: int = 30) -> list[Bar]:
    """Bary celé Globex seance dne `day` po `step_minutes`; cena roste o 1/bar."""
    start, end = session_bounds(day)
    bars: list[Bar] = []
    ts = start
    index = 0
    while ts < end:
        price = base + index
        bars.append(
            Bar(ts=ts, open=price, high=price + 2, low=price - 2, close=price + 1, volume=10.0)
        )
        ts += dt.timedelta(minutes=step_minutes)
        index += 1
    return bars


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    writer = SnapshotWriter(s)
    for offset, day in enumerate(WEEK_DAYS):
        # Partice podle UTC dne barů (#1002): večer seance leží v partici D−1
        writer.write_bars_by_day("ES", _session_bars(day, 7000.0 + 100 * offset))
    return s


def test_session_dates_prirazuje_vecer_nasledujici_seanci() -> None:
    """Minuta po 17:00 CT patří seanci dalšího dne; před ní dnešní (ADR-0023)."""
    ts = pd.Series(
        pd.to_datetime(
            ["2026-07-13T21:59:00Z", "2026-07-13T22:00:00Z", "2026-07-14T13:30:00Z"], utc=True
        )
    )
    assert list(session_dates(ts)) == [
        dt.date(2026, 7, 13),
        dt.date(2026, 7, 14),
        dt.date(2026, 7, 14),
    ]


def test_denni_svicky_sledi_globex_seanci(client: TestClient) -> None:
    payload = client.get("/candles/ES", params={"tf": "D", "limit": 10}).json()
    candles = payload["candles"]
    assert [row["ts"][:10] for row in candles] == [
        "2026-07-12",
        "2026-07-13",
        "2026-07-14",
        "2026-07-15",
        "2026-07-16",
    ]  # ts = UTC otevření seance = večer D−1
    first = candles[0]
    # Seance pondělí = 24 h [22:00 UTC neděle, 22:00 UTC pondělí): 48 barů po 30 min
    assert first["open"] == 7000.0
    assert first["high"] == 7000.0 + 47 + 2
    assert first["low"] == 7000.0 - 2
    assert first["close"] == 7000.0 + 47 + 1
    assert first["volume"] == 480.0
    assert first["partial"] is False


def test_intradenni_kose_zarovnane_na_otevreni_seance(client: TestClient) -> None:
    payload = client.get("/candles/ES", params={"tf": "240", "limit": 6}).json()
    candles = payload["candles"]
    assert len(candles) == 6
    # Poslední seance (pátek 17. 7.): koše 22:00, 02:00, 06:00, 10:00, 14:00, 18:00 UTC
    hours = [row["ts"][11:16] for row in candles]
    assert hours == ["22:00", "02:00", "06:00", "10:00", "14:00", "18:00"]
    # Koš 4 h při 30min barech = 8 barů → volume 80, open = první bar koše
    assert candles[0]["volume"] == 80.0
    assert candles[1]["open"] == candles[0]["open"] + 8
    assert candles[-1]["volume"] == 80.0


def test_tydenni_svicka_z_dennich(client: TestClient) -> None:
    payload = client.get("/candles/ES", params={"tf": "W", "limit": 4}).json()
    candles = payload["candles"]
    assert len(candles) == 1
    week = candles[0]
    assert week["ts"][:10] == "2026-07-12"  # otevření nedělní seance = pondělní seance
    assert week["open"] == 7000.0
    assert week["close"] == 7400.0 + 47 + 1
    assert week["high"] == 7400.0 + 47 + 2
    assert week["low"] == 7000.0 - 2
    assert week["volume"] == 5 * 480.0


def test_neznamy_tf_a_symbol(client: TestClient) -> None:
    assert client.get("/candles/ES", params={"tf": "3"}).status_code == 422
    assert client.get("/candles/NEZNAMY", params={"tf": "D"}).status_code == 404


def test_calendar_days_needed_pokryva_limit() -> None:
    assert calendar_days_needed("D", 120) == 123
    assert calendar_days_needed("W", 52) == 52 * 7 + 3
    # 60 čtyřhodinových svíček = 240 h ≈ 10,4 seance → 11 + 3
    assert calendar_days_needed("240", 60) == 14
    assert set(TIMEFRAMES) == {"W", "D", "240", "60", "15"}


def test_daily_partials_slouci_seanci_ze_dvou_partic(settings: Settings) -> None:
    """Večer seance leží v partici D−1, zbytek v D — open/close musí sedět."""
    from gexlens_api.candles import build_daily_candles
    from gexlens_api.data import DataRepository

    repo = DataRepository(settings)
    partials = repo.daily_partials("ES", 10)
    # 5 seancí × 2 části (večer v D−1, den v D) = 10 řádků
    assert len(partials) == 10
    [monday, *_rest] = build_daily_candles(partials, "D", 10)
    assert monday["open"] == 7000.0
    assert monday["close"] == 7000.0 + 47 + 1
    assert monday["volume"] == 480.0
    # Druhé volání jde z cache (stejný mtime) a dává totéž
    assert repo.daily_partials("ES", 10).equals(partials)


def test_partition_key_statuje_jen_dopisovane_dny(settings: Settings) -> None:
    """Minulé partice mají klíč bez mtime (žádný stat), nedávné s mtime."""
    from gexlens_api.data import MUTABLE_PARTITION_DAYS, DataRepository

    repo = DataRepository(settings)
    old_key = repo._partition_key("ES", WEEK_DAYS[0])
    assert old_key is not None and old_key[1] == 0
    today = dt.datetime.now(dt.UTC).date()
    # Seance včerejška začíná předvčerejším večerem UTC → partice = today − 2 (dopisovaná)
    recent = today - dt.timedelta(days=1)
    bars = _session_bars(recent, 8000.0)
    partition_day = bars[0].ts.astimezone(dt.UTC).date()
    assert partition_day >= today - dt.timedelta(days=MUTABLE_PARTITION_DAYS)
    writer = SnapshotWriter(settings)
    writer.write_bars_by_day("ES", bars[:3])
    repo._listing_cache.clear()
    recent_key = repo._partition_key("ES", partition_day)
    assert recent_key is not None and recent_key[1] > 0
    # Dopsaná partice → nový mtime → jiný klíč → cache se obnoví
    first = repo.daily_partials("ES", 400)
    writer.write_bars_by_day("ES", bars[3:6])
    path = repo._bars_path("ES", partition_day)
    import os

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = repo.daily_partials("ES", 400)
    assert second["volume"].sum() > first["volume"].sum()


def test_build_candles_prazdny_vstup() -> None:
    frame = pd.DataFrame(columns=["ts_min", "open", "high", "low", "close", "volume"])
    assert build_candles(frame, "D", 5) == []


def test_duplicitni_minuta_se_pocita_jednou() -> None:
    """#1002: stejná minuta ve dvou částech vstupu → vyhrává první výskyt."""
    ts = pd.to_datetime(["2026-07-14T13:30:00Z", "2026-07-14T13:30:00Z"], utc=True)
    frame = pd.DataFrame(
        {
            "ts_min": ts,
            "open": [1.0, 9.0],
            "high": [2.0, 9.0],
            "low": [0.5, 9.0],
            "close": [1.5, 9.0],
            "volume": [10.0, 10.0],
        }
    )
    [candle] = build_candles(frame, "D", 5)
    assert candle["volume"] == 10.0
    assert candle["open"] == 1.0


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
