"""Integrační test dopočtu reakcí (#276): DB + parquet archiv + kontaminace."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_events,
    news_reactions,
    reaction_ret,
    unpivot_reaction,
)
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


def reaction_windows(engine: Engine, event_id: int) -> list[tuple[str, ReactionWindow]]:
    """Naměřená okna eventu jako (symbol, okno) — široký řádek rozložený (#998)."""
    with engine.connect() as conn:
        rows = (
            conn.execute(select(news_reactions).where(news_reactions.c.event_id == event_id))
            .mappings()
            .all()
        )
    return [(str(row["symbol"]), window) for row in rows for window in unpivot_reaction(row)]


def test_job_measures_all_windows_for_both_symbols(tmp_path: Path) -> None:
    engine, job = make_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=1, title="Solo headline")

    assert job.run(NOW) == 8  # 4 okna × 2 symboly

    rows = reaction_windows(engine, event_id)
    by_key = {(symbol, window.window_min): window for symbol, window in rows}
    assert sorted({k[1] for k in by_key}) == [1, 5, 15, 60]
    # ES má drift +20 bps po zprávě, NQ je plochý
    assert by_key[("ES", 5)].ret_bp > 19
    assert abs(by_key[("NQ", 5)].ret_bp) < 0.01
    # Archiv má jen jeden den → baseline nestačí, vol_z zůstává None
    assert all(window.vol_z is None for _, window in rows)
    assert all(not window.deferred for _, window in rows)
    # Široký tvar: jeden řádek per symbol, denní fáze zatím prázdná
    with engine.connect() as conn:
        wide = conn.execute(select(news_reactions)).mappings().all()
    assert sorted(row["symbol"] for row in wide) == ["ES", "NQ"]
    assert all(row["computed_at_daily"] is None for row in wide)


def test_job_flags_contaminated_windows_only(tmp_path: Path) -> None:
    """Druhý high-impact event kazí jen okna, do kterých spadne (SPEC 5.1)."""
    engine, job = make_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=1, title="První zpráva")
    add_event(engine, EVENT_TS + dt.timedelta(minutes=12), importance=3, title="FOMC")

    job.run(NOW)

    contaminated = {
        window.window_min: window.contaminated
        for symbol, window in reaction_windows(engine, event_id)
        if symbol == "ES"
    }
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

    assert all(not window.contaminated for _, window in reaction_windows(engine, event_id))


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
        event = conn.execute(select(news_events).where(news_events.c.id == event_id)).fetchone()
    assert all(window.deferred for _, window in reaction_windows(engine, event_id))
    assert event is not None
    assert event.market_closed is True


# ── Denní okna (#564) ──────────────────────────────────────────────


def make_daily_env(tmp_path: Path) -> tuple[Engine, ReactionJob]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    for offset in range(4):  # DAY..DAY+3 — dost na okna 1d a 2d
        write_bars(tmp_path / "data", "ES", DAY + dt.timedelta(days=offset), drift_bp=20.0)
        write_bars(tmp_path / "data", "NQ", DAY + dt.timedelta(days=offset))
    job = ReactionJob(engine, BarsRepository(tmp_path / "data"), daily_window_days=(1, 2))
    return engine, job


def daily_rows(engine: Engine, event_id: int) -> list[tuple[str, int, float]]:
    return sorted(
        (symbol, window.window_min, window.ret_bp)
        for symbol, window in reaction_windows(engine, event_id)
        if window.window_min >= 1440
    )


def test_job_dopocita_denni_okna_a_je_idempotentni(tmp_path: Path) -> None:
    engine, job = make_daily_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=2, title="CPI")
    now = EVENT_TS + dt.timedelta(days=20)  # nejdelší okno dávno uzavřené
    job.run(now)

    rows = daily_rows(engine, event_id)
    assert [(symbol, window) for symbol, window, _ in rows] == [
        ("ES", 1440),
        ("ES", 2880),
        ("NQ", 1440),
        ("NQ", 2880),
    ]
    # ES drift +20 bp od eventu → settle close 1d i 2d o 20 bp nad základnou
    es_1d = next(ret for symbol, window, ret in rows if symbol == "ES" and window == 1440)
    assert es_1d == pytest.approx(20.0, abs=0.1)
    # Idempotence: druhý běh nic nepřidá
    assert job.run(now + dt.timedelta(minutes=1)) == 0
    assert daily_rows(engine, event_id) == rows
    # Denní fáze doplnila TENTÝŽ řádek (#998): minutová okna zůstala, obě
    # fáze mají vlastní computed_at
    with engine.connect() as conn:
        es = (
            conn.execute(
                select(news_reactions).where(
                    news_reactions.c.event_id == event_id, news_reactions.c.symbol == "ES"
                )
            )
            .mappings()
            .one()
        )
    assert es["ret_5"] is not None
    assert es["computed_at_min"] is not None
    assert es["computed_at_daily"] is not None


def test_denni_faze_zalozi_radek_eventu_bez_minutovych_oken(tmp_path: Path) -> None:
    """Event před pokrytím minutových barů dostane jen denní okna (#998 upsert).

    Minutová fáze bez barů nic nezapíše; denní fáze pak musí řádek založit,
    ne jen aktualizovat — jinak by ~27 k historických dvojic vypadlo.
    """
    engine, job = make_daily_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=2, title="CPI")
    now = EVENT_TS + dt.timedelta(days=20)
    # Minutovou fázi obejdeme: řádek neexistuje, denní fáze ho zakládá
    job._windows = [1, 5, 15, 60]
    assert job._pending_daily_events(now, limit=10) == [(event_id, EVENT_TS)]
    assert job._run_daily(now, limit=10) == 4  # 2 okna × 2 symboly
    with engine.connect() as conn:
        rows = conn.execute(select(news_reactions)).mappings().all()
    assert sorted(row["symbol"] for row in rows) == ["ES", "NQ"]
    assert all(row["computed_at_min"] is None and row["ret_5"] is None for row in rows)
    # SQLite vrací naivní datetime — porovnání v UTC
    assert all(row["computed_at_daily"].replace(tzinfo=dt.UTC) == now for row in rows)
    assert all(row["ret_1440"] is not None for row in rows)
    assert job._pending_daily_events(now, limit=10) == []


def test_job_ceka_na_uzavreni_nejdelsiho_denniho_okna(tmp_path: Path) -> None:
    engine, job = make_daily_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=2, title="Fed speech")
    # Event mladší než DAILY_READY_CALENDAR_DAYS → denní fáze ho nebere,
    # minutová okna se ale dopočítají normálně
    job.run(EVENT_TS + dt.timedelta(days=2))
    assert daily_rows(engine, event_id) == []
    with engine.connect() as conn:
        minute_count = conn.execute(
            select(news_reactions.c.event_id).where(
                news_reactions.c.event_id == event_id,
                reaction_ret(5).is_not(None),
            )
        ).fetchall()
    assert len(minute_count) > 0


def test_event_pred_pokrytim_baru_dostane_tombstone(tmp_path: Path) -> None:
    """#655: event bez barů (před archivem) se označí a přestane vybírat."""
    engine, job = make_daily_env(tmp_path)
    # Event rok před prvním barem — základní cena neexistuje a existovat nebude
    prehistoricky = add_event(
        engine, EVENT_TS - dt.timedelta(days=365), importance=2, title="Starý CPI"
    )
    now = EVENT_TS + dt.timedelta(days=20)
    job.run(now)

    assert daily_rows(engine, prehistoricky) == []
    with engine.connect() as conn:
        event = conn.execute(
            select(news_events).where(news_events.c.id == prehistoricky)
        ).fetchone()
    assert event is not None
    assert event.daily_uncomputable is True
    # Pending dotaz ho už nevybírá — další běh ho nezpracovává znovu
    assert job._pending_daily_events(now, limit=200) == []


def test_cekajici_event_tombstone_nedostane(tmp_path: Path) -> None:
    """Dočasné wait_for_close (neuzavřené okno) se NESMÍ zaměnit s trvalým."""
    engine, job = make_daily_env(tmp_path)
    event_id = add_event(engine, EVENT_TS, importance=2, title="CPI")
    # Event už je přes práh 16 dní, ale bary končí DAY+3 → 2d okno nejde
    # uzavřít (chybí settle) — to je dočasný stav, ne tombstone
    now = EVENT_TS + dt.timedelta(days=20)
    job._daily_window_days = [1, 30]  # 30d okno se z 4 dnů barů neuzavře
    job.run(now)

    with engine.connect() as conn:
        event = conn.execute(select(news_events).where(news_events.c.id == event_id)).fetchone()
    assert event is not None
    assert event.daily_uncomputable is not True
    # a zůstává v pending — příští běh to zkusí znovu
    assert (event_id, EVENT_TS) in [
        (eid, ts) for eid, ts in job._pending_daily_events(now, limit=200)
    ]
