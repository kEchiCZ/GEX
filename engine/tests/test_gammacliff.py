"""Testy gamma útesu (#576, fáze 1): golden výpočet, store, kolektor nad particemi."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.gammacliff import (
    CliffRecord,
    ExpiryAtSettle,
    build_cliff,
    is_opex_day,
    range_in_atr,
)
from gexlens_engine.config import Settings
from gexlens_engine.gammacliff import GammaCliffCollector, read_expiries_at, session_ranges
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.gammacliff_store import GammaCliffRepository
from gexlens_engine.storage.parquet_store import LevelsRow, SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository

SESSION = dt.date(2026, 7, 20)  # pondělí, CDT → settle 20:00 UTC


def expiry(
    key: str,
    gex: float,
    flip: float | None = None,
    cw: float | None = None,
    pw: float | None = None,
) -> ExpiryAtSettle:  # noqa: E501
    return ExpiryAtSettle(expiry=key, total_gex=gex, flip=flip, call_wall=cw, put_wall=pw)


# ── Golden výpočet (ručně spočtený příklad z AC) ───────────────────


def test_build_cliff_golden() -> None:
    """Settlující 0DTE nese 600 z 1000 → cliff 60 %; zdi se posunou na zbytkový profil."""
    expiries = [
        expiry("20260720", -600.0, flip=7600.0, cw=7650.0, pw=7550.0),  # dnes settluje
        expiry("20260721", 400.0, flip=7580.0, cw=7700.0, pw=7500.0),  # přeživší
    ]
    record = build_cliff(SESSION, "ES", expiries)
    assert record is not None
    assert record.gex_before == pytest.approx(1000.0)  # Σ |NetGEX|, znaménka nehrají roli
    assert record.gex_expiring == pytest.approx(600.0)
    assert record.cliff_share == pytest.approx(0.6)
    assert record.is_opex is False  # 20. 7. 2026 je pondělí
    # Posun struktury: zbytkový profil minus settlující řetěz
    assert record.flip_shift == pytest.approx(-20.0)
    assert record.call_wall_shift == pytest.approx(50.0)
    assert record.put_wall_shift == pytest.approx(-50.0)


def test_build_cliff_bez_settlujici_expirace_vraci_none() -> None:
    assert build_cliff(SESSION, "ES", [expiry("20260721", 400.0)]) is None


def test_build_cliff_bez_preživsi_expirace_ma_shifty_none() -> None:
    record = build_cliff(SESSION, "ES", [expiry("20260720", 500.0, flip=7600.0)])
    assert record is not None
    assert record.cliff_share == pytest.approx(1.0)
    assert record.flip_shift is None and record.call_wall_shift is None


def test_is_opex_treti_patek() -> None:
    assert is_opex_day(dt.date(2026, 7, 17)) is True  # 3. pátek července 2026
    assert is_opex_day(dt.date(2026, 7, 10)) is False  # 2. pátek
    assert is_opex_day(dt.date(2026, 7, 20)) is False  # pondělí


def test_range_in_atr_sma_predchozich() -> None:
    assert range_in_atr(30.0, [10.0, 20.0]) == pytest.approx(2.0)
    assert range_in_atr(30.0, []) is None


# ── Store ──────────────────────────────────────────────────────────


def make_record(day: dt.date) -> CliffRecord:
    return CliffRecord(
        session_date=day,
        symbol="ES",
        gex_before=1000.0,
        gex_expiring=600.0,
        cliff_share=0.6,
        is_opex=False,
        flip_shift=-20.0,
        call_wall_shift=50.0,
        put_wall_shift=-50.0,
    )


def test_store_upsert_idempotentni_a_next_metriky(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}")
    repository = GammaCliffRepository(db)
    repository.ensure_schema()
    now = dt.datetime(2026, 7, 20, 21, 0, tzinfo=dt.UTC)
    repository.upsert(make_record(SESSION), now)
    repository.upsert(make_record(SESSION), now)  # upsert, žádný duplikát
    assert repository.existing_dates("ES") == {SESSION}
    assert repository.missing_next_metrics("ES") == [SESSION]
    repository.update_next_metrics(
        SESSION, "ES", next_range_atr=1.4, next_setups={"wall_bounce": {"count": 2}}
    )
    assert repository.missing_next_metrics("ES") == []


# ── Kolektor nad particemi ─────────────────────────────────────────


def seed_levels(
    settings: Settings, symbol: str, expiry_key: str, day: dt.date, *, gex: float, flip: float
) -> None:  # noqa: E501
    writer = SnapshotWriter(settings)
    rows = [
        LevelsRow(
            dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=dt.UTC),
            flip - 5.0,  # dřívější minuta — settle stav ji musí přebít
            7000.0,
            6900.0,
            6950.0,
            gex / 2,
        ),
        LevelsRow(
            dt.datetime(day.year, day.month, day.day, 19, 59, tzinfo=dt.UTC),
            flip,
            7000.0,
            6900.0,
            6950.0,
            gex,
        ),
        LevelsRow(
            dt.datetime(day.year, day.month, day.day, 20, 30, tzinfo=dt.UTC),
            flip + 99.0,  # PO settle — nesmí se použít
            7000.0,
            6900.0,
            6950.0,
            gex * 9,
        ),
    ]
    writer.write_levels(symbol, expiry_key, day, rows)


def seed_bars(settings: Settings, symbol: str, day: dt.date, *, high: float, low: float) -> None:
    writer = SnapshotWriter(settings)
    ts = dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=dt.UTC)
    writer.write_bars(
        symbol, day, [Bar(ts=ts, open=low, high=high, low=low, close=high, volume=100.0)]
    )


def test_read_expiries_at_bere_posledni_radek_do_settle(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    seed_levels(settings, "ES", "20260720", SESSION, gex=-600.0, flip=7600.0)
    seed_levels(settings, "ES", "20260721", SESSION, gex=400.0, flip=7580.0)
    at_settle = dt.datetime(2026, 7, 20, 20, 0, tzinfo=dt.UTC)
    expiries = read_expiries_at(tmp_path, "ES", SESSION, at_settle)
    assert [(e.expiry, e.total_gex, e.flip) for e in expiries] == [
        ("20260720", -600.0, 7600.0),
        ("20260721", 400.0, 7580.0),
    ]


def test_collector_zapise_seanci_backfill_i_next_metriky(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path, database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}"
    )
    db = create_engine(settings.database_url)
    repository = GammaCliffRepository(db)
    repository.ensure_schema()
    SetupsRepository(db).ensure_schema()
    previous = dt.date(2026, 7, 17)  # pátek (OPEX!) — backfill kandidát
    seed_levels(settings, "ES", "20260717", previous, gex=-900.0, flip=7590.0)
    seed_levels(settings, "ES", "20260720", previous, gex=100.0, flip=7585.0)
    seed_levels(settings, "ES", "20260720", SESSION, gex=-600.0, flip=7600.0)
    seed_levels(settings, "ES", "20260721", SESSION, gex=400.0, flip=7580.0)
    # Bary: pátek rozsah 20 b, pondělí 30 b → pondělní range_atr = 30/20 = 1.5
    seed_bars(settings, "ES", previous, high=7620.0, low=7600.0)
    seed_bars(settings, "ES", SESSION, high=7630.0, low=7600.0)

    collector = GammaCliffCollector(symbol="ES", repository=repository, db=db, data_dir=tmp_path)
    now = dt.datetime(2026, 7, 20, 20, 10, tzinfo=dt.UTC)  # těsně po settle pondělí
    collector._run(SESSION, now)

    assert repository.existing_dates("ES") == {previous, SESSION}  # backfill + dnešek
    # Pátek (OPEX) dostal metriky následující seance (pondělí settled v `now`? ne —
    # settle pondělí 20:00 < now 20:10 → ano)
    assert repository.missing_next_metrics("ES") == [SESSION]
    ranges = session_ranges(tmp_path, "ES")
    assert [(day, round(value)) for day, value in ranges] == [(previous, 20), (SESSION, 30)]
