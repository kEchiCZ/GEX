"""Blokující údržba mimo minutový cyklus (#1337) — jeden běh naráz, výsledek se jen vyzvedne.

Měření disku (DiskWatch, přes bind mount až 121 s) i noční purge (~2,5 min)
běžely přes `await asyncio.to_thread(...)` PŘÍMO v cyklu: vlákno sice nedrželo
event loop, ale cyklus na něj čekal, takže 29. 9. chyběly minutové cykly ES
21:31–21:32 UTC. `BackgroundJob` práci odpálí jako úlohu a cyklus si v další
minutě jen vyzvedne hotový výsledek; dokud předchozí běh neskončil, další se
nespustí (souběžné procházení bind mountu by jen zdvojilo IO).
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundJob[T]:
    """Nejvýš jeden běh blokující funkce ve vlákně; výjimka se loguje, ne polyká."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._task: asyncio.Task[T] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, func: Callable[..., T], *args: Any) -> bool:
        """Odpálí `func(*args)` ve vlákně; False, když předchozí běh ještě neskončil.

        Nevyzvednutý hotový výsledek se zahodí — nový běh je čerstvější.
        """
        if self.running:
            logger.info("%s: předchozí běh ještě neskončil — nový se nespouští", self._name)
            return False
        task = asyncio.create_task(asyncio.to_thread(func, *args), name=self._name)
        task.add_done_callback(self._report_failure)
        self._task = task
        return True

    def take_result(self) -> T | None:
        """Výsledek doběhlého úspěšného běhu, a to jen jednou; jinak None."""
        task = self._task
        if task is None or not task.done():
            return None
        self._task = None
        if task.cancelled() or task.exception() is not None:
            return None  # už zalogováno v _report_failure
        return task.result()

    def _report_failure(self, task: "asyncio.Task[T]") -> None:
        if task.cancelled():
            logger.warning("%s: běh na pozadí byl zrušen", self._name)
            return
        exc = task.exception()
        if exc is not None:
            # Bez callbacku by pád skončil jen „Task exception was never retrieved"
            logger.error("%s selhal na pozadí", self._name, exc_info=exc)
