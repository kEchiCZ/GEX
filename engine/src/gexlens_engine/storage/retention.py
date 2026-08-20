"""RetentionJob (SPEC 5.2 + R3/R4): noční purge Parquet partic starších retention_days.

Maže výhradně denní partice pod `snapshots/`, `ticks/` a `derived/` — k databázi
(oi_eod, R4) job vůbec nemá přístup, takže ji z principu nemůže poškodit.
Součástí je monitoring obsazení disku s hard limitem (alert pro UI/notifikace).

**Výjimka: 1min bary podkladu se nemažou nikdy** (SPEC SentimentLens S4, #275).
Jsou to trénovací data — bez nich nejde spočítat volume z-score reakčních oken
(potřebuje 20 seancí, tedy víc než 14denní okno) ani zpětně přepočítat reakce
na zprávy. Objem je zanedbatelný: 2 symboly × ~1400 barů/den ≈ desítky MB/rok.
Stejný duch jako věčný OI archiv (ADR-0001).

**Výjimka: snapshots/ a derived/ se nemažou nikdy** (#762, ADR-0029). Jsou to
nenahraditelná učicí data — IBKR historii řetězce zpětně nedá a samoučící
smyčka (#794) se nad nimi učí replayem (`scripts/backtest_setups.py` z nich
rekonstruuje MinuteInputs). Precedens ztráty: #575 nemohl doplnit 495 setupů,
protože profily byly za retencí. S výjimkou zapnutou purge reálně maže jen
`ticks/`; výjimka je vypnutelná (`keep_learning_data_forever=False`).
"""

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from gexlens_engine.config import Settings

logger = logging.getLogger(__name__)

# Adresář s 1min bary podkladu; partice pod ním retence nemaže (S4, #275)
BARS_DIR_NAME = "bars"


@dataclass(frozen=True)
class RetentionReport:
    """Výsledek jednoho purge běhu pro log, stavovou lištu a alerty."""

    deleted: tuple[Path, ...]
    kept_files: int
    disk_usage_bytes: int
    disk_limit_bytes: int
    disk_limit_exceeded: bool


class RetentionJob:
    """Purge partic starších než retention okno + kontrola obsazení disku."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def purge(self, today: dt.date) -> RetentionReport:
        """Smaže partice starší než retention_days; nečitelné názvy nechává být.

        Partice stará přesně retention_days dní se ještě ponechává — maže se
        až „starší než" okno (15. den při retenci 14).
        """
        deleted: list[Path] = []
        kept = 0
        for root in (
            self._settings.snapshots_dir,
            self._settings.ticks_dir,
            self._settings.derived_dir,
        ):
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.parquet")):
                if self._is_protected(path):
                    kept += 1
                    continue
                day = self._partition_day(path)
                if day is None:
                    logger.warning("Partice s nerozpoznatelným datem, ponechávám: %s", path)
                    kept += 1
                    continue
                if (today - day).days > self._settings.retention_days:
                    path.unlink()
                    deleted.append(path)
                else:
                    kept += 1
        self._remove_empty_dirs()

        usage = self._disk_usage_bytes()
        limit = int(self._settings.disk_limit_gb * 1024**3)
        exceeded = usage > limit
        if exceeded:
            logger.warning("Obsazení disku %d B překročilo limit %d B — alert pro UI", usage, limit)
        if deleted:
            logger.info("Retention purge: smazáno %d partic, ponecháno %d", len(deleted), kept)
        return RetentionReport(
            deleted=tuple(deleted),
            kept_files=kept,
            disk_usage_bytes=usage,
            disk_limit_bytes=limit,
            disk_limit_exceeded=exceeded,
        )

    def _is_protected(self, path: Path) -> bool:
        """Partice vyňatá z retence — rozhoduje se podle adresáře, ne stáří.

        Dvě nezávislé výjimky: věčný archiv 1min barů (S4, #275,
        `derived/{symbol}/bars/`) a věčný archiv učicích dat (#762, ADR-0029,
        celé `snapshots/` a `derived/`). Nezávislé proto, aby vypnutí jedné
        nestrhlo druhou: bary chrání SentimentLens i při
        `keep_learning_data_forever=False`.
        """
        if self._settings.keep_bars_forever and BARS_DIR_NAME in path.parts:
            return True
        if self._settings.keep_learning_data_forever:
            for root in (self._settings.snapshots_dir, self._settings.derived_dir):
                if path.is_relative_to(root):
                    return True
        return False

    def seconds_until_next_run(self, now: dt.datetime) -> float:
        """Prodleva do dalšího nočního běhu (konfig. čas UTC po zavření US)."""
        run_time = self._settings.retention_purge_time_utc
        candidate = now.replace(hour=run_time.hour, minute=run_time.minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return (candidate - now).total_seconds()

    def _partition_day(self, path: Path) -> dt.date | None:
        try:
            return dt.date.fromisoformat(path.stem)
        except ValueError:
            return None

    def _disk_usage_bytes(self) -> int:
        data_dir = self._settings.data_dir
        if not data_dir.exists():
            return 0
        return sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())

    def _remove_empty_dirs(self) -> None:
        """Po purge uklidí prázdné adresáře partic (symbol/expirace bez dat)."""
        for root in (
            self._settings.snapshots_dir,
            self._settings.ticks_dir,
            self._settings.derived_dir,
        ):
            if not root.exists():
                continue
            for directory in sorted(root.rglob("*"), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
