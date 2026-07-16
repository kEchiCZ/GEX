"""PacingGuard (SPEC 3.6): globální rate limiter historical requestů.

IBKR penalizuje > 60 historical requestů za 10 minut (pacing violation).
Guard drží klouzavé okno požadavků, identické souběžné requesty deduplikuje
(sdílejí výsledek) a čekající požadavky pouští podle priority (nižší číslo
= dřív; aktuální den má přednost před backfillem historie).
"""

import asyncio
import heapq
import itertools
import time
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar

T = TypeVar("T")

_POLL_INTERVAL_S = 0.005


class PacingGuard:
    """Klouzavé okno max_requests/window_s s prioritní frontou a dedupem."""

    def __init__(self, max_requests: int = 60, window_s: float = 600.0) -> None:
        self._max_requests = max_requests
        self._window_s = window_s
        self._timestamps: deque[float] = deque()
        self._waiting: list[tuple[int, int]] = []  # heap (priorita, pořadí)
        self._seq = itertools.count()
        self._inflight: dict[Hashable, asyncio.Future[object]] = {}

    @property
    def used_slots(self) -> int:
        self._prune(time.monotonic())
        return len(self._timestamps)

    async def run(
        self,
        key: Hashable,
        func: Callable[[], Awaitable[T]],
        *,
        priority: int = 0,
    ) -> T:
        """Provede func pod rate limitem; identické souběžné klíče sdílí výsledek."""
        existing = self._inflight.get(key)
        if existing is not None:
            result = await asyncio.shield(existing)
            return result  # type: ignore[return-value]  # future nese výsledek téhož func

        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        self._inflight[key] = future
        try:
            await self._acquire_slot(priority)
            result_value = await func()
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            future.exception()  # označit jako převzatou, ať event loop neloguje
            raise
        else:
            future.set_result(result_value)
            return result_value
        finally:
            self._inflight.pop(key, None)

    async def _acquire_slot(self, priority: int) -> None:
        ticket = (priority, next(self._seq))
        heapq.heappush(self._waiting, ticket)
        try:
            while True:
                now = time.monotonic()
                self._prune(now)
                if self._waiting[0] == ticket and len(self._timestamps) < self._max_requests:
                    heapq.heappop(self._waiting)
                    self._timestamps.append(now)
                    return
                await asyncio.sleep(_POLL_INTERVAL_S)
        except BaseException:
            self._waiting.remove(ticket)
            heapq.heapify(self._waiting)
            raise

    def _prune(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self._window_s:
            self._timestamps.popleft()
