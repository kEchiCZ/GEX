"""Job předobchodního upozornění (#1291 Q2): SQLite zprávy a stav, parquet bary a úrovně.

Text a rozhodnutí pokrývá `test_preopen.py`; tady se ověřuje IO adaptér — etapy
v čase, poslední bar a úrovně příští expirace per symbol, stav v `settings`
a hlavně dedup přes restart news-enginu v neděli večer.
"""

import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.meta import meta_metadata, settings_table
from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.bars import BarsRepository
from gexlens_news.preopen_job import PREOPEN_SETTINGS_KEY, PreopenJob

UTC = dt.UTC
FRIDAY_LAST = dt.datetime(2026, 9, 18, 20, 59, tzinfo=UTC)  # 15:59 CT
OPENING = dt.datetime(2026, 9, 20, 22, 0, tzinfo=UTC)  # neděle 17:00 CT
MAIN_AT = OPENING - dt.timedelta(hours=4)  # 20:00 Praha
UPDATE_AT = OPENING - dt.timedelta(minutes=15)  # 23:45 Praha
_ids = itertools.count(1)


def write_data(data_dir: Path, symbol: str, close: float, walls: tuple[float, float]) -> None:
    """Páteční bary do závěru a levels pro páteční (zaniklou) i pondělní expiraci."""
    bars_dir = data_dir / "derived" / symbol / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts_min": FRIDAY_LAST - dt.timedelta(minutes=k),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 10.0,
        }
        for k in range(60, -1, -1)
    ]
    pq.write_table(pa.Table.from_pylist(rows), bars_dir / "2026-09-18.parquet")

    def levels(expiry: str, call_wall: float, put_wall: float) -> None:
        directory = data_dir / "derived" / symbol / expiry / "levels"
        directory.mkdir(parents=True, exist_ok=True)
        table = [
            {
                "ts_min": FRIDAY_LAST - dt.timedelta(minutes=2),
                "flip": close + 10,
                "call_wall": call_wall,
                "put_wall": put_wall,
                "centroid": close - 5,
                "total_gex": 1.0,
            },
            # Engine píše minuty i po zavření — prázdné úrovně
            {
                "ts_min": FRIDAY_LAST + dt.timedelta(hours=2),
                "flip": None,
                "call_wall": None,
                "put_wall": None,
                "centroid": None,
                "total_gex": 0.0,
            },
        ]
        pq.write_table(pa.Table.from_pylist(table), directory / "2026-09-18.parquet")

    levels("20260918", 1.0, 1.0)  # páteční 0DTE po settle zanikla — nesmí se použít
    levels("20260921", *walls)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    meta_metadata.create_all(engine)
    return engine


def add_event(
    engine: Engine,
    at: dt.datetime,
    *,
    title: str,
    importance: int | None = 1,
    category: str | None = "OTHER",
    direction: int | None = None,
    ingested: dt.datetime | None = None,
) -> int:
    event_id = next(_ids)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events).values(
                id=event_id,
                ts_event=at,
                ts_ingested=ingested or at,
                source="alpaca",
                kind="headline",
                title=title,
                importance=importance,
                category=category,
                sentiment_dir=direction,
                symbols=[],
                market_closed=True,
                dedup_hash=f"hash-{event_id}",
                raw={},
            )
        )
    return event_id


def stored_state(engine: Engine) -> Any:
    with engine.connect() as conn:
        row = conn.execute(
            select(settings_table.c.value).where(settings_table.c.key == PREOPEN_SETTINGS_KEY)
        ).first()
    return None if row is None else row.value


def job(engine: Engine, data_dir: Path) -> PreopenJob:
    return PreopenJob(engine, BarsRepository(data_dir))


def seeded(tmp_path: Path) -> tuple[Engine, Path, int]:
    data_dir = tmp_path / "data"
    write_data(data_dir, "ES", 7725.0, (7730.0, 7680.0))
    write_data(data_dir, "NQ", 29980.0, (30000.0, 29880.0))
    engine = make_db(tmp_path)
    strike = add_event(
        engine,
        dt.datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
        title="Weekend strike on oil facility",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    add_event(engine, dt.datetime(2026, 9, 19, 11, 0, tzinfo=UTC), title="Šum o víkendu")
    # Zpráva ve čtvrtek (před oknem zavřeného trhu) do souhrnu nepatří
    add_event(engine, FRIDAY_LAST - dt.timedelta(days=1), title="Čtvrteční zpráva", importance=3)
    return engine, data_dir, strike


def test_hlavni_souhrn_jednou_per_instrument_a_ne_znovu_po_restartu(tmp_path: Path) -> None:
    engine, data_dir, strike = seeded(tmp_path)
    first = job(engine, data_dir)
    assert first.run(MAIN_AT - dt.timedelta(minutes=1)) == []
    assert stored_state(engine) is None  # mimo etapy job do DB ani nesáhne

    now = MAIN_AT + dt.timedelta(minutes=2)
    payloads = first.run(now)
    assert [(p["kind"], p["symbol"]) for p in payloads] == [
        ("news_preopen", "ES"),
        ("news_preopen", "NQ"),
    ]
    es, nq = (str(p["message"]) for p in payloads)
    assert es.startswith("Před otevřením ES: Globex otevře v pondělí 00:00 (za 3 h 58 min)")
    # Úrovně pondělní expirace, ne zaniklé páteční; close z posledního baru
    assert "Úrovně ES z poslední seance (expirace 21. 9.): call zeď 7730 · put zeď 7680" in es
    assert "close 7725" in es
    assert "call zeď 30000 · put zeď 29880" in nq and "close 29980" in nq
    for message in (es, nq):
        assert "od pátku 22:55: 1 · sklon 🔴" in message
        assert "• 🔴 Weekend strike on oil facility" in message
        assert "ostatní zprávy: 1" in message and "Čtvrteční" not in message
    assert all(p["event_ids"] == [strike] for p in payloads)
    assert all(p["ts_event"] == OPENING.isoformat() for p in payloads)

    # Týž proces za 5 min i nový proces po restartu ve 21:00 — nic
    assert first.run(now + dt.timedelta(minutes=5)) == []
    assert job(engine, data_dir).run(MAIN_AT + dt.timedelta(hours=1)) == []
    # V čase aktualizace bez nové významné zprávy — nic, etapa se jen zapíše
    restarted = job(engine, data_dir)
    assert restarted.run(UPDATE_AT + dt.timedelta(minutes=1)) == []
    assert stored_state(engine)["done"] == ["main", "update"]
    assert restarted.run(UPDATE_AT + dt.timedelta(minutes=6)) == []


def test_aktualizace_jen_s_novou_zpravou_i_po_restartu(tmp_path: Path) -> None:
    engine, data_dir, strike = seeded(tmp_path)
    assert len(job(engine, data_dir).run(MAIN_AT + dt.timedelta(minutes=1))) == 2
    # Pozdě zapsaná zpráva s časem před hlavním souhrnem je pro aktualizaci nová
    late = add_event(
        engine,
        MAIN_AT - dt.timedelta(minutes=30),
        title="Iran closes Strait of Hormuz",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
        ingested=MAIN_AT + dt.timedelta(hours=2),
    )
    payloads = job(engine, data_dir).run(UPDATE_AT + dt.timedelta(minutes=2))  # po restartu
    assert [p["symbol"] for p in payloads] == ["ES", "NQ"]
    for payload in payloads:
        message = str(payload["message"])
        assert message.startswith("Aktualizace před otevřením")
        assert "Nové zásadní zprávy: 1 (celkem 2" in message
        assert "• 🔴 Iran closes Strait of Hormuz" in message
        assert "Weekend strike" not in message
        assert payload["event_ids"] == [late]
    assert stored_state(engine)["announced"]["ES"] == sorted([strike, late])


def test_zmeskany_hlavni_souhrn_dojde_jednou_v_case_aktualizace(tmp_path: Path) -> None:
    """News-engine ve 20:00 neběžel: první běh v 23:47 pošle jeden souhrn, ne dva."""
    engine, data_dir, _ = seeded(tmp_path)
    payloads = job(engine, data_dir).run(UPDATE_AT + dt.timedelta(minutes=2))
    assert [p["symbol"] for p in payloads] == ["ES", "NQ"]
    assert all(str(p["message"]).startswith("Před otevřením") for p in payloads)
    assert job(engine, data_dir).run(UPDATE_AT + dt.timedelta(minutes=7)) == []


def test_bez_zasadni_zpravy_nic_a_vsedni_den_ani_denni_pauza_nic(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_data(data_dir, "ES", 7725.0, (7730.0, 7680.0))
    engine = make_db(tmp_path)
    add_event(engine, dt.datetime(2026, 9, 19, 11, 0, tzinfo=UTC), title="Šum o víkendu")
    add_event(
        engine,
        dt.datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        title="Firma X výsledky",
        importance=3,
        category="EARNINGS",
    )
    # Významná podle varianty B, ale ne zásadní — souhrn nevznikne
    add_event(
        engine,
        dt.datetime(2026, 9, 19, 13, 0, tzinfo=UTC),
        title="Inflation is eating your savings",
        importance=2,
        category="MACRO_INFLATION",
    )
    preopen = PreopenJob(engine, BarsRepository(data_dir), symbols=("ES",))
    assert preopen.run(MAIN_AT + dt.timedelta(minutes=1)) == []
    assert stored_state(engine)["done"] == ["main"]
    assert stored_state(engine)["announced"] == {"ES": []}
    # Zásadní zpráva až po 20:00: aktualizace je první upozornění → vypadá jako souhrn
    add_event(
        engine,
        UPDATE_AT - dt.timedelta(minutes=30),
        title="Iran closes Strait of Hormuz",
        importance=3,
        category="GEOPOLITICS",
        direction=-1,
    )
    payloads = preopen.run(UPDATE_AT + dt.timedelta(minutes=1))
    assert len(payloads) == 1
    message = str(payloads[0]["message"])
    assert message.startswith("Před otevřením ES:")
    assert "Zásadní zprávy za zavřený trh od pátku 22:55: 1 · sklon 🔴" in message
    assert message.endswith("další významné: 1 · ostatní zprávy: 2")
    # Středa 4 h a 15 min před otevřením po denní pauze — žádná etapa
    wednesday_open = dt.datetime(2026, 9, 23, 22, 0, tzinfo=UTC)
    add_event(engine, wednesday_open - dt.timedelta(hours=1), title="Pauza", importance=3)
    for lead in (dt.timedelta(hours=4), dt.timedelta(minutes=15)):
        assert preopen.run(wednesday_open - lead + dt.timedelta(minutes=1)) == []


def test_bez_baru_zavreni_podle_rozvrhu_a_urovne_chybi(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    add_event(
        engine,
        dt.datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
        title="Weekend strike on oil facility",
        importance=3,
        category="GEOPOLITICS",
    )
    preopen = PreopenJob(engine, BarsRepository(tmp_path / "prazdno"), symbols=("NQ",))
    payloads = preopen.run(MAIN_AT + dt.timedelta(minutes=1))
    assert len(payloads) == 1
    message = str(payloads[0]["message"])
    assert "Úrovně NQ z poslední seance chybí" in message
    assert "od pátku 22:55" in message  # rozvrh: pá 16:00 CT − 5 min
