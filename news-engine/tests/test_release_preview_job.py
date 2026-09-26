"""Job upozornění před releasem (#1296): SQLite kalendář, release_moves a stav etap, parquet.

Text pokrývá `test_release_preview.py`; tady se ověřuje IO adaptér — etapy v čase,
dedup přes restart (stav zapsaný před payloady), výběr releasů podle dopadu
a rodiny a viditelná degradace bez barů jednoho instrumentu.
"""

import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    release_moves,
    release_previews,
)
from gexlens_news.bars import BarsRepository
from gexlens_news.release_preview import PREVIEW_KIND
from gexlens_news.release_preview_job import ReleasePreviewJob

UTC = dt.UTC
MINUTE = dt.timedelta(minutes=1)
CPI_AT = dt.datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
_ids = itertools.count(1)


def write_bars(data_dir: Path, symbol: str, base: float, until: dt.datetime) -> None:
    """25 dní s denním rozsahem 60 b. (vol_ref) a minutové bary do `until` v den releasu."""
    rows: list[dict[str, Any]] = [
        {
            "ts_min": dt.datetime.combine(until.date() - dt.timedelta(days=k), dt.time(15), UTC),
            "open": base,
            "high": base + 30,
            "low": base - 30,
            "close": base,
            "volume": 1.0,
        }
        for k in range(25, 0, -1)
    ]
    rows += [
        {
            "ts_min": until - k * MINUTE,
            "open": base,
            "high": base,
            "low": base,
            "close": base,
            "volume": 1.0,
        }
        for k in range(120, -1, -1)
    ]
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    by_day: dict[dt.date, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["ts_min"].date(), []).append(row)
    for day, items in by_day.items():
        pq.write_table(pa.Table.from_pylist(items), directory / f"{day.isoformat()}.parquet")


def write_levels(data_dir: Path, symbol: str, at: dt.datetime, walls: tuple[float, float]) -> None:
    directory = data_dir / "derived" / symbol / at.strftime("%Y%m%d") / "levels"
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_min": at,
        "flip": walls[0] - 20,
        "call_wall": walls[0],
        "put_wall": walls[1],
        "centroid": walls[0] - 10,
        "total_gex": 1.0,
    }
    pq.write_table(pa.Table.from_pylist([row]), directory / f"{at.date().isoformat()}.parquet")


def add_release(engine: Engine, at: dt.datetime, title: str, impact: str = "High") -> int:
    event_id = next(_ids)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events).values(
                id=event_id,
                ts_event=at,
                ts_ingested=at - dt.timedelta(days=2),
                source="forexfactory",
                kind="scheduled",
                title=title,
                forecast=0.3,
                symbols=[],
                market_closed=False,
                dedup_hash=f"hash-{event_id}",
                raw={"impact": impact, "forecast": "0.3%"},
            )
        )
    return event_id


def add_history(engine: Engine, family: str, count: int = 10) -> None:
    """Minulé releasy rodiny v `release_moves` (násobky 0,4–0,76 při vol_ref 100 bp)."""
    rows = []
    for symbol in ("ES", "NQ"):
        for k in range(count):
            event_id = add_release(engine, CPI_AT - dt.timedelta(days=30 * (k + 1)), "USD x")
            rows.append(
                {
                    "cluster_ts": CPI_AT - dt.timedelta(days=30 * (k + 1)),
                    "symbol": symbol,
                    "family": family,
                    "headline": "Core CPI m/m",
                    "headline_event_id": event_id,
                    "surprise_sign": 1,
                    "vol_ref_bp": 100.0,
                    "exc_15m_bp": 40.0 * (1 + k / 10),
                    "ret_15m_bp": -5.0,
                    "ret_60m_bp": 5.0,
                    "tod_med_15m_bp": None,
                    "measured_at": CPI_AT,
                }
            )
    with engine.begin() as conn:
        conn.execute(insert(release_moves), rows)


def seeded(tmp_path: Path, *, es_bars: bool = True) -> tuple[Engine, Path]:
    data_dir = tmp_path / "data"
    if es_bars:
        write_bars(data_dir, "ES", 7800.0, CPI_AT - MINUTE)
    write_bars(data_dir, "NQ", 30000.0, CPI_AT - MINUTE)
    write_levels(data_dir, "ES", CPI_AT - dt.timedelta(minutes=61), (7810.0, 7735.0))
    write_levels(data_dir, "NQ", CPI_AT - dt.timedelta(minutes=61), (30025.0, 29900.0))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_release(engine, CPI_AT, "USD Core CPI m/m")
    add_release(engine, CPI_AT, "USD CPI y/y", impact="Medium")
    add_history(engine, "CPI")
    return engine, data_dir


def stages(engine: Engine) -> set[tuple[str, str, bool]]:
    with engine.connect() as conn:
        return {
            (row.symbol, row.stage, row.sent)
            for row in conn.execute(select(release_previews)).fetchall()
        }


def test_t60_a_t15_jednou_i_pres_restart(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path)
    repo = BarsRepository(data_dir)
    job = ReleasePreviewJob(engine, repo)
    assert job.run(CPI_AT - dt.timedelta(minutes=61)) == []
    first = job.run(CPI_AT - dt.timedelta(minutes=60))
    assert [(p["kind"], p["symbol"]) for p in first] == [(PREVIEW_KIND, "ES"), (PREVIEW_KIND, "NQ")]
    es = str(first[0]["message"])
    assert es.startswith("CPI za 60 min (14:30) — ES 7800")
    assert "medián–p75 z 10 CPI" in es and "H1 – OVĚŘUJE SE" in es
    assert "call zeď 7810" in es
    assert len(first[0]["event_ids"]) == 2
    assert stages(engine) == {("ES", "T60", True), ("NQ", "T60", True)}
    # Restart: nová instance v T−59 nic nepošle, v T−15 jen T15
    restarted = ReleasePreviewJob(engine, repo)
    assert restarted.run(CPI_AT - dt.timedelta(minutes=59)) == []
    second = restarted.run(CPI_AT - dt.timedelta(minutes=15))
    assert len(second) == 2 and "za 15 min" in str(second[1]["message"])
    assert restarted.run(CPI_AT - dt.timedelta(minutes=14)) == []
    assert restarted.run(CPI_AT) == []
    with engine.connect() as conn:
        row = conn.execute(
            select(release_previews).where(
                release_previews.c.symbol == "ES", release_previews.c.stage == "T15"
            )
        ).one()
    assert row.n == 10 and row.family == "CPI"
    assert row.expected_p50_bp is not None and row.vol_now_bp is not None


def test_pozdni_start_posle_jen_t15(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path)
    payloads = ReleasePreviewJob(engine, BarsRepository(data_dir)).run(
        CPI_AT - dt.timedelta(minutes=10)
    )
    assert len(payloads) == 2 and "za 10 min" in str(payloads[0]["message"])
    assert stages(engine) == {
        ("ES", "T60", False),
        ("ES", "T15", True),
        ("NQ", "T60", False),
        ("NQ", "T15", True),
    }


def test_jen_high_a_rodiny_s_upozornenim(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    job = ReleasePreviewJob(engine, BarsRepository(data_dir))
    now = CPI_AT - dt.timedelta(minutes=60)
    assert job.run(now) == []  # prázdný kalendář
    add_release(engine, CPI_AT, "USD Core CPI m/m", impact="Medium")
    add_release(engine, CPI_AT + dt.timedelta(minutes=-30), "USD Building Permits")
    add_release(engine, CPI_AT, "EUR CPI m/m")
    assert job.run(now) == []


def test_bez_baru_es_odejde_s_chybejici_cenou(tmp_path: Path) -> None:
    engine, data_dir = seeded(tmp_path, es_bars=False)
    payloads = ReleasePreviewJob(engine, BarsRepository(data_dir)).run(
        CPI_AT - dt.timedelta(minutes=60)
    )
    by_symbol = {p["symbol"]: str(p["message"]) for p in payloads}
    assert "ES cena chybí" in by_symbol["ES"]
    assert "Úrovně ES: call zeď 7810" in by_symbol["ES"]
    assert "NQ 30000" in by_symbol["NQ"] and "× obvyklé (bez přepočtu" in by_symbol["NQ"]


def test_malo_historie_je_videt(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_bars(data_dir, "ES", 7800.0, CPI_AT - MINUTE)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_release(engine, CPI_AT, "USD Core PCE Price Index m/m")
    payloads = ReleasePreviewJob(engine, BarsRepository(data_dir), symbols=("ES",)).run(
        CPI_AT - dt.timedelta(minutes=60)
    )
    (message,) = [str(p["message"]) for p in payloads]
    assert message.startswith("PCE za 60 min")
    assert "málo historie (n = 0" in message and "Úrovně ES chybí" in message
