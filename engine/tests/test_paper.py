"""Paper účet (#1187 fáze 1): fily proti barům, stop-first, settle, deník, brzdy."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from gexlens_engine.compute.paper import PaperOrder, evaluate_open, fill_entry, validate_levels
from gexlens_engine.compute.risk import brake_state
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.paper import PaperBroker
from gexlens_engine.runtime import PublisherLike
from gexlens_engine.storage.meta import ensure_meta_schema
from gexlens_engine.storage.paper_store import PaperRepository

TS = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)  # 10:00 ET


def bar(minute: int, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(ts=TS + dt.timedelta(minutes=minute), open=o, high=h, low=low, close=c, volume=10.0)


def order(**overrides: Any) -> PaperOrder:
    base: dict[str, Any] = {
        "id": 1,
        "symbol": "ES",
        "side": "long",
        "qty": 1,
        "order_type": "limit",
        "entry_price": 7600.0,
        "stop_price": 7592.0,
        "target_price": 7616.0,
        "status": "working",
    }
    base.update(overrides)
    return PaperOrder(**base)


def test_fily_vstupu_podle_typu_orderu() -> None:
    # Limit long se plní až když bar protne úroveň; gap pod úroveň = fill na open
    assert fill_entry(order(), bar(0, 7605, 7608, 7601, 7603)) is None
    fill = fill_entry(order(), bar(0, 7603, 7604, 7598, 7599))
    assert fill is not None and fill.price == 7600.0
    gap = fill_entry(order(), bar(0, 7597, 7599, 7595, 7598))
    assert gap is not None and gap.price == 7597.0
    # Market: open + 1 tick proti (long +0.25, short −0.25)
    market_long = fill_entry(order(order_type="market"), bar(0, 7603, 7604, 7598, 7599))
    assert market_long is not None and market_long.price == 7603.25
    market_short = fill_entry(
        order(order_type="market", side="short", stop_price=7608.0, target_price=7584.0),
        bar(0, 7603, 7604, 7598, 7599),
    )
    assert market_short is not None and market_short.price == 7602.75
    # Stop vstup long: průraz nahoru → max(úroveň, open) + tick
    stop_in = fill_entry(
        order(order_type="stop", entry_price=7605.0, stop_price=7597.0),
        bar(0, 7603, 7607, 7602, 7606),
    )
    assert stop_in is not None and stop_in.price == 7605.25
    # Short limit
    short = fill_entry(
        order(side="short", stop_price=7608.0, target_price=7584.0), bar(0, 7598, 7601, 7597, 7600)
    )
    assert short is not None and short.price == 7600.0


def test_vystup_stop_first_a_slippage() -> None:
    open_long = order(status="open", fill_price=7600.0)
    # Bar zasáhne stop i cíl → stop, s tickem proti (7592 − 0.25)
    exit_ = evaluate_open(open_long, bar(1, 7600, 7617, 7591, 7605))
    assert exit_ is not None and exit_.reason == "stop" and exit_.price == 7591.75
    # Jen cíl → přesně na cíli
    exit_ = evaluate_open(open_long, bar(1, 7600, 7617, 7598, 7615))
    assert exit_ is not None and exit_.reason == "target" and exit_.price == 7616.0
    # Gap pod stop → fill na open − tick
    exit_ = evaluate_open(open_long, bar(1, 7588, 7590, 7585, 7589))
    assert exit_ is not None and exit_.price == 7587.75
    # Bez cíle se nikdy nezavře cílem
    assert (
        evaluate_open(
            order(status="open", fill_price=7600.0, target_price=None),
            bar(1, 7600, 7700, 7599, 7690),
        )
        is None
    )
    assert validate_levels("long", "limit", 7600, 7610, None) == "stop musí být pod entry (long)"
    assert validate_levels("short", "limit", 7600, 7610, 7620) == "cíl musí být pod entry (short)"
    assert validate_levels("long", "limit", 7600, 7590, 7620) is None


class _Publisher(PublisherLike):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.events.append({"channel": channel, **data})


def _repo(tmp_path: Path) -> PaperRepository:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    ensure_meta_schema(db)  # deník — zápis uzavřeného obchodu
    repo = PaperRepository(db)
    repo.ensure_schema()
    return repo


def _place(repo: PaperRepository, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "account_id": 1,
        "symbol": "ES",
        "side": "long",
        "qty": 2,
        "order_type": "limit",
        "entry_price": 7600.0,
        "stop_price": 7595.0,
        "target_price": 7615.0,
        "status": "working",
        "created_ts": TS,
        "risk_usd": 500.0,
        "point_value": 50.0,
        "setup_key": "failed_break",
        "context": {"note": "test"},
        "close_requested": False,
    }
    values.update(overrides)
    return repo.create_order(values)


def test_broker_fill_cil_denik_a_equity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert repo.account() is not None and repo.equity() == 50000.0
    order_id = _place(repo)
    publisher = _Publisher()
    broker = PaperBroker("ES", repo, publisher)
    # Minuta 1: bar nad úrovní — nic; minuta 2: protne 7600 → fill; minuta 3: cíl 7615
    asyncio.run(
        broker.on_minute(TS + dt.timedelta(minutes=1), 7605.0, [bar(1, 7605, 7608, 7602, 7604)])
    )
    assert repo.get_order(order_id)["status"] == "working"  # type: ignore[index]
    asyncio.run(
        broker.on_minute(TS + dt.timedelta(minutes=2), 7599.0, [bar(2, 7603, 7604, 7599, 7600)])
    )
    stored = repo.get_order(order_id)
    assert stored is not None and stored["status"] == "open" and stored["fill_price"] == 7600.0
    asyncio.run(
        broker.on_minute(TS + dt.timedelta(minutes=3), 7616.0, [bar(3, 7601, 7617, 7600, 7616)])
    )
    closed = repo.get_order(order_id)
    assert closed is not None and closed["status"] == "closed" and closed["exit_reason"] == "target"
    # 2 kontrakty × 15 b × 50 $ = 1 500 $ − poplatky 2 × 10 $ = 1 480 $; R = 15/5 = 3
    assert closed["pnl_usd"] == 1480.0 and closed["r_multiple"] == 3.0 and closed["mfe"] == 17.0
    assert repo.equity() == 51480.0
    assert closed["journal_entry_id"] is not None
    kinds = [e["event"] for e in publisher.events if e.get("kind") == "paper"]
    assert kinds == ["filled", "closed"]
    assert any(e["channel"] == "paper.ES" for e in publisher.events)


def test_broker_stop_first_manual_close_a_settle(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    broker = PaperBroker("ES", repo, _Publisher())
    # Stop-first: bar po fillu zasáhne stop i cíl → stop s tickem proti
    stop_id = _place(repo)
    asyncio.run(
        broker.on_minute(
            TS, 7600.0, [bar(0, 7603, 7604, 7599, 7600), bar(1, 7600, 7620, 7594, 7610)]
        )
    )
    stopped = repo.get_order(stop_id)
    assert (
        stopped is not None
        and stopped["exit_reason"] == "stop"
        and stopped["exit_price"] == 7594.75
    )
    assert stopped["pnl_usd"] == (7594.75 - 7600.0) * 2 * 50 - 20.0
    # Ruční zavření: open pozice + close_requested → výstup na open dalšího baru
    manual_id = _place(repo, order_type="market", entry_price=7600.0)
    asyncio.run(
        broker.on_minute(TS + dt.timedelta(minutes=5), 7600.0, [bar(5, 7600, 7602, 7599, 7601)])
    )
    repo.update_order(manual_id, close_requested=True)
    asyncio.run(
        broker.on_minute(TS + dt.timedelta(minutes=6), 7602.0, [bar(6, 7602, 7603, 7601, 7602)])
    )
    manual = repo.get_order(manual_id)
    assert (
        manual is not None and manual["exit_reason"] == "manual" and manual["exit_price"] == 7602.0
    )
    # Settle: otevřená pozice se zavře na close, čekající zruší
    open_id = _place(repo, order_type="market", entry_price=7600.0)
    waiting_id = _place(
        repo,
        symbol="NQ",
        entry_price=29000.0,
        stop_price=28980.0,
        target_price=29050.0,
        point_value=20.0,
    )
    asyncio.run(
        broker.on_minute(TS + dt.timedelta(minutes=7), 7600.0, [bar(7, 7600, 7601, 7599, 7600)])
    )
    settle = dt.datetime(2026, 9, 17, 20, 0, tzinfo=dt.UTC)
    asyncio.run(
        broker.on_minute(
            settle,
            7610.0,
            [
                Bar(
                    ts=settle - dt.timedelta(minutes=1),
                    open=7609,
                    high=7611,
                    low=7608,
                    close=7610,
                    volume=1,
                )
            ],
        )
    )
    settled = repo.get_order(open_id)
    assert (
        settled is not None
        and settled["exit_reason"] == "settle"
        and settled["exit_price"] == 7610.0
    )
    nq = PaperBroker("NQ", repo, _Publisher())
    asyncio.run(
        nq.on_minute(
            settle,
            29000.0,
            [
                Bar(
                    ts=settle - dt.timedelta(minutes=1),
                    open=29100,
                    high=29110,
                    low=29090,
                    close=29100,
                    volume=1,
                )
            ],
        )
    )
    cancelled = repo.get_order(waiting_id)
    assert (
        cancelled is not None
        and cancelled["status"] == "cancelled"
        and cancelled["exit_reason"] == "settle"
    )
    # Brzdy z realizovaných paper obchodů: dnes −1 R (stop) + 0 … → bez brzdy; realized má 3 řádky
    realized = repo.realized_since(TS - dt.timedelta(days=1))
    assert len(realized) == 3 and all(r.tradeable for r in realized)
    state = brake_state(
        realized,
        "paper",
        session_day=dt.date(2026, 9, 17),
        daily_brake_r=3.0,
        weekly_brake_r=6.0,
        max_template_stops_per_day=0,
    )
    assert state.block is None and state.day_r > 0  # −1,05 R stop + dva kladné výstupy
