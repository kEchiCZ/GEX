"""Start enginu bez běžícího TWS (#756).

Do #756 čekal `main()` na IBKR spojení v nekonečné smyčce, takže za tou
bariérou zůstalo VŠECHNO — schéma DB, pipelines, tastytrade větev i spot
fallback z #614. Bez spuštěné TWS tedy neběželo nic a v logu nebyl jediný
řádek `tasty`, přestože fallback má chránit právě proti mlčícímu IBKR.
"""

import asyncio
from typing import cast

from gexlens_engine.__main__ import wait_for_connection
from gexlens_engine.ibkr.connection import ConnectionManager, ConnectionState


class FakeManager:
    """Jen `state` — `wait_for_connection` nic jiného nepotřebuje."""

    def __init__(self, state: ConnectionState = ConnectionState.RECONNECTING) -> None:
        self.state = state


def as_manager(fake: FakeManager) -> ConnectionManager:
    return cast(ConnectionManager, fake)


async def test_pripojene_ibkr_ceka_nulu() -> None:
    fake = FakeManager(ConnectionState.CONNECTED)

    assert await wait_for_connection(as_manager(fake), 5.0) is True


async def test_mlcici_ibkr_start_neblokuje() -> None:
    """Jádro issue: po vypršení stropu se pokračuje dál, ne že se čeká věčně."""
    fake = FakeManager()

    assert await wait_for_connection(as_manager(fake), 0.05) is False


async def test_pripojeni_behem_cekani_se_zachyti() -> None:
    """Běžný start: TWS se rozjíždí souběžně s enginem."""
    fake = FakeManager()

    async def connect_later() -> None:
        await asyncio.sleep(0.02)
        fake.state = ConnectionState.CONNECTED

    task = asyncio.create_task(connect_later())
    result = await wait_for_connection(as_manager(fake), 5.0)
    await task

    assert result is True


async def test_nulovy_strop_necheka_vubec() -> None:
    """Konfigurace 0 = „nečekej"; hlavní smyčka si pipeline založí, až spojení bude."""
    fake = FakeManager()

    assert await wait_for_connection(as_manager(fake), 0.0) is False
