"""Testy klíče trading_class v oi_eod (#736) — série se neslévají, čtení Σ."""

import datetime as dt

from sqlalchemy import create_engine

from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord

DAY = dt.date(2026, 8, 24)


def repo(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}")
    repository = OIEodRepository(engine)
    repository.ensure_schema()
    return repository


def test_dve_serie_teze_expirace_maji_vlastni_radky(tmp_path) -> None:
    """AC #736: CardinalityViolation z #215 se nevrací a série se rozlišují."""
    repository = repo(tmp_path)
    repository.upsert_many(
        [
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 100.0, trading_class="E1A"),
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 40.0, trading_class="EX1"),
        ]
    )

    total = repository.get_oi("MES", DAY, 6400.0, "C", expiry="20260824")
    assert total == 140.0  # Σ přes série = chování před #736

    per_class = repository.values_for("MES", "20260824", DAY, trading_class="E1A")
    assert len(per_class) == 1 and per_class[0].oi == 100.0  # datová strana #513


def test_chain_for_day_agreguje_jako_stary_zapis(tmp_path) -> None:
    """Forward GEX: Σ OI, snímek od série s větším OI (zrcadlo write-merge #215)."""
    repository = repo(tmp_path)
    repository.upsert_many(
        [
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 100.0, iv=0.2, trading_class="E1A"),
            OIRecord("MES", "20260824", 6400.0, "C", DAY, 40.0, iv=0.9, trading_class="EX1"),
        ]
    )

    chain = repository.chain_for_day("MES", DAY)

    assert len(chain) == 1
    assert chain[0].oi == 140.0
    assert chain[0].iv == 0.2  # dominantní série (větší OI)


def test_snapshot_a_values_scitaji(tmp_path) -> None:
    repository = repo(tmp_path)
    repository.upsert_many(
        [
            OIRecord("MES", "20260824", 6400.0, "P", DAY, 10.0, trading_class="E1A"),
            OIRecord("MES", "20260824", 6400.0, "P", DAY, 5.0, trading_class="EX1"),
        ]
    )

    assert repository.snapshot("MES", DAY) == {("20260824", 6400.0, "P"): 15.0}
    values = repository.values_for("MES", "20260824", DAY)
    assert len(values) == 1 and values[0].oi == 15.0
