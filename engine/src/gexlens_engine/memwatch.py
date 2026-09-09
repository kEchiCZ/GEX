"""Hlídka paměti procesu (#1105): RSS do logu a stavu, volitelně tracemalloc.

9. 9. 2026 narostl engine za půl hodiny po startu z 1,2 na 3,0 GB a news-engine
z 0,4 na 1,0 GB; vmmem (WSL2 VM s Dockerem) seděl na stropu 6 GB a PC se
restartoval. Kde paměť roste, nevíme — a bez měření by oprava byla hádání
(pravidlo „příčina, ne symptom"). Proto:

- `rss_mb()` čte skutečnou rezidentní paměť z `/proc/self/status` (v kontejneru
  vždy Linux); mimo Linux vrací None a nic nepředstírá.
- `MemoryWatch.sample()` jednou za `interval_s` zaloguje RSS; s
  `GEXLENS_MEMORY_TRACE=1` navíc drží `tracemalloc` a vypíše top-N řádků
  kódu podle alokované paměti (rozdíl proti startu). tracemalloc stojí
  ~30 % CPU a paměť navíc, proto jen za flagem a jen po dobu hledání viníka.

Sdílí ho engine i news-engine (news-engine z balíku engine už importuje).
"""

import logging
import os
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

#: Env přepínač pro tracemalloc (1/true/yes); bez něj se loguje jen RSS
TRACE_ENV = "GEXLENS_MEMORY_TRACE"
DEFAULT_INTERVAL_S = 600.0
TOP_LINES = 10
_PROC_STATUS = Path("/proc/self/status")


def rss_mb() -> float | None:
    """Rezidentní paměť procesu v MB z /proc; None mimo Linux nebo při chybě."""
    try:
        text = _PROC_STATUS.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return round(int(parts[1]) / 1024, 1)
    return None


def trace_enabled(environ: dict[str, str] | None = None) -> bool:
    value = (environ if environ is not None else os.environ).get(TRACE_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


class MemoryWatch:
    """Periodický vzorek RSS (+ tracemalloc) — volá se z minutového cyklu, škrtí se sama."""

    def __init__(
        self,
        name: str,
        logger: logging.Logger,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        trace: bool = False,
        rss_provider: Callable[[], float | None] = rss_mb,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._logger = logger
        self._interval_s = interval_s
        self._trace = trace
        self._rss = rss_provider
        self._clock = clock
        self._last_sample: float | None = None
        self._baseline: tracemalloc.Snapshot | None = None
        #: Poslední změřené RSS — do /status bez dalšího čtení /proc
        self.last_rss_mb: float | None = None
        if trace:
            tracemalloc.start(25)
            self._baseline = tracemalloc.take_snapshot()
            logger.warning(
                "%s: tracemalloc zapnutý (%s) — dražší běh, jen na dobu hledání viníka",
                name,
                TRACE_ENV,
            )

    @classmethod
    def from_env(cls, name: str, logger: logging.Logger) -> "MemoryWatch":
        return cls(name, logger, trace=trace_enabled())

    def sample(self) -> float | None:
        """Změří RSS; loguje nejvýš jednou za interval. Vrací aktuální RSS."""
        now = self._clock()
        self.last_rss_mb = self._rss()
        if self._last_sample is not None and now - self._last_sample < self._interval_s:
            return self.last_rss_mb
        self._last_sample = now
        if self.last_rss_mb is None:
            self._logger.info("%s: RSS nedostupné (mimo Linux)", self._name)
        else:
            self._logger.info("%s: RSS %.0f MB", self._name, self.last_rss_mb)
        if self._trace:
            for line in self.top_lines():
                self._logger.info("%s: tracemalloc %s", self._name, line)
        return self.last_rss_mb

    def top_lines(self, limit: int = TOP_LINES) -> list[str]:
        """Top řádky kódu podle přírůstku alokované paměti od startu."""
        if not self._trace or self._baseline is None:
            return []
        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.compare_to(self._baseline, "lineno")
        out: list[str] = []
        for stat in stats[:limit]:
            frame = stat.traceback[0]
            out.append(
                f"+{stat.size_diff / 1024 / 1024:.1f} MB ({stat.count_diff:+d} bloků) "
                f"{frame.filename}:{frame.lineno}"
            )
        return out
