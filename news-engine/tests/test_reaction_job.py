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


def write_holiday_bars(data_dir: Path, symbol: str, day: dt.date) -> None:
    """Zavřený den: bary jen do 12:00 UTC, pak celý den nic.

    Zrcadlí svátek — rozvrh Globexu by tvrdil, že se obchoduje, ale žádný bar
    neexistuje.
    """
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
    rows = [
        {
            "ts_min": start + dt.timedelta(minutes=minute),
            "open": 7000.0,
            "high": 7001.0,
            "low": 6999.0,
            "close": 7000.0,
            "volume": 100.0,
        }
        for minute in range(12 * 60)
    ]
    pq.write_table(pa.Table.from_pylist(rows), directory / f"{day.isoformat()}.parquet")


def test_market_closed_se_opravi_podle_baru(tmp_path: Path) -> None:
    """Svátek: rozvrh říká „otevřeno", bary říkají pravdu (#339)."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    # Zpráva ve 14:30 UTC ve všední den — rozvrh Globexu = otevřeno
    for symbol in ("ES", "NQ"):
        write_holiday_bars(tmp_path / "data", symbol, DAY)
        write_bars(tmp_path / "data", symbol, DAY + dt.timedelta(days=1))
    job = ReactionJob(engine, BarsRepository(tmp_path / "data"))
    event_id = add_event(engine, EVENT_TS, importance=1, title="Svátek headline")

    job.run(NOW)

    with engine.connect() as conn:
        row = conn.execute(select(news_events).where(news_events.c.id == event_id)).fetchone()
    assert row is not None
    assert row.market_closed is True


def test_dira_v_datech_jednoho_symbolu_neni_zavreny_trh(tmp_path: Path) -> None:
    """Chybějící bary jednoho symbolu nesmí předstírat zavřený trh (#339)."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    write_bars(tmp_path / "data", "ES", DAY)  # ES obchoduje
    write_holiday_bars(tmp_path / "data", "NQ", DAY)  # NQ má díru
    write_bars(tmp_path / "data", "NQ", DAY + dt.timedelta(days=1))
    job = ReactionJob(engine, BarsRepository(tmp_path / "data"))
    event_id = add_event(engine, EVENT_TS, importance=1, title="Díra v NQ")

    job.run(NOW)

    with engine.connect() as conn:
        row = conn.execute(select(news_events).where(news_events.c.id == event_id)).fetchone()
    assert row is not None
    assert row.market_closed is False


def test_vikendova_zprava_dostane_deferred_reakci(tmp_path: Path) -> None:
    """Sobotní geopolitika je příklad ze SPEC 5.1 — dřív nedostala reakci žádnou.

    Job načítal bary jen 30 min zpět, takže přes zavřený víkend nenašel základní
    cenu a `compute_reactions` vrátil prázdno. Deferred tím nikdy nevystřelilo.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    patek = dt.date(2026, 7, 24)
    pondeli = dt.date(2026, 7, 27)
    for symbol in ("ES", "NQ"):
        write_bars(tmp_path / "data", symbol, patek)
        write_bars(tmp_path / "data", symbol, pondeli)  # sobota a neděle chybí

    sobota = dt.datetime(2026, 7, 25, 14, 0, tzinfo=dt.UTC)
    event_id = add_event(engine, sobota, importance=3, title="Víkendová geopolitika")

    job = ReactionJob(engine, BarsRepository(tmp_path / "data"))
    assert job.run(dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC)) == 8

    with engine.connect() as conn:
        rows = conn.execute(
            select(news_reactions).where(news_reactions.c.event_id == event_id)
        ).fetchall()
        event = conn.execute(select(news_events).where(news_events.c.id == event_id)).fetchone()
    assert all(row.deferred for row in rows)
    assert event is not None
    assert event.market_closed is True
