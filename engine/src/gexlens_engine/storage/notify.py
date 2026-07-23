"""PG LISTEN watchlistu (#207): probuzení orchestrátoru hned po změně v UI.

API po zápisu do watchlistu pošle `pg_notify(WATCHLIST_CHANNEL, symbol)`;
listener drží vlastní spojení s LISTEN a nastavuje asyncio.Event, na kterém
orchestrátor čeká místo prostého sleep — nový symbol tak startuje do sekund.
Mimo PostgreSQL (SQLite v testech/dev) se listener tiše neaktivuje a zůstává
pollovací fallback (WATCHLIST_POLL_CYCLES).

Spojení je synchronní psycopg v pracovním vlákně (asyncio.to_thread):
psycopg async neběží na Windows ProactorEventLoop a blokující
`Connection.notifies()` je oficiální vzor — přerušuje se zavřením spojení
z jiného vlákna (stop()).
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url

from gexlens_engine.storage.meta import WATCHLIST_CHANNEL

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0


def listen_dsn(database_url: str) -> str | None:
    """DSN pro LISTEN spojení (bez SQLAlchemy driveru); None = backend bez NOTIFY."""
    try:
        url = make_url(database_url)
    except Exception:
        return None
    if url.get_backend_name() != "postgresql":
        return None
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


class WatchlistListener:
    """Vlastní LISTEN spojení s reconnectem; pád listeneru nesmí shodit engine."""

    def __init__(self, database_url: str) -> None:
        self._dsn = listen_dsn(database_url)
        self._event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._connection: psycopg.Connection | None = None
        self._stopping = False

    @property
    def active(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._dsn is None:
            logger.info("Watchlist LISTEN neaktivní (backend bez NOTIFY) — zůstává poll")
            return
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        connection = self._connection
        if connection is not None:
            try:
                # Zavření z jiného vlákna přeruší blokující notifies() (psycopg vzor)
                connection.close()
            except Exception:
                logger.exception("Zavření LISTEN spojení selhalo — pokračuji")

    def trigger(self) -> None:
        """Ruční probuzení (testy, případný interní signál)."""
        self._event.set()

    async def wait(self, timeout_s: float) -> bool:
        """Čeká na notifikaci max timeout_s; True = probuzení změnou watchlistu."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        self._event.clear()
        return True

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                await asyncio.to_thread(self._listen_blocking, loop)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stopping:
                    return
                logger.warning(
                    "Watchlist LISTEN spojení spadlo — reconnect za %g s",
                    RECONNECT_DELAY_S,
                    exc_info=True,
                )
                await asyncio.sleep(RECONNECT_DELAY_S)

    def _listen_blocking(self, loop: asyncio.AbstractEventLoop) -> None:
        import psycopg

        assert self._dsn is not None  # start() jinak task nespouští
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            self._connection = connection
            # Kanál je konstanta modulu, ne uživatelský vstup
            connection.execute(f"LISTEN {WATCHLIST_CHANNEL}")
            logger.info("Watchlist LISTEN aktivní (kanál %s)", WATCHLIST_CHANNEL)
            for _ in connection.notifies():
                loop.call_soon_threadsafe(self._event.set)
