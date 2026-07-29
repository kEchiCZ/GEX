"""Test persistence stavu retro passu (#297, SPEC 9.6)."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, select

from gexlens_engine.storage.meta import meta_metadata, settings_table
from gexlens_news.retro_pass import RETRO_PASS_SETTINGS_KEY, RetroResult, store_retro_result


def test_store_retro_result_upserts(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}")
    meta_metadata.create_all(engine)
    first = RetroResult(
        ran_at=dt.datetime(2026, 7, 29, 5, 0, tzinfo=dt.UTC),
        classified=12,
        reactions=96,
        index_points=480,
    )
    store_retro_result(engine, first)
    second = RetroResult(
        ran_at=dt.datetime(2026, 7, 30, 5, 0, tzinfo=dt.UTC),
        classified=3,
        reactions=8,
        index_points=120,
    )
    store_retro_result(engine, second)  # update, ne druhý řádek

    with engine.connect() as conn:
        rows = conn.execute(
            select(settings_table).where(settings_table.c.key == RETRO_PASS_SETTINGS_KEY)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0].value["ran_at"] == "2026-07-30T05:00:00+00:00"
    assert rows[0].value["reactions"] == 8
