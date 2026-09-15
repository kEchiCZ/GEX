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

- `malloc_trim` (noc 10./11. 9.): tracemalloc ukázal, že RSS roste MIMO
  Python heap (news-engine 3,1 GB RSS při ~28 MB sledovaných alokací, engine
  1,5 GB při ~230 MB) — glibc drží uvolněné bloky v arénách a OS je nedostane
  zpět. Po každém intervalovém vzorku se proto volá `malloc_trim(0)` (jen
  glibc; jinde no-op) a loguje se, kolik RSS vrátilo — to je zároveň měření:
  velký pokles = fragmentace, ne únik. Vypnutí `GEXLENS_MALLOC_TRIM=0`.
  Doplněk v compose: `MALLOC_ARENA_MAX=2` (méně arén = méně fragmentace).

- Arrow pool (15. 9.): trim vrací u news-engine ~280 MB za vzorek (RSS spadlo
  z 3,1 GB na 0,4–0,7 GB), u enginu jen ~45 MB — jeho ~3 GB je držená paměť,
  ne fragmentace glibc. Python heap byl podle tracemalloc ~230 MB, zbytek je
  nativní a největší kandidát je pyarrow: parquet čtení/zápis partic jede
  přes vlastní pool (mimalloc), který uvolněné bloky drží stranou glibc, takže
  `malloc_trim` na ně nedosáhne. Po každém vzorku se proto zaloguje
  `bytes_allocated` / `max_memory` poolu (kolik z RSS je Arrow) a zavolá
  `release_unused()` s měřením vráceného RSS. Vypnutí `GEXLENS_ARROW_RELEASE=0`.

Sdílí ho engine i news-engine (news-engine z balíku engine už importuje).
"""

import ctypes
import gc
import logging
import os
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

#: Env přepínač pro tracemalloc (1/true/yes); bez něj se loguje jen RSS
TRACE_ENV = "GEXLENS_MEMORY_TRACE"
#: Env přepínač pro glibc malloc_trim po vzorku (default zapnuto; 0/false vypne)
TRIM_ENV = "GEXLENS_MALLOC_TRIM"
#: Env přepínač pro měření Arrow poolu + release_unused po vzorku (default zapnuto)
ARROW_ENV = "GEXLENS_ARROW_RELEASE"
#: Pokles RSS po trimu, od kterého se loguje samostatný řádek (šum pod tím mlčí)
TRIM_LOG_MIN_MB = 20.0
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


def trim_enabled(environ: dict[str, str] | None = None) -> bool:
    """`GEXLENS_MALLOC_TRIM` — default zapnuto; vypíná jen výslovné 0/false/no/off."""
    value = (environ if environ is not None else os.environ).get(TRIM_ENV, "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def arrow_release_enabled(environ: dict[str, str] | None = None) -> bool:
    """`GEXLENS_ARROW_RELEASE` — default zapnuto; vypíná jen výslovné 0/false/no/off."""
    value = (environ if environ is not None else os.environ).get(ARROW_ENV, "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


class ArrowPool(Protocol):
    """Podmnožina `pyarrow.MemoryPool`, kterou hlídka používá (test si podstrčí atrapu)."""

    @property
    def backend_name(self) -> str: ...

    def bytes_allocated(self) -> int: ...

    def max_memory(self) -> int: ...

    def release_unused(self) -> None: ...


def _load_arrow_pool() -> ArrowPool | None:
    """Výchozí pool pyarrow; None bez pyarrow (news-engine ho nemusí mít) — nikdy výjimka."""
    try:
        import pyarrow as pa
    except ImportError:
        return None
    try:
        pool: ArrowPool = pa.default_memory_pool()
    except Exception:  # noqa: BLE001 — diagnostika nesmí shodit start
        return None
    return pool


def _load_malloc_trim() -> Callable[[], int] | None:
    """`malloc_trim(0)` z glibc; None mimo glibc (musl, macOS, Windows) — nikdy výjimka."""
    try:
        libc = ctypes.CDLL("libc.so.6")
        fn = libc.malloc_trim
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_size_t]
    fn.restype = ctypes.c_int

    def _trim() -> int:
        return int(fn(0))

    return _trim


class MemoryWatch:
    """Periodický vzorek RSS (+ tracemalloc) — volá se z minutového cyklu, škrtí se sama."""

    def __init__(
        self,
        name: str,
        logger: logging.Logger,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        trace: bool = False,
        trim: bool = False,
        arrow: bool = False,
        rss_provider: Callable[[], float | None] = rss_mb,
        clock: Callable[[], float] = time.monotonic,
        malloc_trim: Callable[[], int] | None = None,
        arrow_pool: ArrowPool | None = None,
    ) -> None:
        self._name = name
        self._logger = logger
        self._interval_s = interval_s
        self._trace = trace
        self._rss = rss_provider
        self._clock = clock
        # Trim jen když je zapnutý A libc ho umí; test si může podstrčit vlastní
        self._trim: Callable[[], int] | None = None
        if trim:
            self._trim = malloc_trim if malloc_trim is not None else _load_malloc_trim()
            if self._trim is None:
                logger.info("%s: malloc_trim nedostupný (není glibc) — přeskočen", name)
        #: Kolik MB vrátil poslední malloc_trim (do /status; None = nevolán)
        self.last_trim_mb: float | None = None
        # Arrow pool jen když je zapnutý A pyarrow je k dispozici
        self._arrow_pool: ArrowPool | None = None
        if arrow:
            self._arrow_pool = arrow_pool if arrow_pool is not None else _load_arrow_pool()
            if self._arrow_pool is None:
                logger.info("%s: Arrow pool nedostupný (bez pyarrow) — přeskočen", name)
        #: Alokace Arrow poolu při posledním vzorku v MB (do /status; None = neměřeno)
        self.last_arrow_mb: float | None = None
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
        return cls(
            name,
            logger,
            trace=trace_enabled(),
            trim=trim_enabled(),
            arrow=arrow_release_enabled(),
        )

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
        # Nejdřív Arrow (vrátí bloky svému alokátoru / OS), pak glibc trim —
        # v opačném pořadí by trim neměl na co sáhnout
        self._arrow_release_and_log()
        self._trim_and_log()
        return self.last_rss_mb

    def _arrow_release_and_log(self) -> None:
        """Podíl Arrow poolu na RSS + `release_unused()` s měřením vráceného RSS (#1105)."""
        pool = self._arrow_pool
        if pool is None or self.last_rss_mb is None:
            return
        before = self.last_rss_mb
        try:
            allocated_mb = pool.bytes_allocated() / 1024 / 1024
            peak_mb = pool.max_memory() / 1024 / 1024
            backend = pool.backend_name
            pool.release_unused()
        except Exception:  # noqa: BLE001 — diagnostika nesmí shodit sběr
            self._logger.exception("%s: Arrow pool selhal — vypínám", self._name)
            self._arrow_pool = None
            return
        self.last_arrow_mb = round(allocated_mb, 1)
        after = self._rss()
        if after is not None:
            self.last_rss_mb = after
        released = before - after if after is not None else 0.0
        self._logger.info(
            "%s: Arrow pool (%s) alokováno %.0f MB, max %.0f MB; release_unused vrátil %.0f MB",
            self._name,
            backend,
            allocated_mb,
            peak_mb,
            released,
        )

    def _trim_and_log(self) -> None:
        """glibc `malloc_trim(0)` + měření, kolik RSS se vrátilo OS (#1105)."""
        if self._trim is None or self.last_rss_mb is None:
            return
        before = self.last_rss_mb
        try:
            self._trim()
        except Exception:  # noqa: BLE001 — diagnostika nesmí shodit sběr
            self._logger.exception("%s: malloc_trim selhal — vypínám", self._name)
            self._trim = None
            return
        after = self._rss()
        if after is None:
            return
        self.last_rss_mb = after
        self.last_trim_mb = round(before - after, 1)
        if self.last_trim_mb >= TRIM_LOG_MIN_MB:
            self._logger.info(
                "%s: malloc_trim vrátil %.0f MB (RSS %.0f → %.0f MB)",
                self._name,
                self.last_trim_mb,
                before,
                after,
            )

    def note(self, label: str) -> float | None:
        """Vzorek bez škrcení — po těžkém jobu (#1105 bod 2), s `gc.collect()` před ním."""
        gc.collect()
        self.last_rss_mb = self._rss()
        if self.last_rss_mb is not None:
            self._logger.info("%s: RSS %.0f MB po %s", self._name, self.last_rss_mb, label)
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
