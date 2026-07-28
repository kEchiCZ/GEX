"""Integrační test dopočtu reakcí (#276): DB + parquet archiv + kontaminace."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events, news_reactions
from gexlens_news.bars import BarsRepository
from gexlens_news.reaction_job import ReactionJob

DAY = dt.date(2026, 7, 28)
EVENT_TS = dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC)
NOW = EVENT_TS + dt.timedelta(hours=3)


def write_bars(data_dir: Path, symbol: str, day: dt.date, *, drift_bp: float = 0.0) -> None:
    """Den plochých barů s lineárním driftem — snadno kontrolovatelný výsledek."""
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
    rows = []
    base = 7000.0
    for minute in range(24 * 60):
        ts = start + dt.timedelta(minutes=minute)
        price = base * (1 + drift_bp / 10_000 * (1 if ts >= EVENT_TS else 0))
        rows.append(
            {
                "ts_min": ts,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 100.0,
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), directory / f"{day.isoformat()}.parquet")


def add_event(engine: Engine, ts: dt.datetime, *, importance: int | None, title: str) -> int:
    with engine.begin() as conn:
        key = conn.execute(
            insert(news_events).values(
                ts_event=ts,
                ts_ingested=ts,
                source="finnhub",
                kind="headline",
                title=title,
                importance=importance,
                symbols=[],
                market_closed=False,
                dedup_hash=title,
                raw={},
            )
        ).inserted_primary_key
    assert key is not None
    return int(key[0])


def make_env(tmp_path: Path) -> tuple[Engine, ReactionJob]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    write_bars(tmp_path / "data", "ES", DAY, drift_bp=20.0)
    write_bars(tmp_path / "data", "NQ", DAY)
    return engine, ReactionJob(engine, BarsRepository(tmp_path / "data"))


def test_job_measures_all_windows_for_both_symbols(tmp_path: Path) -> None:
    engine, job = make_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=1, title="Solo headline")

    assert job.run(NOW) == 8  # 4 okna × 2 symboly

    with engine.connect() as conn:
        rows = conn.execute(
            select(news_reactions).where(news_reactions.c.event_id == event_id)
        ).fetchall()
    by_key = {(r.symbol, r.window_min): r for r in rows}
    assert sorted({k[1] for k in by_key}) == [1, 5, 15, 60]
    # ES má drift +20 bps po zprávě, NQ je plochý
    assert by_key[("ES", 5)].ret_bp > 19
    assert abs(by_key[("NQ", 5)].ret_bp) < 0.01
    # Archiv má jen jeden den → baseline nestačí, vol_z zůstává None
    assert all(r.vol_z is None for r in rows)
    assert all(not r.deferred for r in rows)


def test_job_flags_contaminated_windows_only(tmp_path: Path) -> None:
    """Druhý high-impact event kazí jen okna, do kterých spadne (SPEC 5.1)."""
    engine, job = make_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=1, title="První zpráva")
    add_event(engine, EVENT_TS + dt.timedelta(minutes=12), importance=3, title="FOMC")

    job.run(NOW)

    with engine.connect() as conn:
        rows = conn.execute(
            select(news_reactions.c.window_min, news_reactions.c.contaminated).where(
                news_reactions.c.event_id == event_id, news_reactions.c.symbol == "ES"
            )
        ).fetchall()
    contaminated = {r.window_min: r.contaminated for r in rows}
    assert contaminated == {1: False, 5: False, 15: True, 60: True}


def test_job_skips_events_with_open_windows_and_is_idempotent(tmp_path: Path) -> None:
    engine, job = make_env(tmp_path)
    add_event(engine, EVENT_TS, importance=1, title="Hotová")
    # Zpráva stará 10 min — nejdelší okno (60) ještě neuplynulo
    add_event(engine, NOW - dt.timedelta(minutes=10), importance=1, title="Čerstvá")

    first = job.run(NOW)
    assert first == 8  # jen ta s uzavřenými okny

    # Opakovaný běh už nic nepřidá — eventy s reakcemi se přeskakují
    assert job.run(NOW) == 0


def test_low_importance_event_does_not_contaminate(tmp_path: Path) -> None:
    engine, job = make_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=1, title="Hlavní")
    add_event(engine, EVENT_TS + dt.timedelta(minutes=2), importance=1, title="Nedůležitá")
    add_event(engine, EVENT_TS + dt.timedelta(minutes=3), importance=None, title="Neklasifikovaná")

    job.run(NOW)

    with engine.connect() as conn:
        rows = conn.execute(
            select(news_reactions.c.contaminated).where(news_reactions.c.event_id == event_id)
        ).fetchall()
    assert all(not r.contaminated for r in rows)
