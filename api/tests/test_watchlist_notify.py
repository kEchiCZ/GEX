"""Test NOTIFY po zápisu do watchlistu (#207) — end-to-end proti reálnému PG."""

import asyncio
import os
import uuid

import pytest

from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.config import Settings
from gexlens_engine.storage.notify import WatchlistListener


@pytest.mark.skipif(
    not os.environ.get("GEXLENS_TEST_PG_DSN"),
    reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)",
)
async def test_watchlist_add_wakes_engine_listener() -> None:
    dsn = os.environ["GEXLENS_TEST_PG_DSN"]
    repository = MetaRepository(Settings(database_url=dsn))
    listener = WatchlistListener(dsn)
    listener.start()
    created: list[int] = []
    try:
        # LISTEN spojení se teprve navazuje → přidávat unikátní symboly,
        # dokud notifikace nedorazí (max 10 s)
        deadline = asyncio.get_running_loop().time() + 10.0
        woken = False
        while asyncio.get_running_loop().time() < deadline:
            symbol = f"T{uuid.uuid4().hex[:6].upper()}"
            created.append(int(repository.watchlist_add(symbol)["id"]))
            if await listener.wait(0.5):
                woken = True
                break
        assert woken, "watchlist_add neprobudil engine listener do 10 s"
        # Odebrání notifikuje také (orchestrátor zastaví pipeline dřív)
        repository.watchlist_remove(created.pop())
        assert await listener.wait(5.0) is True
    finally:
        listener.stop()
        for item_id in created:
            repository.watchlist_remove(item_id)


def test_ulozeni_nastaveni_notifikuje_engine(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#992: PUT /settings musí probudit orchestrátor, jinak se port přepne až za 5 min."""
    from gexlens_api import meta_repo

    calls: list[str] = []
    monkeypatch.setattr(meta_repo, "_notify_watchlist", lambda conn, payload: calls.append(payload))
    repository = MetaRepository(
        Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'meta.db'}")
    )
    repository.setting_put("ibkr_port", 4001)
    repository.setting_put("ibkr_port", 4002)  # update i insert větev

    assert calls == ["", ""]
