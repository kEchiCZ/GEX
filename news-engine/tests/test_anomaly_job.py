"""Job upozornění na reakci trhu (#1291): SQLite zprávy + parquet bary, dedup, watermark.

Rozhodovací logiku pokrývá `test_clusters.py` nad seznamy; tady se ověřuje IO
adaptér — výběr zpráv (FF impact a kurátor z `raw`), čtení barů per symbol,
baseline z 20 seancí, cooldown mezi běhy, watermark a zavřený trh.
"""

import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.anomaly_job import AnomalyJob
from gexlens_news.bars import BarsRepository

T0 = dt.datetime(2026, 10, 7, 12, 30, tzinfo=dt.UTC)  # středa, release v 8:30 ET
FRIDAY_CLOSE = dt.datetime(2026, 10, 9, 21, 0, tzinfo=dt.UTC)  # 16:00 CT
SUNDAY_OPEN = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.UTC)  # 17:00 CT
_ids = itertools.count(1)


def write_archive(
    data_dir: Path,
    symbol: str,
    *,
    jump_bp: float = 0.0,
    weekend_gap_bp: float = 0.0,
) -> None:
    """Bary 1. 9. – 11. 10. 2026: klidné vlnění, volitelně skok ve 12:30 dne T0
    a gap na nedělním otevření 11. 10.

    Kvůli rychlosti jen části seancí: 11:00–14:00 UTC (hodina σ před oknem
    denní doby kolem 8:30 ET), pátek do závěru 21:00 a neděle od otevření
    22:00 — víkendová zavření jsou tak stejná jako v plném archivu.
    """
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    day = dt.date(2026, 9, 1)
    while day <= SUNDAY_OPEN.date():
        rows = []
        for minute in (
            *range(11 * 60, 14 * 60),
            *range(19 * 60 + 30, 21 * 60),
            *range(22 * 60, 22 * 60 + 30),
        ):
            ts = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC) + dt.timedelta(
                minutes=minute
            )
            if is_market_closed(ts):
                continue
            close = 7000.0 + 0.5 * (minute % 2)
            if ts >= T0:
                close *= 1 + jump_bp / 10_000
            if ts >= SUNDAY_OPEN:
                close *= 1 + weekend_gap_bp / 10_000
            rows.append(
                {
                    "ts_min": ts,
                    "open": close,
                    "high": close + 0.25,
                    "low": close - 0.25,
                    "close": close,
                    "volume": 100.0,
                }
            )
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), directory / f"{day.isoformat()}.parquet")
        day += dt.timedelta(days=1)


def add_event(
    engine: Engine,
    at: dt.datetime,
    *,
    title: str,
    kind: str = "headline",
    importance: int | None = 1,
    category: str | None = "OTHER",
    raw: dict[str, Any] | None = None,
    ingested: dt.datetime | None = None,
) -> int:
    event_id = next(_ids)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events).values(
                id=event_id,
                ts_event=at,
                ts_ingested=ingested or at,
                source="forexfactory" if kind == "scheduled" else "alpaca",
                kind=kind,
                title=title,
                importance=importance,
                category=category,
                symbols=[],
                market_closed=False,
                dedup_hash=f"hash-{event_id}",
                raw=raw or {},
            )
        )
    return event_id


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> BarsRepository:
    """ES skok −20 bp v T0 a gap +35 bp na nedělním otevření, NQ −35 bp a −50 bp.

    Skok i gap jsou v jiných dnech, takže si testy shluku a víkendu nepřekážejí;
    archiv se píše jednou pro celý modul (bary jen čteme).
    """
    data_dir = tmp_path_factory.mktemp("bars")
    write_archive(data_dir, "ES", jump_bp=-20.0, weekend_gap_bp=35.0)
    write_archive(data_dir, "NQ", jump_bp=-35.0, weekend_gap_bp=-50.0)
    return BarsRepository(data_dir)


@pytest.fixture(scope="module")
def quiet_es(tmp_path_factory: pytest.TempPathFactory) -> BarsRepository:
    """ES bez mimořádného pohybu, NQ se skokem −35 bp v T0."""
    data_dir = tmp_path_factory.mktemp("bars_quiet_es")
    write_archive(data_dir, "ES")
    write_archive(data_dir, "NQ", jump_bp=-35.0)
    return BarsRepository(data_dir)


def seed_cpi_cluster(engine: Engine) -> tuple[int, int]:
    """FF High (klasifikátor mu dal importance 1), headline 3 a osm šumových titulků."""
    ff = add_event(
        engine,
        T0,
        title="USD CPI m/m",
        kind="scheduled",
        importance=1,
        category="MACRO_INFLATION",
        raw={"impact": "High", "country": "USD"},
        ingested=T0 - dt.timedelta(days=3),  # kalendář je v DB předem
    )
    headline = add_event(
        engine,
        T0 + dt.timedelta(seconds=40),
        title="US CPI hotter than expected",
        importance=3,
        category="MACRO_INFLATION",
    )
    for i in range(8):
        add_event(engine, T0 + dt.timedelta(seconds=5 + 10 * i), title=f"Šum {i}")
    return ff, headline


def test_shluk_se_ohlasi_po_uzavreni_okna_jednou_per_instrument(
    tmp_path: Path, archive: BarsRepository
) -> None:
    engine, bars = make_db(tmp_path), archive
    ff, headline = seed_cpi_cluster(engine)
    job = AnomalyJob(engine, bars, started_at=T0 - dt.timedelta(hours=1))

    # Okno 12:30–12:35 + 2 min na zápis barů → hotovo 12:37
    assert job.run(T0 + dt.timedelta(minutes=6)) == []
    now = T0 + dt.timedelta(minutes=7, seconds=30)
    alerts = job.run(now)
    assert [(a["kind"], a["symbol"]) for a in alerts] == [
        ("news_anomaly", "ES"),
        ("news_anomaly", "NQ"),
    ]
    for alert in alerts:
        assert alert["ts"] == int(now.timestamp())
        assert alert["ts_event"] == T0.isoformat()
        assert alert["event_ids"] == [ff, headline]
        assert "USD CPI m/m" in alert["message"] and "ostatní zprávy: 8" in alert["message"]
    assert "Reakce ES na zprávy z 14:30: ↓ -2" in alerts[0]["message"]
    assert "Reakce NQ na zprávy z 14:30: ↓ -3" in alerts[1]["message"]
    # Další běh týž shluk znovu neohlásí
    assert job.run(T0 + dt.timedelta(minutes=12, seconds=30)) == []


def test_bez_mimoradneho_pohybu_jen_nq(tmp_path: Path, quiet_es: BarsRepository) -> None:
    engine, bars = make_db(tmp_path), quiet_es
    seed_cpi_cluster(engine)
    alerts = AnomalyJob(engine, bars, started_at=T0 - dt.timedelta(hours=1)).run(
        T0 + dt.timedelta(minutes=8)
    )
    assert [a["symbol"] for a in alerts] == ["NQ"]


def test_socialni_sit_jen_od_kuratora(tmp_path: Path, archive: BarsRepository) -> None:
    engine, bars = make_db(tmp_path), archive
    add_event(engine, T0, title="Obecný post", kind="social", importance=3)
    job = AnomalyJob(engine, bars, symbols=("ES",), started_at=T0 - dt.timedelta(hours=1))
    assert job.run(T0 + dt.timedelta(minutes=8)) == []
    add_event(
        engine,
        T0,
        title="Post kurátora",
        kind="social",
        importance=2,
        raw={"did": "did:plc:x", "curated": True},
    )
    alerts = job.run(T0 + dt.timedelta(minutes=9))
    assert len(alerts) == 1 and "Post kurátora" in alerts[0]["message"]


def test_shluk_hotovy_pred_startem_procesu_se_nehlasi(
    tmp_path: Path, archive: BarsRepository
) -> None:
    """Watermark: po restartu nevzniknou duplicity (riziko je ztráta, ne duplicita)."""
    engine, bars = make_db(tmp_path), archive
    seed_cpi_cluster(engine)
    job = AnomalyJob(engine, bars, started_at=T0 + dt.timedelta(minutes=8))
    assert job.run(T0 + dt.timedelta(minutes=10)) == []


def test_stara_udalost_nealertuje_pri_dopoctu_historie(
    tmp_path: Path, archive: BarsRepository
) -> None:
    """#744: backfill zapíše dva roky starou zprávu TEĎ — upozornění o pohybu
    z předloňska nesmí vzniknout. Chrání to LOOKBACK: shluky se staví jen ze
    zpráv za posledních 30 min podle `ts_event`, ne podle času zápisu."""
    engine, bars = make_db(tmp_path), archive
    now = T0 + dt.timedelta(minutes=40)
    add_event(engine, T0 - dt.timedelta(days=400), title="Stará zpráva", importance=3, ingested=now)
    add_event(engine, T0, title="Zpráva starší než LOOKBACK", importance=3)
    assert AnomalyJob(engine, bars, started_at=T0 - dt.timedelta(hours=1)).run(now) == []


def test_zavreny_trh_ani_gap_na_otevreni_reakci_nehlasi(
    tmp_path: Path, archive: BarsRepository
) -> None:
    """Q2 (rozhodnutí 25. 9.) nahrazuje test #744 „deferred reakce přes víkend se
    pořád hlásí": gap na otevření je součet celého víkendu a jedné zprávě ho
    přisoudit nejde (přesně bug #1291). Víkendová zpráva se neměří (chybí bar
    před ní) a gap +35/−50 bp se nezapočte ani zprávě těsně po otevření —
    výchylka se měří od close minuty před zprávou. Víkend pokryje předobchodní
    upozornění (`test_preopen_job.py`)."""
    engine, bars = make_db(tmp_path), archive
    saturday = dt.datetime(2026, 10, 10, 10, 0, tzinfo=dt.UTC)
    add_event(engine, saturday, title="Weekend strike on oil facility", importance=3)
    add_event(engine, SUNDAY_OPEN, title="Headline v minutě otevření", importance=3)
    job = AnomalyJob(engine, bars, started_at=FRIDAY_CLOSE)
    assert job.run(saturday + dt.timedelta(minutes=10)) == []
    for minutes in (5, 10, 15):
        assert job.run(SUNDAY_OPEN + dt.timedelta(minutes=minutes)) == []
