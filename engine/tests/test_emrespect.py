"""Testy respektování pásma EM (#872): klasifikace, zdroje EM, kolektor."""

import datetime as dt
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from gexlens_engine.compute.emrespect import (
    SOURCE_CLOSE_PREM,
    SOURCE_STRADDLE,
    EmReference,
    StraddleQuote,
    classify,
    negative_share,
    straddle_em,
)
from gexlens_engine.compute.settle import ET_TZ, session_time_utc
from gexlens_engine.emrespect import EmRespectCollector, compute_session
from gexlens_engine.storage.emrespect_store import EmRespectRepository, em_respect_metadata
from gexlens_engine.storage.oi_archive import metadata as oi_metadata
from gexlens_engine.storage.oi_archive import oi_eod_table

SESSION = dt.date(2026, 8, 25)
US_OPEN = session_time_utc(SESSION, 9, 30, ET_TZ)


def reference(anchor: float = 7600.0, em: float = 40.0) -> EmReference:
    return EmReference(
        ts=US_OPEN, source=SOURCE_STRADDLE, anchor=anchor, atm_strike=anchor, em_points=em
    )


# ── Golden klasifikace seance ────────────────────────────────────────


def test_classify_close_uvnitr_bez_dotyku() -> None:
    record = classify(
        session_date=SESSION,
        symbol="ES",
        reference=reference(),
        high=7630.0,
        low=7580.0,
        close=7610.0,
        negative_gamma_share=0.25,
    )
    assert record is not None
    assert record.close_in_band and not record.touch_upper and not record.touch_lower
    assert record.range_vs_em == pytest.approx(50.0 / 40.0)
    assert record.em_pct == pytest.approx(100.0 * 40.0 / 7600.0)


def test_classify_touch_bez_close_beyond_a_pruraz() -> None:
    # Dotyk horní hranice, close zpět uvnitř — touch ano, průraz close ne
    touched = classify(
        session_date=SESSION,
        symbol="ES",
        reference=reference(),
        high=7645.0,
        low=7590.0,
        close=7620.0,
        negative_gamma_share=None,
    )
    assert touched is not None
    assert touched.touch_upper and touched.close_in_band
    # Close pod dolní hranicí = průraz
    broken = classify(
        session_date=SESSION,
        symbol="ES",
        reference=reference(),
        high=7610.0,
        low=7540.0,
        close=7550.0,
        negative_gamma_share=0.9,
    )
    assert broken is not None
    assert broken.touch_lower and not broken.close_in_band


def test_classify_hrana_patri_dovnitr_a_nesmysl_vraci_none() -> None:
    # Close přesně na hranici není průraz (konvence flip zóny #209)
    edge = classify(
        session_date=SESSION,
        symbol="ES",
        reference=reference(),
        high=7640.0,
        low=7560.0,
        close=7640.0,
        negative_gamma_share=None,
    )
    assert edge is not None
    assert edge.close_in_band and not edge.touch_upper
    assert (
        classify(
            session_date=SESSION,
            symbol="ES",
            reference=reference(em=0.0),
            high=7610.0,
            low=7590.0,
            close=7600.0,
            negative_gamma_share=None,
        )
        is None
    )


def test_straddle_em_zrcadli_frontend() -> None:
    """Nejbližší strike se zaplacenýma oběma stranama, max 3 kandidáti (#676)."""
    quotes = [
        StraddleQuote(strike=7600.0, call_mid=0.0, put_mid=12.0),  # chybí call
        StraddleQuote(strike=7605.0, call_mid=18.0, put_mid=21.0),
        StraddleQuote(strike=7610.0, call_mid=15.0, put_mid=25.0),
    ]
    hit = straddle_em(quotes, 7601.0)
    assert hit == (7605.0, 39.0)
    assert straddle_em([StraddleQuote(7600.0, 0.0, 5.0)], 7600.0) is None


def test_negative_share_hrana_neni_negativni() -> None:
    ts = [US_OPEN + dt.timedelta(minutes=i) for i in range(4)]
    spots = {ts[0]: 7590.0, ts[1]: 7600.0, ts[2]: 7610.0, ts[3]: 7580.0}
    flips = {ts[0]: 7600.0, ts[1]: 7600.0, ts[2]: 7600.0}  # ts[3] bez flipu
    assert negative_share(spots, flips) == pytest.approx(1 / 3)
    assert negative_share(spots, {}) is None


# ── Kolektor nad tmp parquet + sqlite ────────────────────────────────


def write_parquet(path: Path, rows: dict[str, list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), path)


def seed_session_files(data_dir: Path, symbol: str = "ES") -> None:
    """Bary + levels + 0DTE snapshoty jedné seance (US open → settle)."""
    minutes = [US_OPEN + dt.timedelta(minutes=i) for i in range(3)]
    write_parquet(
        data_dir / "derived" / symbol / "bars" / f"{SESSION.isoformat()}.parquet",
        {
            "ts_min": minutes,
            "high": [7602.0, 7648.0, 7625.0],
            "low": [7595.0, 7600.0, 7570.0],
            "close": [7600.0, 7640.0, 7610.0],
        },
    )
    write_parquet(
        data_dir / "derived" / symbol / "levels" / f"{SESSION.isoformat()}.parquet",
        {"ts_min": minutes, "flip": [7605.0, 7605.0, 7605.0]},
    )
    write_parquet(
        data_dir
        / "snapshots"
        / symbol
        / SESSION.strftime("%Y%m%d")
        / f"{SESSION.isoformat()}.parquet",  # noqa: E501
        {
            "ts_min": [minutes[0], minutes[0]],
            "strike": [7600.0, 7600.0],
            "right": ["C", "P"],
            "bid": [19.0, 20.0],
            "ask": [21.0, 22.0],
        },
    )


def make_db(tmp_path: Path) -> Engine:
    # Soubor, ne :memory: — kolektor běží přes asyncio.to_thread a paměťová
    # sqlite je per vlákno (nové vlákno = prázdná databáze)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'em.sqlite'}")
    oi_metadata.create_all(engine)
    em_respect_metadata.create_all(engine)
    return engine


def test_compute_session_ze_snapshotu(tmp_path: Path) -> None:
    """EM ze straddlu (mid C 20 + mid P 21 = 41 b), klasifikace nad bary."""
    db = make_db(tmp_path)
    seed_session_files(tmp_path)
    record = compute_session(tmp_path, db, "ES", SESSION)
    assert record is not None
    assert record.reference.source == SOURCE_STRADDLE
    assert record.reference.em_points == pytest.approx(41.0)
    assert record.reference.anchor == pytest.approx(7600.0)
    # High 7648 > 7641 → touch, close 7610 uvnitř; pod flipem 1 z 3 minut (7600)
    assert record.touch_upper and record.close_in_band
    assert record.negative_gamma_share == pytest.approx(1 / 3)


def test_compute_session_fallback_close_prem(tmp_path: Path) -> None:
    """Bez snapshotů se EM bere z close prémií věčného oi_eod (#519)."""
    db = make_db(tmp_path)
    seed_session_files(tmp_path)
    snapshot = (
        tmp_path
        / "snapshots"
        / "ES"
        / SESSION.strftime("%Y%m%d")
        / f"{SESSION.isoformat()}.parquet"  # noqa: E501
    )
    snapshot.unlink()
    with db.begin() as conn:
        conn.execute(
            insert(oi_eod_table),
            [
                {
                    "symbol": "ES",
                    "expiry": SESSION.strftime("%Y%m%d"),
                    "trading_class": "",
                    "strike": 7600.0,
                    "right": side,
                    "date": SESSION,
                    "oi": 100.0,
                    "close_prem": prem,
                    "und_price": 7598.0,
                }
                for side, prem in (("C", 17.0), ("P", 19.0))
            ],
        )
    record = compute_session(tmp_path, db, "ES", SESSION)
    assert record is not None
    assert record.reference.source == SOURCE_CLOSE_PREM
    assert record.reference.ts is None
    assert record.reference.em_points == pytest.approx(36.0)
    assert record.reference.anchor == pytest.approx(7598.0)


def test_compute_session_bez_zdroju_vraci_none(tmp_path: Path) -> None:
    """Bez snapshotů i bez close prémií se seance poctivě vynechá (ADR-0028)."""
    db = make_db(tmp_path)
    seed_session_files(tmp_path)
    (
        tmp_path
        / "snapshots"
        / "ES"
        / SESSION.strftime("%Y%m%d")
        / f"{SESSION.isoformat()}.parquet"  # noqa: E501
    ).unlink()
    assert compute_session(tmp_path, db, "ES", SESSION) is None


async def test_collector_zapise_po_settle_a_backfill_je_idempotentni(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    seed_session_files(tmp_path)
    repository = EmRespectRepository(db)
    collector = EmRespectCollector(
        symbol="ES",
        repository=repository,
        db=db,
        data_dir=tmp_path,
        backfill_days=3,
    )
    # Před settle se nezapisuje (jen backfill starších seancí — tady žádné nejsou)
    await collector.on_minute(US_OPEN + dt.timedelta(hours=1))
    assert repository.existing_dates("ES") == set()
    # Po settle + grace se seance klasifikuje a zapíše
    after_settle = session_time_utc(SESSION, 16, 10, ET_TZ)
    await collector.on_minute(after_settle)
    assert repository.existing_dates("ES") == {SESSION}
    rows = repository.list_for("ES")
    assert rows[0]["em_source"] == SOURCE_STRADDLE
    summary = repository.summary("ES", window_days=365)
    assert summary is not None and summary["n"] == 1
    # Druhé volání nic nepřidá (jeden pokus per seance)
    await collector.on_minute(after_settle + dt.timedelta(minutes=5))
    assert len(repository.list_for("ES")) == 1
