"""Migrace feed_comparison (#757): shodit zděděný sloupec `id` + PK."""

import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from gexlens_engine.storage.feed_comparison import ComparisonRow, FeedComparisonRepository

TS = dt.datetime(2026, 8, 19, 14, 0, tzinfo=dt.UTC)


def row() -> ComparisonRow:
    return ComparisonRow(
        ts=TS,
        symbol="ES 20260819 6500C",
        field="bid",
        value_ibkr=18.0,
        value_tasty=18.1,
        age_ibkr_ms=100,
        age_tasty_ms=200,
    )


def test_cerstve_schema_nema_id_a_insert_funguje(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'x.sqlite'}")
    repo = FeedComparisonRepository(engine)
    repo.ensure_schema()

    columns = {c["name"] for c in inspect(engine).get_columns("feed_comparison")}
    assert "id" not in columns

    repo.insert_many([row()])
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT symbol, value_tasty FROM feed_comparison")).one()
    assert stored == ("ES 20260819 6500C", 18.1)


@pytest.mark.skipif(
    not os.environ.get("GEXLENS_TEST_PG_DSN"),
    reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)",
)
def test_migrace_shodi_zdedeny_sloupec_id_na_postgresu() -> None:
    """Přesně stav prod tabulky: staré schéma s `id` PK a existujícími řádky.

    Migrace musí sloupec (i PK index) shodit, data nechat a být idempotentní.
    SQLite `DROP COLUMN` na PK neumí — proto integrace s reálným PG (CI službou),
    stejný vzor jako test_notify/test_oi_archive.
    """
    engine = create_engine(os.environ["GEXLENS_TEST_PG_DSN"])
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS feed_comparison"))
        conn.execute(
            text(
                "CREATE TABLE feed_comparison ("
                " id SERIAL PRIMARY KEY,"
                " ts TIMESTAMPTZ NOT NULL, symbol VARCHAR(48) NOT NULL,"
                " field VARCHAR(16) NOT NULL, value_ibkr FLOAT, value_tasty FLOAT,"
                " delta FLOAT, age_ibkr_ms BIGINT, age_tasty_ms BIGINT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO feed_comparison"
                " (ts, symbol, field, value_ibkr, value_tasty, delta, age_ibkr_ms, age_tasty_ms)"
                " VALUES (:ts, 'ES stary', 'bid', 1.0, 1.1, 0.1, 5, 6)"
            ),
            {"ts": TS},
        )

    repo = FeedComparisonRepository(engine)
    try:
        repo.ensure_schema()
        repo.ensure_schema()  # idempotence — druhý běh nesmí spadnout

        columns = {c["name"] for c in inspect(engine).get_columns("feed_comparison")}
        assert "id" not in columns
        repo.insert_many([row()])  # zápis po migraci jede dál
        with engine.connect() as conn:
            symbols = conn.execute(text("SELECT symbol FROM feed_comparison ORDER BY symbol")).all()
        assert [s for (s,) in symbols] == ["ES 20260819 6500C", "ES stary"]
    finally:
        with engine.begin() as conn:  # úklid po sobě — DB sdílí ostatní PG testy
            conn.execute(text("DROP TABLE IF EXISTS feed_comparison"))
