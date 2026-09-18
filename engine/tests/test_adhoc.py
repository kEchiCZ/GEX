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
        if product == "KO":
            # Akcie (#206 f. 2): dnešní 0DTE + další týden — po close se dnešek přeskočí
            by_contract.update(
                {
                    ("20260827", float(strike), right): f".KO260827{right}{strike}"
                    for strike in range(60, 91)
                    for right in ("C", "P")
                }
            )
        return ChainSymbols(product=product, day=day, by_contract=by_contract)

    async def front_future(self, product: str) -> str | None:
        if product == "KO":
            return "KO"  # akcie (#206): podklad je symbol sám
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


async def test_akcie_prvni_snapshot_hned_po_kotacich(tmp_path: Path) -> None:
    """#206: equity ad-hoc pohled — podklad = symbol, první snapshot bez čekání na minutu."""
    viewer, cache, db = make_viewer(tmp_path)
    request(db, "KO", NOW)
    await viewer.refresh(NOW)
    assert viewer.active() == ["KO"]
    # Bez kotací se první snapshot nezapíše (pásmo prázdné → nic)
    assert await viewer.write_first_snapshot(NOW) == 0
    feed_quote(cache, "KO", 74.9, 75.1)
    assert "KO" in viewer.streamers()
    band = [s for s in viewer.streamers() if s != "KO"]
    # Kotace pro 20 % pásma stačí — heatmapa naskočí do sekund, ne až na minutové hranici
    for streamer in band[: max(2, len(band) // 5 + 1)]:
        feed_quote(cache, streamer, 1.0, 1.2)
    written = await viewer.write_first_snapshot(NOW + dt.timedelta(seconds=7))
    assert written > 0
    # Opakované volání je no-op — o zbytek se stará minutová uzávěrka
    assert await viewer.write_first_snapshot(NOW + dt.timedelta(seconds=12)) == 0


async def test_akcie_po_close_preskoci_dnesni_0dte(tmp_path: Path) -> None:
    """#206 fáze 2: KO má expirace 20260827 (dnes) a 20260828; v 14:00 UTC (10:00 ET)
    je dnešní živá, ve 21:00 UTC (17:00 ET) už ne → bere se další."""
    viewer, _cache, db = make_viewer(tmp_path)
    request(db, "KO", NOW)
    await viewer.refresh(NOW)  # 14:00 UTC = 10:00 ET
    assert viewer._views["KO"].expiry == "20260827"

    viewer2, _cache2, db2 = make_viewer(tmp_path)
    after_close = NOW.replace(hour=21)  # 17:00 ET
    request(db2, "KO", after_close)
    await viewer2.refresh(after_close)
    assert viewer2._views["KO"].expiry == "20260828"


def test_equity_expiry_open_pravidla() -> None:
    from gexlens_engine.tasty.adhoc import equity_expiry_open

    # Akcie: 0DTE žije do 16:00 ET (20:00 UTC v létě)
    assert equity_expiry_open("20260827", "KO", dt.datetime(2026, 8, 27, 19, 59, tzinfo=dt.UTC))
    assert not equity_expiry_open("20260827", "KO", dt.datetime(2026, 8, 27, 20, 0, tzinfo=dt.UTC))
    # Index, 3. pátek (18. 9. 2026) = AM vypořádání 9:30 ET (13:30 UTC)
    assert equity_expiry_open("20260918", "SPX", dt.datetime(2026, 9, 18, 13, 29, tzinfo=dt.UTC))
    assert not equity_expiry_open(
        "20260918", "SPX", dt.datetime(2026, 9, 18, 13, 31, tzinfo=dt.UTC)
    )
    # Index, týdenní (pondělí) = 16:00 ET
    assert equity_expiry_open("20260921", "SPX", dt.datetime(2026, 9, 21, 19, 0, tzinfo=dt.UTC))
    assert not equity_expiry_open("20260921", "SPX", dt.datetime(2026, 9, 21, 20, 1, tzinfo=dt.UTC))
    # Včerejší / zítřejší
    assert not equity_expiry_open("20260826", "KO", dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.UTC))
    assert equity_expiry_open("20260828", "KO", dt.datetime(2026, 8, 27, 23, 0, tzinfo=dt.UTC))
    assert not equity_expiry_open("nesmysl", "KO", dt.datetime(2026, 8, 27, tzinfo=dt.UTC))
