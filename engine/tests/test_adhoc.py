"""Ad-hoc pohled přes tasty (#521 C): životní cyklus, pásmo, zápis snapshotů."""

import datetime as dt
from pathlib import Path
from typing import cast

from sqlalchemy import create_engine, delete, insert, select

from gexlens_engine.config import Settings
from gexlens_engine.storage.meta import adhoc_view_table, ensure_meta_schema
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.tasty.adhoc import ADHOC_TTL_S, AdhocViewer
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap

NOW = dt.datetime(2026, 8, 27, 14, 0, tzinfo=dt.UTC)


class _FakeSymbolMap:
    """Chain CL: nejbližší expirace 20260828, strike 60–90 à 1."""

    async def chain(self, product: str, day: dt.date) -> ChainSymbols:
        by_contract = {
            ("20260828", float(strike), right): f".{product}{strike}{right}"
            for strike in range(60, 91)
            for right in ("C", "P")
        }
        return ChainSymbols(product=product, day=day, by_contract=by_contract)

    async def front_future(self, product: str) -> str | None:
        return f"/{product}V26:XNYM"


def make_viewer(tmp_path: Path) -> tuple[AdhocViewer, TastyChainCache, object]:
    db = create_engine("sqlite+pysqlite:///:memory:")
    ensure_meta_schema(db)
    cache = TastyChainCache(clock=lambda: NOW)
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    viewer = AdhocViewer(
        db=db,
        symbol_map=cast(SymbolMap, _FakeSymbolMap()),
        cache=cache,
        writer=writer,
        is_watched=lambda product: product in WATCHED,
    )
    return viewer, cache, db


#: Watchované produkty testů — mutable, ať jde simulovat pipeline, která
#: vznikla až po založení ad-hoc pohledu
WATCHED: set[str] = {"ES", "NQ"}


def request(db: object, symbol: str, ts: dt.datetime) -> None:
    """Stejně jako API: opakovaný požadavek řádek přepíše (prodloužení TTL)."""
    with db.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(delete(adhoc_view_table).where(adhoc_view_table.c.symbol == symbol))
        conn.execute(insert(adhoc_view_table).values(symbol=symbol, requested_ts=ts))


def feed_quote(cache: TastyChainCache, streamer: str, bid: float, ask: float) -> None:
    cache.on_event("Quote", [streamer, bid, ask, 1.0, 1.0])


async def test_zalozeni_a_uklid_po_ttl(tmp_path: Path) -> None:
    viewer, _cache, db = make_viewer(tmp_path)
    request(db, "CL", NOW)
    await viewer.refresh(NOW)
    assert viewer.active() == ["CL"]
    # Bez prodloužení po TTL pohled zmizí a řádek se smaže (uvolnění kapacity)
    later = NOW + dt.timedelta(seconds=ADHOC_TTL_S + 1)
    await viewer.refresh(later)
    assert viewer.active() == []
    with db.connect() as conn:  # type: ignore[attr-defined]
        assert conn.execute(select(adhoc_view_table)).fetchall() == []


async def test_pohled_ustoupi_kdyz_produkt_dostane_pipeline(tmp_path: Path) -> None:
    """4. 9.: ad-hoc NQ vznikl po startu enginu (pipelines prázdné) a přežíval
    díky pingům z UI i po návratu IBKR — chip „ad-hoc · tastytrade" nad IBKR daty."""
    viewer, _cache, db = make_viewer(tmp_path)
    WATCHED.discard("CL")
    try:
        request(db, "CL", NOW)
        await viewer.refresh(NOW)
        assert viewer.active() == ["CL"]
        WATCHED.add("CL")  # pipeline se postavila
        request(db, "CL", NOW + dt.timedelta(seconds=30))  # UI dál pinguje
        await viewer.refresh(NOW + dt.timedelta(seconds=31))
        assert viewer.active() == []
        with db.connect() as conn:  # type: ignore[attr-defined]
            assert conn.execute(select(adhoc_view_table)).fetchall() == []
    finally:
        WATCHED.discard("CL")


async def test_watchovane_symboly_se_nezakladaji(tmp_path: Path) -> None:
    viewer, _cache, db = make_viewer(tmp_path)
    request(db, "ES", NOW)
    await viewer.refresh(NOW)
    assert viewer.active() == []  # ES má plnou IBKR pipeline


async def test_streamers_pasmo_kolem_spotu(tmp_path: Path) -> None:
    viewer, cache, db = make_viewer(tmp_path)
    request(db, "CL", NOW)
    await viewer.refresh(NOW)
    # Bez spotu jde celá nejbližší expirace + front future
    assert len(viewer.streamers()) == 31 * 2 + 1
    # Se spotem 75 se drží jen ±8 % (69–81)
    feed_quote(cache, "/CLV26:XNYM", 74.9, 75.1)
    banded = viewer.streamers()
    assert ".CL75C" in banded and ".CL81C" in banded
    assert ".CL60C" not in banded and ".CL90P" not in banded


async def test_write_minute_zapisuje_snapshoty_a_bar(tmp_path: Path) -> None:
    viewer, cache, db = make_viewer(tmp_path)
    request(db, "CL", NOW)
    await viewer.refresh(NOW)
    feed_quote(cache, "/CLV26:XNYM", 74.9, 75.1)
    feed_quote(cache, ".CL75C", 1.2, 1.4)
    viewer.sample_spot()
    written = await viewer.write_minute(NOW.replace(second=0), NOW)
    assert written >= 1
    day = NOW.date().isoformat()
    assert (tmp_path / "snapshots" / "CL" / "20260828" / f"{day}.parquet").exists()
    bars_path = tmp_path / "derived" / "CL" / "bars" / f"{day}.parquet"
    assert bars_path.exists()
    import pyarrow.parquet as pq

    bar = pq.read_table(bars_path).to_pylist()[0]
    assert bar["close"] == 75.0
    assert bar["volume"] == 0.0  # kotace, ne obchody
