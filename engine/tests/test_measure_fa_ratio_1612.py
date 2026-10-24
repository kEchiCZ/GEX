"""Rozbor poměrů ΔOI/net kalibrace α (#1612) — `scripts/measure_fa_ratio_1612.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_recompute_fa).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.cumdelta import SOURCE_MIDPOINT
from gexlens_engine.compute.facalibration import AlphaCalibrationPoint
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import FaAlphaRepository
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter, TastyTradeRow

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_fa_ratio_1612.py"
PREV = dt.date(2026, 10, 1)  # čtvrtek, bod D
TODAY = dt.date(2026, 10, 2)  # pátek, archiv D+1
EXPIRY = "20261002"  # první expirace po D = sekundár celé seance D
TRADING_CLASS = "QN1"
STRIKES = [29000.0 + 10 * i for i in range(6)]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("measure_fa_ratio_1612", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_group_stats_deli_strany_podle_znamenka_netu() -> None:
    module = _load()
    # Dvě buy strany, dvě sell strany, OI roste všude; strana pod prahem |net| se nepočítá
    stats = module.group_stats(
        [(100.0, 40.0), (50.0, 10.0), (-100.0, 30.0), (-50.0, -5.0), (10.0, 99.0)]
    )
    assert stats.samples == 4
    assert stats.buy_sides == 2 and stats.sell_sides == 2
    assert stats.ratio_buy == pytest.approx((0.4 + 0.2) / 2)
    assert stats.ratio_sell == pytest.approx((-0.3 + 0.1) / 2)
    assert stats.up_buy == 1.0 and stats.up_sell == 0.5
    # ΔOI/|net|: 0,4 · 0,2 · 0,3 · −0,1 → medián 0,25
    assert stats.unsigned_median == pytest.approx(0.25)
    empty = module.group_stats([])
    assert empty.samples == 0 and empty.median is None and empty.up_buy is None


def test_two_factor_fit_kladne_s_znamena_ze_prodej_oi_zveda() -> None:
    module = _load()
    # ΔOI = 0,5·net⁺ + 0,3·|net⁻| přesně
    pairs = [(100.0, 50.0), (200.0, 100.0), (-100.0, 30.0), (-300.0, 90.0)]
    b, s = module.two_factor_fit(pairs)
    assert b == pytest.approx(0.5) and s == pytest.approx(0.3)
    assert module.two_factor_fit([(100.0, 50.0)]) is None  # chybí sell strany


def test_spearman_a_pasma_vzdalenosti() -> None:
    module = _load()
    assert module.spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert module.spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert module.spearman([1.0, 2.0], [1.0, 2.0]) is None
    assert module.distance_bucket(29000.0, 29000.0) == 0
    assert module.distance_bucket(29100.0, 29000.0) == 1  # 0,34 %
    assert module.distance_bucket(29200.0, 29000.0) == 2  # 0,69 %
    assert module.distance_bucket(30000.0, 29000.0) == 3  # 3,4 %


def test_parse_streamer() -> None:
    module = _load()
    assert module.parse_streamer("./QN1V26C29010:XCME") == ("QN1", 10, 2026, "C", 29010.0)
    assert module.parse_streamer("./E1BV26P7785.5:XCME") == ("E1B", 10, 2026, "P", 7785.5)
    assert module.parse_streamer("/NQZ26:XCME") is None


def _seed(tmp_path: Path) -> tuple[str, Settings]:
    settings = Settings(data_dir=tmp_path / "data")
    url = f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"
    oi_repo = OIEodRepository(create_engine(url))
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(create_engine(url))
    alpha_repo.ensure_schema()
    # Uložený bod z doby před #1605 (medián 0 z tasty stran) — report ho ukáže vedle přepočtu
    alpha_repo.record(
        "NQ",
        PREV,
        EXPIRY,
        AlphaCalibrationPoint(48, 0.0, None, None),
        0.16,
        5,
        source=SOURCE_MIDPOINT,
    )
    after_pub = {
        day: settings.oi_publication_utc(day) + dt.timedelta(hours=1) for day in (PREV, TODAY)
    }
    oi_repo.upsert_many(
        [OIRecord("NQ", EXPIRY, s, "C", PREV, 100.0, trading_class=TRADING_CLASS) for s in STRIKES],
        after_pub[PREV],
    )
    # ΔOI +20 na každé straně, poslední strana jen před publikací D+1 → „nevíme“
    oi_repo.upsert_many(
        [
            OIRecord("NQ", EXPIRY, s, "C", TODAY, 120.0, trading_class=TRADING_CLASS)
            for s in STRIKES[:-1]
        ],
        after_pub[TODAY],
    )
    oi_repo.upsert_many(
        [OIRecord("NQ", EXPIRY, STRIKES[-1], "C", TODAY, 120.0, trading_class=TRADING_CLASS)],
        dt.datetime.combine(TODAY, dt.time(5, 0), tzinfo=dt.UTC),
    )
    writer = SnapshotWriter(settings)
    at_20 = dt.datetime.combine(PREV, dt.time(20, 0), tzinfo=dt.UTC)
    at_21 = dt.datetime.combine(PREV, dt.time(21, 0), tzinfo=dt.UTC)
    # Net 21:00: tři buy strany, tři sell strany (midpoint); ve 20:00 polovina
    nets = [100.0, 50.0, 40.0, -100.0, -50.0, -40.0]
    for ts, factor in ((at_20, 0.5), (at_21, 1.0)):
        writer.write_netflow(
            "NQ",
            EXPIRY,
            PREV,
            [
                NetFlowRow(
                    ts_min=ts, strike=s, right="C", net_volume=n * factor, source=SOURCE_MIDPOINT
                )
                for s, n in zip(STRIKES, nets, strict=True)
            ],
        )
    # Tisky: seance D začíná 30. 9. 22:00 UTC; tisk 30. 9. 21:30 (předchozí seance) a po řezu
    # se nepočítá, tisk jiné expirace taky ne
    session_open = dt.datetime(2026, 9, 30, 22, 30, tzinfo=dt.UTC)
    trades = {
        dt.date(2026, 9, 30): [
            TastyTradeRow(
                dt.datetime(2026, 9, 30, 21, 30, tzinfo=dt.UTC),
                "./QN1V26C29000:XCME",
                1.0,
                500.0,
                "BUY",
                False,
                True,
            ),
            TastyTradeRow(session_open, "./QN1V26C29000:XCME", 1.0, 30.0, "BUY", False, True),
        ],
        PREV: [
            TastyTradeRow(at_20, "./QN1V26C29000:XCME", 1.0, 5.0, "SELL", False, False),
            TastyTradeRow(at_20, "./QN1V26C29010:XCME", 1.0, 40.0, "SELL", False, False),
            TastyTradeRow(at_20, "./Q1DV26C29000:XCME", 1.0, 900.0, "BUY", False, False),
            TastyTradeRow(
                at_21 + dt.timedelta(seconds=1),
                "./QN1V26C29000:XCME",
                1.0,
                700.0,
                "SELL",
                False,
                False,
            ),
        ],
    }
    for day, rows in trades.items():
        path = writer.write_tasty_trades("NQ", day, rows)
        assert path.exists()
    snapshot = settings.snapshots_dir / "NQ" / EXPIRY / f"{PREV.isoformat()}.parquet"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array(
                    [at_20] * len(STRIKES) + [at_21] * len(STRIKES),
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "strike": pa.array(STRIKES * 2, type=pa.float64()),
                "right": pa.array(["C"] * 2 * len(STRIKES), type=pa.string()),
                # Objem kmitá: v řezu nižší než ve 20:00 (#1602)
                "volume": pa.array(
                    [150.0] * len(STRIKES) + [120.0] * len(STRIKES), type=pa.float64()
                ),
            }
        ),
        snapshot,
    )
    bars = settings.derived_dir / "NQ" / "bars" / f"{PREV.isoformat()}.parquet"
    bars.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array([at_20, at_21], type=pa.timestamp("us", tz="UTC")),
                "close": pa.array([29020.0, 29999.0], type=pa.float64()),
            }
        ),
        bars,
    )
    return url, settings


def test_bod_d_bere_jen_zmerene_strany_tisky_seance_a_rez(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    engine = create_engine(url)
    points = module.collect_symbol("NQ", settings, engine, PREV, PREV)
    assert [(p.day, p.expiry, p.role) for p in points] == [(PREV, EXPIRY, "D+1")]
    point = points[0]
    # Strana změřená v D+1 jen před publikací vypadla (#1605)
    assert [s.strike for s in point.sides] == STRIKES[:-1]
    assert point.spot == 29020.0  # bar 21:00 už patří za řez
    first, second = point.sides[0], point.sides[1]
    # Klíč = posun od řezu dne 16:00 CT (v létě 21:00 UTC): −1 h, řez, +1 h
    assert first.net == {-1: 50.0, 0: 100.0, 1: 100.0}
    # Tisky jen ze seance D do řezu a jen expirace řetězu
    assert (first.buy, first.sell) == (30.0, 5.0)
    assert first.aggressor_net == 25.0
    assert (second.buy, second.sell, second.aggressor_net) == (0.0, 40.0, -40.0)
    assert (first.volume_last, first.volume_max) == (120.0, 150.0)
    assert all(side.doi == 20.0 for side in point.sides)
    stats = module.group_stats(module.midpoint_pairs(point.sides))
    assert stats.samples == 5 and stats.buy_sides == 3 and stats.sell_sides == 2
    assert stats.up_sell == 1.0  # OI roste i na sell stranách
    assert (point.stored_median, point.stored_samples) == (0.0, 48)
    report = module.report_markdown({"NQ": points}, dt.datetime(2026, 10, 23, 20, 0, tzinfo=dt.UTC))
    assert "### H7" in report and "### H2" in report


def _seed_pair(
    tmp_path: Path,
    day: dt.date,
    archive: dict[dt.date, float],
    netflow: dict[str, list[tuple[dt.datetime, float]]],
    stored_expiry: str | None = None,
) -> tuple[str, Settings]:
    """Archiv OI všech `netflow` řetězů po dnech (strana 29000 C, třída per expirace) a netflow."""
    settings = Settings(data_dir=tmp_path / "data")
    url = f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"
    oi_repo = OIEodRepository(create_engine(url))
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(create_engine(url))
    alpha_repo.ensure_schema()
    if stored_expiry is not None:
        alpha_repo.record(
            "NQ",
            day,
            stored_expiry,
            AlphaCalibrationPoint(7, 0.5, None, None),
            0.3,
            5,
            source=SOURCE_MIDPOINT,
        )
    for expiry in netflow:
        for archive_day, oi in archive.items():
            oi_repo.upsert_many(
                [
                    OIRecord(
                        "NQ", expiry, 29000.0, "C", archive_day, oi, trading_class=f"Q{expiry[-1]}A"
                    )
                ],
                settings.oi_publication_utc(archive_day) + dt.timedelta(hours=1),
            )
    writer = SnapshotWriter(settings)
    for expiry, rows in netflow.items():
        for ts, net in rows:
            writer.write_netflow(
                "NQ",
                expiry,
                day,
                [
                    NetFlowRow(
                        ts_min=ts, strike=29000.0, right="C", net_volume=net, source=SOURCE_MIDPOINT
                    )
                ],
            )
    return url, settings


def test_patek_paruje_s_pondelim_pres_vikendove_klice_archivu(tmp_path: Path) -> None:
    module = _load()
    friday, monday = dt.date(2026, 10, 2), dt.date(2026, 10, 5)
    cutoff = dt.datetime(2026, 10, 2, 21, 0, tzinfo=dt.UTC)  # 16:00 CDT
    # Sobotní a nedělní klíč archivu nejsou obchodní den: ΔOI se bere pá → po (130 − 100)
    url, settings = _seed_pair(
        tmp_path,
        friday,
        {
            friday: 100.0,
            dt.date(2026, 10, 3): 999.0,
            dt.date(2026, 10, 4): 999.0,
            monday: 130.0,
        },
        # D+1 = pondělní expirace; uložený bod pod v5 bral D+2 (#1580)
        {"20261005": [(cutoff, 40.0)], "20261006": [(cutoff, -40.0)]},
        stored_expiry="20261006",
    )
    points = module.collect_symbol("NQ", settings, create_engine(url), friday, friday)
    assert [(p.day, p.next_day, p.expiry, p.role) for p in points] == [
        (friday, monday, "20261005", "D+1"),
        (friday, monday, "20261006", "uložený"),
    ]
    assert [[side.doi for side in p.sides] for p in points] == [[30.0], [30.0]]
    assert (points[0].stored_median, points[1].stored_median) == (None, 0.5)
    assert points[1].stored_samples == 7
    assert points[0].sides[0].net[0] == 40.0 and points[1].sides[0].net[0] == -40.0


def test_rez_v_zime_je_22_utc(tmp_path: Path) -> None:
    module = _load()
    day, next_day = dt.date(2026, 11, 2), dt.date(2026, 11, 3)  # po konci US letního času
    url, settings = _seed_pair(
        tmp_path,
        day,
        {day: 100.0, next_day: 120.0},
        {
            "20261103": [
                (dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC), 10.0),  # 15:00 CST
                (dt.datetime(2026, 11, 2, 22, 0, tzinfo=dt.UTC), 60.0),  # řez 16:00 CST
            ]
        },
    )
    points = module.collect_symbol("NQ", settings, create_engine(url), day, day)
    assert [(p.day, p.expiry) for p in points] == [(day, "20261103")]
    assert points[0].sides[0].net == {-1: 10.0, 0: 60.0, 1: 60.0}


def test_report_nesmi_do_data(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    with pytest.raises(SystemExit):
        module.main(
            [
                "--db",
                url,
                "--data",
                str(settings.data_dir),
                "--out",
                str(settings.data_dir / "reports"),
            ]
        )
    assert (
        module.main(
            [
                "--db",
                url,
                "--data",
                str(settings.data_dir),
                "--symbols",
                "NQ",
                "--from",
                PREV.isoformat(),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert (
        next((tmp_path / "out").glob("fa-ratio-1612-*.md"))
        .read_text("utf-8")
        .startswith("# Poměry")
    )
    sides = next((tmp_path / "out").glob("fa-ratio-1612-*-strany.csv")).read_text("utf-8")
    lines = sides.splitlines()
    assert lines[0].startswith("symbol,day,next_day,expiry,role")
    assert len(lines) == 1 + 5  # pět stran změřených v obou dnech
    assert lines[1].startswith(f"NQ,{PREV.isoformat()},{TODAY.isoformat()},{EXPIRY},D+1")
