"""Doplnění klíče uzavírky deferred reakcí z archivu barů (#1311).

Skript `scripts/backfill_reaction_closure.py` se načítá přes importlib.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, insert, select

from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_events,
    news_reactions,
    reaction_row_values,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_reaction_closure.py"
SATURDAY = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)
SUNDAY_OPEN = dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_reaction_closure", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bars(data: Path, day: dt.date, minutes: list[dt.datetime]) -> None:
    path = data / "derived" / "ES" / "bars" / f"{day.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    n = len(minutes)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array(minutes, type=pa.timestamp("us", tz="UTC")),
                "open": [7000.0] * n,
                "high": [7001.0] * n,
                "low": [6999.0] * n,
                "close": [7000.0] * n,
                "volume": [10.0] * n,
            }
        ),
        path,
    )


def test_backfill_doplni_jen_deferred_bez_klice(tmp_path: Path) -> None:
    module = _load()
    url = f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}"
    engine = create_engine(url)
    ensure_sentiment_schema(engine)
    data = tmp_path / "data"
    _bars(data, dt.date(2026, 9, 25), [dt.datetime(2026, 9, 25, 20, 59, tzinfo=dt.UTC)])
    _bars(data, SUNDAY_OPEN.date(), [SUNDAY_OPEN, SUNDAY_OPEN + dt.timedelta(minutes=1)])
    events = [(SATURDAY, True), (SATURDAY + dt.timedelta(hours=20), True), (SUNDAY_OPEN, False)]
    with engine.begin() as conn:
        for index, (ts, deferred) in enumerate(events):
            key = conn.execute(
                insert(news_events).values(
                    ts_event=ts,
                    ts_ingested=ts,
                    source="test",
                    kind="headline",
                    title=f"zpráva {index}",
                    symbols=[],
                    market_closed=deferred,
                    dedup_hash=f"bf-{index}",
                    raw={},
                )
            ).inserted_primary_key
            assert key is not None
            window = ReactionWindow(
                window_min=5,
                ret_bp=1.0,
                range_bp=1.0,
                vol_z=None,
                contaminated=False,
                deferred=deferred,
                gex_regime=None,
                computed_at=ts,
            )
            conn.execute(
                insert(news_reactions).values(
                    event_id=int(key[0]), symbol="ES", **reaction_row_values([window])
                )
            )

    assert module.main(["--db", url, "--data", str(data)]) == 0  # dry-run
    with engine.connect() as conn:
        assert all(
            row is None for row in conn.execute(select(news_reactions.c.closure_open_ts)).scalars()
        )

    assert module.main(["--db", url, "--data", str(data), "--apply"]) == 0
    with engine.connect() as conn:
        stored = [
            row.closure_open_ts
            for row in conn.execute(select(news_reactions).order_by(news_reactions.c.event_id))
        ]
    opened = SUNDAY_OPEN.replace(tzinfo=None)  # sqlite vrací naivní UTC
    assert stored == [opened, opened, None]
    # Idempotence: druhý běh nemá co doplnit
    planned, missing = module.plan_closure_opens(engine, module.BarsRepository(data))
    assert planned == [] and missing == 0
