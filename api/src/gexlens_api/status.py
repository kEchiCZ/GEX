"""Stav pipeline pro GET /status (SPEC 3.7 + kap. 6).

Engine (samostatný proces / task groupa) do storu průběžně pushuje stav;
API vrací jeho poslední snapshot. Bez enginu je stav `engine: offline`.
"""

import time
from threading import Lock


class StatusStore:
    """Thread-safe úložiště posledního stavu enginu."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._status: dict[str, object] = {"engine": "offline"}
        self._updated_at: float | None = None

    def update(self, **fields: object) -> None:
        with self._lock:
            self._status.update(fields)
            self._status["engine"] = self._status.get("engine", "online")
            self._updated_at = time.time()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {**self._status, "updated_at": self._updated_at}
