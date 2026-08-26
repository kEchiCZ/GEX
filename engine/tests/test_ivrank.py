"""Testy IV Ranku (#871): okno rank/percentil, tři řady, izolace zdrojů."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from gexlens_engine.compute.settle import ET_TZ, session_time_utc
from gexlens_engine.ivrank import (
    IvRankCollector,
    rank_of,
    window_context,
)
from gexlens_engine.storage.ivrank_store import (
    SOURCE_IBKR,
    SOURCE_OWN_ATM,
    SOURCE_TASTY,
    IvRankRepository,
    iv_rank_metadata,
)
from gexlens_engine.storage.oi_archive import metadata as oi_metadata
from gexlens_engine.storage.oi_archive import oi_eod_table

SESSION = dt.date(2026, 8, 26)


def num(value: object) -> float:
    assert isinstance(value, (int, float, str))
    return float(value)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'iv.sqlite'}")
    oi_metadata.create_all(engine)
    iv_rank_metadata.create_all(engine)
    return engine


def series(values: list[float], end: dt.date = SESSION) -> list[tuple[dt.date, float]]:
    start = end - dt.timedelta(days=len(values))
    return [(start + dt.timedelta(days=index), value) for index, value in enumerate(values)]


def test_rank_a_percentil_okna() -> None:
    assert rank_of(15.0, [10.0, 20.0]) == pytest.approx(0.5)
    assert rank_of(25.0, [10.0, 20.0]) == 1.0  # clamp
    assert rank_of(10.0, [10.0, 10.0]) is None  # degenerované okno

    history = series([float(value) for value in range(100)])  # dny SESSION−100 … SESSION−1
    rank, pct, sample = window_context(history, SESSION, 50.0)
    assert sample == 100  # všechny dny jsou před hodnoceným dnem
    assert rank == pytest.approx(50.0 / 99.0)
    assert pct is not None and 0.4 < pct < 0.6


def test_pod_min_sample_zadny_rank() -> None:
    """ADR-0028: percentil z hrstky dnů je náhoda vydávaná za měření."""
    rank, pct, sample = window_context(series([1.0] * 10), SESSION, 1.0)
    assert rank is None and pct is None and sample == 10


class _FakeIb:
    """reqContractDetails + reqHistoricalData s předpřipravenými bary."""

    def __init__(self, bars: list[SimpleNamespace]) -> None:
        self._bars = bars
        self.requests: list[str] = []

    async def reqContractDetailsAsync(self, contract: Any) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                contract=SimpleNamespace(lastTradeDateOrContractMonth="20260918", symbol="ES")
            )
        ]

    async def reqHistoricalDataAsync(self, contract: Any, **kwargs: Any) -> list[SimpleNamespace]:
        self.requests.append(str(kwargs.get("durationStr")))
        return self._bars


class _FakeTasty:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items
        self.paths: list[str] = []

    async def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        return {"data": {"items": self._items}}


def ib_bars(count: int) -> list[SimpleNamespace]:
    start = SESSION - dt.timedelta(days=count)
    return [
        SimpleNamespace(date=start + dt.timedelta(days=index), close=0.10 + 0.001 * index)
        for index in range(count)
    ]


async def run_collector(collector: IvRankCollector) -> None:
    after_settle = session_time_utc(SESSION, 16, 20, ET_TZ)
    await collector.on_minute(after_settle)


async def test_ibkr_backfill_pocita_rank_jen_z_minulosti(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    repository = IvRankRepository(db)
    ib = _FakeIb(ib_bars(100))
    collector = IvRankCollector(symbol="ES", repository=repository, db=db, ib=ib, tasty=None)
    await run_collector(collector)
    assert ib.requests == ["1 Y"]  # první běh = backfill
    rows = repository.series("ES", SOURCE_IBKR)
    assert len(rows) == 100
    latest = repository.latest("ES")
    ibkr = next(row for row in latest if row["source"] == SOURCE_IBKR)
    # Poslední den má okno 99 dnů minulosti a rostoucí řadu → rank u 1
    assert ibkr["sample"] == 99
    assert num(ibkr["iv_rank"]) > 0.95
    # Druhý běh (nová instance ~ restart) už jen dotahuje
    collector2 = IvRankCollector(symbol="ES", repository=repository, db=db, ib=ib, tasty=None)
    collector2._ibkr_backfilled = True
    await collector2._collect_ibkr(dt.datetime.now(dt.UTC), SESSION)
    assert ib.requests[-1] == "10 D"


async def test_tasty_cisla_se_prebiraji(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    repository = IvRankRepository(db)
    tasty = _FakeTasty(
        [
            {
                "symbol": "/ES",
                "implied-volatility-index": "0.1557",
                "implied-volatility-index-rank": "0.3119",
                "implied-volatility-percentile": "0.1883",
            },
            {"symbol": "ES", "implied-volatility-index": "0.2495"},  # equity ES — ignorovat
        ]
    )
    collector = IvRankCollector(symbol="ES", repository=repository, db=db, ib=None, tasty=tasty)
    await run_collector(collector)
    latest = repository.latest("ES")
    row = next(item for item in latest if item["source"] == SOURCE_TASTY)
    assert num(row["iv"]) == pytest.approx(0.1557)
    assert num(row["iv_rank"]) == pytest.approx(0.3119)
    assert num(row["iv_percentile"]) == pytest.approx(0.1883)
    assert tasty.paths == ["/market-metrics?symbols=%2FES"]


async def test_own_atm_tenor_a_atm_vyber(tmp_path: Path) -> None:
    """Expirace nejblíž ~7 dnům, strike nejblíž und_price s oběma stranami."""
    db = make_db(tmp_path)
    repository = IvRankRepository(db)
    rows = []
    for expiry, iv_call, iv_put in (
        ("20260827", 0.30, 0.32),  # 1 den — moc krátký tenor
        ("20260902", 0.20, 0.22),  # 7 dní — vítěz
        ("20260918", 0.18, 0.19),  # 23 dní
    ):
        for right, iv in (("C", iv_call), ("P", iv_put)):
            rows.append(
                {
                    "symbol": "ES",
                    "expiry": expiry,
                    "trading_class": "",
                    "strike": 7600.0,
                    "right": right,
                    "date": SESSION,
                    "oi": 10.0,
                    "iv": iv,
                    "und_price": 7598.0,
                }
            )
    # ATM kandidát bez druhé strany nesmí vyhrát ani při bližším striku
    rows.append(
        {
            "symbol": "ES",
            "expiry": "20260902",
            "trading_class": "",
            "strike": 7597.5,
            "right": "C",
            "date": SESSION,
            "oi": 10.0,
            "iv": 0.99,
            "und_price": 7598.0,
        }
    )
    with db.begin() as conn:
        conn.execute(insert(oi_eod_table), rows)
    collector = IvRankCollector(symbol="ES", repository=repository, db=db, ib=None, tasty=None)
    await run_collector(collector)
    latest = repository.latest("ES")
    own = next(item for item in latest if item["source"] == SOURCE_OWN_ATM)
    assert num(own["iv"]) == pytest.approx(0.21)  # (0.20 + 0.22) / 2
    assert own["iv_rank"] is None  # 0 dnů historie < MIN_SAMPLE — žádný default


async def test_pad_jedne_rady_neshodi_ostatni(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    repository = IvRankRepository(db)

    class _BrokenTasty:
        async def get_json(self, path: str) -> dict[str, Any]:
            raise RuntimeError("tasty down")

    ib = _FakeIb(ib_bars(70))
    collector = IvRankCollector(
        symbol="ES", repository=repository, db=db, ib=ib, tasty=_BrokenTasty()
    )
    await run_collector(collector)
    sources = {row["source"] for row in repository.latest("ES")}
    assert SOURCE_IBKR in sources and SOURCE_TASTY not in sources
