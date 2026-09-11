"""Testy gamma útesu (#576, fáze 1): golden výpočet, store, kolektor nad particemi."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.bandregime import GATE_BLOCK, band_class, band_gate_simple
from gexlens_engine.compute.gammacliff import (
    CliffRecord,
    ExpiryAtSettle,
    build_cliff,
    is_opex_day,
    is_outside_band,
    outside_share,
    range_in_atr,
)
from gexlens_engine.config import Settings
from gexlens_engine.gammacliff import (
    GammaCliffCollector,
    read_expiries_at,
    session_band_depths,
    session_ranges,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.gammacliff_store import GammaCliffRepository
from gexlens_engine.storage.parquet_store import FeatureRow, LevelsRow, SnapshotWriter
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


def test_outside_share_golden() -> None:
    """Ručně: 7 minut, 1 nezměřená → jmenovatel 6; mimo zónu 0,0 / −0,3 / −1,0 → 3/6."""
    depths = [1.5, 0.5, 0.0, -0.3, -1.0, None, 2.0]
    assert outside_share(depths) == pytest.approx(0.5)
    assert outside_share([2.0, 1.2, 0.01]) == pytest.approx(0.0)  # hrana All (0) ≠ 0,01
    assert outside_share([-1.0, -0.5]) == pytest.approx(1.0)


def test_outside_share_bez_zmerene_minuty_vraci_none() -> None:
    assert outside_share([]) is None
    assert outside_share([None, None]) is None


def test_is_outside_band_shodne_se_stinovou_branou_1060() -> None:
    """Jedno pravidlo na obou místech: outside ⇔ band_gate_simple(band_class) = block."""
    for depth in (-1.0, -0.99, -0.5, -0.0001, 0.0, 0.0001, 0.5, 1.0, 1.0001, 2.0):
        assert is_outside_band(depth) is (band_gate_simple(band_class(depth)) == GATE_BLOCK)


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
    # Řádek z doby před #1115: rozsah je, podíl mimo zónu ne → pořád „chybí"
    repository.update_next_metrics(
        SESSION, "ES", next_range_atr=1.4, next_setups={"wall_bounce": {"count": 2}}
    )
    assert repository.missing_next_metrics("ES") == [SESSION]
    repository.update_next_metrics(
        SESSION,
        "ES",
        next_range_atr=1.4,
        next_setups={"wall_bounce": {"count": 2}},
        next_outside_share=0.25,
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


def feature_row(ts: dt.datetime, depth: float | None) -> FeatureRow:
    return FeatureRow(
        ts=ts,
        expiry="20260720",
        open=7600.0,
        high=7601.0,
        low=7599.0,
        close=7600.0,
        flip=None,
        call_wall=None,
        put_wall=None,
        max_pain=None,
        cum_delta=0.0,
        call_flow=0.0,
        put_flow=0.0,
        opt_vol=0.0,
        minutes_to_expiry=None,
        call_wall_dom=None,
        put_wall_dom=None,
        gex_regime=None,
        gamma_edge_up=None,
        gamma_edge_dn=None,
        atr=None,
        band_sharpness=None,
        band_sharpness_pct=None,
        band_depth=depth,
        band_metrics_version=3 if depth is not None else None,
    )


def seed_features(settings: Settings, symbol: str, rows: list[FeatureRow]) -> None:
    """Zápis po UTC dnech — přesně tak, jak feature log klíčuje partice (`now.date()`)."""
    writer = SnapshotWriter(settings)
    by_day: dict[dt.date, list[FeatureRow]] = {}
    for row in rows:
        by_day.setdefault(row.ts.date(), []).append(row)
    for day, day_rows in by_day.items():
        writer.write_features(symbol, day, day_rows)


def test_session_band_depths_hranice_seance_jako_next_range_atr(tmp_path: Path) -> None:
    """Globex seance = od 17:00 CT předchozího dne do settle 16:00 ET; přes dvě UTC partice."""
    settings = Settings(data_dir=tmp_path)
    utc = dt.UTC
    seed_features(
        settings,
        "ES",
        [
            feature_row(dt.datetime(2026, 7, 19, 21, 59, tzinfo=utc), 9.0),  # před Globex open
            feature_row(dt.datetime(2026, 7, 19, 22, 0, tzinfo=utc), -0.5),  # open neděle 17:00 CT
            feature_row(dt.datetime(2026, 7, 20, 15, 0, tzinfo=utc), 1.5),
            feature_row(dt.datetime(2026, 7, 20, 19, 59, tzinfo=utc), None),  # nezměřeno
            feature_row(dt.datetime(2026, 7, 20, 20, 0, tzinfo=utc), 0.0),  # settle včetně
            feature_row(dt.datetime(2026, 7, 20, 20, 1, tzinfo=utc), -1.0),  # po settle — ne
        ],
    )
    assert session_band_depths(tmp_path, "ES", SESSION) == [-0.5, 1.5, None, 0.0]
    assert outside_share(session_band_depths(tmp_path, "ES", SESSION)) == pytest.approx(2 / 3)
    assert session_band_depths(tmp_path, "ES", dt.date(2026, 7, 21)) == []


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
    # Bez profilu partice hrubá gamma chybí → build_cliff spadne na |NetGEX|
    assert [e.gross_gex for e in expiries] == [None, None]


def test_hruba_gamma_z_profilu_prebije_vynulovany_netgex(tmp_path: Path) -> None:
    """#576 fix: ES OPEX 21. 8. měl NetGEX řetězu ~0 (call/put se vynulovaly), profil ne."""
    from gexlens_engine.compute.gammacliff import gex_magnitude
    from gexlens_engine.storage.parquet_store import GexProfileRow

    settings = Settings(data_dir=tmp_path)
    seed_levels(settings, "ES", "20260720", SESSION, gex=62.0, flip=7600.0)  # net skoro nula
    seed_levels(settings, "ES", "20260721", SESSION, gex=400.0, flip=7580.0)
    writer = SnapshotWriter(settings)
    at_settle = dt.datetime(2026, 7, 20, 20, 0, tzinfo=dt.UTC)
    # Profil settlující expirace: +3000 nad cenou, −3000 pod ní → net 0, hrubá 6000
    writer.write_gexprofile(
        "ES",
        "20260720",
        SESSION,
        [
            GexProfileRow(
                ts_min=at_settle - dt.timedelta(minutes=2),
                grid_start=7500.0,
                grid_step=50.0,
                values=[-1000.0, -2000.0, 2000.0, 1000.0],
            ),  # noqa: E501
            GexProfileRow(
                ts_min=at_settle + dt.timedelta(minutes=30),
                grid_start=7500.0,
                grid_step=50.0,
                values=[0.0, 0.0, 0.0, 0.0],
            ),  # po settle se nepočítá  # noqa: E501
        ],
    )
    writer.write_gexprofile(
        "ES",
        "20260721",
        SESSION,
        [
            GexProfileRow(
                ts_min=at_settle - dt.timedelta(minutes=1),
                grid_start=7500.0,
                grid_step=50.0,
                values=[100.0, 300.0],
            )
        ],  # noqa: E501
    )
    expiries = read_expiries_at(tmp_path, "ES", SESSION, at_settle)
    assert [e.gross_gex for e in expiries] == [6000.0, 400.0]
    assert gex_magnitude(expiries[0]) == 6000.0
    record = build_cliff(SESSION, "ES", expiries)
    assert record is not None
    assert record.gex_expiring == pytest.approx(6000.0)
    assert record.cliff_share == pytest.approx(6000.0 / 6400.0)


def test_recompute_prepise_historii_a_necha_next_metriky(tmp_path: Path) -> None:
    from gexlens_engine.storage.parquet_store import GexProfileRow

    settings = Settings(
        data_dir=tmp_path, database_url=f"sqlite+pysqlite:///{tmp_path / 'm.sqlite'}"
    )
    engine = create_engine(settings.database_url)
    repository = GammaCliffRepository(engine)
    repository.ensure_schema()
    seed_levels(settings, "ES", "20260720", SESSION, gex=62.0, flip=7600.0)
    seed_levels(settings, "ES", "20260721", SESSION, gex=400.0, flip=7580.0)
    now = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.UTC)
    collector = GammaCliffCollector(
        symbol="ES", repository=repository, db=engine, data_dir=tmp_path
    )
    collector._run_backfill(now)
    repository.update_next_metrics(SESSION, "ES", next_range_atr=1.3, next_setups=None)
    # Profil dorazí (např. nový výpočet) → přepočet změní cliff_share, next_range_atr zůstane
    SnapshotWriter(settings).write_gexprofile(
        "ES",
        "20260720",
        SESSION,
        [
            GexProfileRow(
                ts_min=dt.datetime(2026, 7, 20, 19, 59, tzinfo=dt.UTC),
                grid_start=7500.0,
                grid_step=50.0,
                values=[-3000.0, 3000.0],
            )
        ],  # noqa: E501
    )
    assert collector.recompute(now) == 1
    from sqlalchemy import select

    from gexlens_engine.storage.gammacliff_store import gamma_cliff_table

    with engine.connect() as conn:
        row = conn.execute(select(gamma_cliff_table)).mappings().one()
    assert row["gex_expiring"] == pytest.approx(6000.0)
    assert row["cliff_share"] == pytest.approx(6000.0 / 6400.0)
    assert row["next_range_atr"] == pytest.approx(1.3)


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
    # Feature log pondělí: 4 změřené minuty, 1 mimo zónu → next_outside_share 0,25 (#1115)
    seed_features(
        settings,
        "ES",
        [
            feature_row(dt.datetime(2026, 7, 20, 14, 30 + i, tzinfo=dt.UTC), depth)
            for i, depth in enumerate((1.8, 1.2, -0.4, 0.6))
        ],
    )

    collector = GammaCliffCollector(symbol="ES", repository=repository, db=db, data_dir=tmp_path)
    now = dt.datetime(2026, 7, 20, 20, 10, tzinfo=dt.UTC)  # těsně po settle pondělí
    collector._run(SESSION, now)

    assert repository.existing_dates("ES") == {previous, SESSION}  # backfill + dnešek
    # Pátek (OPEX) dostal metriky následující seance (pondělí settled v `now`? ne —
    # settle pondělí 20:00 < now 20:10 → ano)
    assert repository.missing_next_metrics("ES") == [SESSION]
    ranges = session_ranges(tmp_path, "ES")
    assert [(day, round(value)) for day, value in ranges] == [(previous, 20), (SESSION, 30)]
    from sqlalchemy import select

    from gexlens_engine.storage.gammacliff_store import gamma_cliff_table

    with db.connect() as conn:
        row = (
            conn.execute(
                select(gamma_cliff_table).where(gamma_cliff_table.c.session_date == previous)
            )
            .mappings()
            .one()
        )
    assert row["next_range_atr"] == pytest.approx(1.5)
    assert row["next_outside_share"] == pytest.approx(0.25)


def test_collector_doplni_outside_share_do_starsich_radku(tmp_path: Path) -> None:
    """Backfill #1115: řádek s `next_range_atr`, ale bez podílu mimo zónu, se doplní."""
    settings = Settings(
        data_dir=tmp_path, database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}"
    )
    db = create_engine(settings.database_url)
    repository = GammaCliffRepository(db)
    repository.ensure_schema()
    SetupsRepository(db).ensure_schema()
    previous = dt.date(2026, 7, 17)
    now = dt.datetime(2026, 7, 20, 20, 10, tzinfo=dt.UTC)
    repository.upsert(make_record(previous), now)
    repository.update_next_metrics(previous, "ES", next_range_atr=1.5, next_setups=None)
    seed_bars(settings, "ES", previous, high=7620.0, low=7600.0)
    seed_bars(settings, "ES", SESSION, high=7630.0, low=7600.0)
    seed_features(
        settings, "ES", [feature_row(dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC), -0.2)]
    )
    collector = GammaCliffCollector(
        symbol="ES", repository=repository, db=db, data_dir=tmp_path, backfill=False
    )
    collector._fill_next_metrics(now)
    from sqlalchemy import select

    from gexlens_engine.storage.gammacliff_store import gamma_cliff_table

    with db.connect() as conn:
        row = conn.execute(select(gamma_cliff_table)).mappings().one()
    assert row["next_range_atr"] == pytest.approx(1.5)
    assert row["next_outside_share"] == pytest.approx(1.0)
    assert repository.missing_next_metrics("ES") == []
