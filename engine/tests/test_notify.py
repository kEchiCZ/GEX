"""Testy PG LISTEN watchlistu (#207): DSN, čekání s timeoutem, PG integrace."""

import asyncio
import os

import pytest
from sqlalchemy import create_engine, text

from gexlens_engine.storage.meta import WATCHLIST_CHANNEL
from gexlens_engine.storage.notify import WatchlistListener, listen_dsn


def test_listen_dsn_only_for_postgres() -> None:
    assert (
        listen_dsn("postgresql+psycopg://user:secret@localhost:5432/gexlens")
        == "postgresql://user:secret@localhost:5432/gexlens"
    )
    assert listen_dsn("sqlite+pysqlite:///meta.sqlite") is None
    assert listen_dsn("nesmysl") is None


async def test_listener_inactive_without_postgres() -> None:
    listener = WatchlistListener("sqlite+pysqlite:///meta.sqlite")
    listener.start()
    assert listener.active is False  # žádný task, žádný pád — zůstává poll


async def test_wait_returns_on_trigger_and_clears() -> None:
    listener = WatchlistListener("sqlite+pysqlite:///meta.sqlite")
    listener.trigger()
    assert await listener.wait(0.05) is True
    # Event se po probuzení čistí — další čekání vyprší
    assert await listener.wait(0.05) is False


@pytest.mark.skipif(
    not os.environ.get("GEXLENS_TEST_PG_DSN"),
    reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)",
)
async def test_notify_wakes_listener_on_real_postgres() -> None:
    dsn = os.environ["GEXLENS_TEST_PG_DSN"]
    listener = WatchlistListener(dsn)
    listener.start()
    assert listener.active is True
    engine = create_engine(dsn)
    try:
        # LISTEN spojení se teprve navazuje → notify opakovaně, dokud nedorazí
        deadline = asyncio.get_running_loop().time() + 10.0
        woken = False
        while asyncio.get_running_loop().time() < deadline:
            with engine.begin() as connection:
                connection.execute(
                    text("select pg_notify(:channel, 'ES')"), {"channel": WATCHLIST_CHANNEL}
                )
            if await listener.wait(0.5):
                woken = True
                break
        assert woken, "NOTIFY listener neprobudil do 10 s"
    finally:
        listener.stop()
        engine.dispose()
