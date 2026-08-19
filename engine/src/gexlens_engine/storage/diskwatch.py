"""Dohled nad volným místem (#773) — měření a alerty, žádné mazání.

19. 8. byly oba disky stroje pod 20 GB volného místa a poznalo se to jen
ručním pohledem — /status hlásil `disk — / —` a docházející místo by se
projevilo až pádem (zaseknutý zápis PostgreSQL položí celý stack, viz
incident 17. 8. s vyčerpanou pamětí WSL).

Co jde z kontejneru měřit POCTIVĚ (ověřeno 19. 8. na živém stacku):

* **bind mount datového adresáře** — `shutil.disk_usage` vrací skutečná
  čísla hostitelského disku (D:, 259 GB total / 19 GB free),
* **velikost PostgreSQL databáze** — `pg_database_size()` je přesný tahoun
  růstu (feed_comparison ~500 MB/den, #757).

Co z kontejneru změřit NEJDE: obsazení hostitelského disku WSL vhdx, ve
kterém žije PG volume — statfs uvnitř VM ukazuje virtuální kapacitu
(1 TB total / 924 GB free), ne realitu hostitele. Na tomhle stroji leží
vhdx na D: (`D:\Programy\Docker\DockerDesktopWSL`, dohledáno 19. 8.),
tedy na TÉMŽE disku jako datový adresář — jeho růst tak měřené volné
místo ukusuje přímo. Vlastní práh na velikost DB (`db_size_alert_gb`)
zůstává jako časná výstraha na tahouna růstu a pojistka pro konfigurace,
kde vhdx leží na jiném disku než data.

Alert má hysterezi: hlásí se přechod mezi úrovněmi hned, trvající stav
nejvýš jednou za cooldown; návrat do pořádku úrovně re-armuje.
"""

import datetime as dt
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

#: Kolik největších tabulek se vypisuje do alertu — žrouti se nemají hledat ručně
TOP_TABLES = 5


@dataclass(frozen=True)
class DiskSnapshot:
    """Jedno měření; hodnoty v bajtech. None = zdroj se nepodařilo změřit."""

    ts: float
    data_dir_bytes: int
    disk_free_bytes: int | None
    disk_total_bytes: int | None
    db_bytes: int | None
    #: (název tabulky, bajty vč. indexů), největší první
    top_tables: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DiskAlert:
    level: str  # "warning" | "critical"
    message: str


def _gb(value: int | None) -> str:
    return "?" if value is None else f"{value / 1024**3:.1f} GB"


class DiskWatch:
    """Měření obsazení disku + vyhodnocení prahů s hysterezí a cooldownem.

    `measure()` je blokující (statfs, rglob, SQL) — volá se přes `to_thread`
    z hlavní smyčky v intervalu `interval_s`; mezitím se vrací poslední
    snímek. Vyhodnocení je čistá funkce nad snímkem, testovatelná bez disku.
    """

    def __init__(
        self,
        data_dir: Path,
        db: Engine | None,
        *,
        warn_free_gb: float = 15.0,
        crit_free_gb: float = 5.0,
        db_alert_gb: float = 4.0,
        interval_s: float = 600.0,
        cooldown_s: float = 6 * 3600.0,
    ) -> None:
        self._data_dir = data_dir
        self._db = db
        self._warn_free = int(warn_free_gb * 1024**3)
        self._crit_free = int(crit_free_gb * 1024**3)
        self._db_alert = int(db_alert_gb * 1024**3)
        self._interval_s = interval_s
        self._cooldown_s = cooldown_s
        self.last: DiskSnapshot | None = None
        self._last_measure: float | None = None
        self._last_level: str | None = None
        self._last_alert_ts: float | None = None

    # ── měření ────────────────────────────────────────────────────────

    def tick(self, now: float) -> DiskSnapshot | None:
        """Změří, jen když uplynul interval; vrací aktuální snímek (nebo poslední)."""
        if self._last_measure is not None and now - self._last_measure < self._interval_s:
            return self.last
        self._last_measure = now
        self.last = self.measure(now)
        return self.last

    def measure(self, now: float) -> DiskSnapshot:
        data_bytes = self._data_dir_bytes()
        free = total = None
        try:
            usage = shutil.disk_usage(self._data_dir)
            free, total = usage.free, usage.total
        except OSError as exc:
            logger.warning("Volné místo datového disku se nepodařilo změřit: %s", exc)
        db_bytes: int | None = None
        top: tuple[tuple[str, int], ...] = ()
        if self._db is not None:
            try:
                db_bytes, top = self._measure_db()
            except Exception as exc:
                # Dohled nesmí shodit hlavní smyčku — bez čísla se jen loguje
                logger.warning("Velikost PostgreSQL se nepodařilo změřit: %s", exc)
        return DiskSnapshot(
            ts=now,
            data_dir_bytes=data_bytes,
            disk_free_bytes=free,
            disk_total_bytes=total,
            db_bytes=db_bytes,
            top_tables=top,
        )

    def _data_dir_bytes(self) -> int:
        if not self._data_dir.exists():
            return 0
        return sum(f.stat().st_size for f in self._data_dir.rglob("*") if f.is_file())

    def _measure_db(self) -> tuple[int | None, tuple[tuple[str, int], ...]]:
        with self._db.connect() as conn:  # type: ignore[union-attr]
            if conn.dialect.name != "postgresql":
                return None, ()  # testy běží nad SQLite — bez PG čísel
            size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
            rows = conn.execute(
                text(
                    "SELECT relname, pg_total_relation_size(oid) AS bytes"
                    " FROM pg_class"
                    " WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"
                    " ORDER BY bytes DESC LIMIT :n"
                ),
                {"n": TOP_TABLES},
            ).all()
        return int(size), tuple((str(name), int(size_)) for name, size_ in rows)

    # ── vyhodnocení ──────────────────────────────────────────────────

    def evaluate(self, snapshot: DiskSnapshot) -> DiskAlert | None:
        """Alert při podkročení prahů; hystereze viz docstring modulu."""
        level = self._level(snapshot)
        if level is None:
            if self._last_level is not None:
                logger.info("Volné místo se vrátilo nad prahy dohledu (#773)")
            self._last_level = None
            self._last_alert_ts = None
            return None
        escalated = self._last_level != level
        if (
            not escalated
            and self._last_alert_ts is not None
            and snapshot.ts - self._last_alert_ts < self._cooldown_s
        ):
            return None
        self._last_level = level
        self._last_alert_ts = snapshot.ts
        return DiskAlert(level=level, message=self._message(level, snapshot))

    def _level(self, snapshot: DiskSnapshot) -> str | None:
        free = snapshot.disk_free_bytes
        if free is not None and free < self._crit_free:
            return "critical"
        db_over = snapshot.db_bytes is not None and snapshot.db_bytes > self._db_alert
        if (free is not None and free < self._warn_free) or db_over:
            return "warning"
        return None

    def _message(self, level: str, snapshot: DiskSnapshot) -> str:
        eaters = ", ".join(f"{name} {_gb(size)}" for name, size in snapshot.top_tables[:TOP_TABLES])
        parts = [
            ("KRITICKY málo místa na datovém disku" if level == "critical" else "Dochází místo"),
            f"volných {_gb(snapshot.disk_free_bytes)} z {_gb(snapshot.disk_total_bytes)}",
            f"data/ {_gb(snapshot.data_dir_bytes)}",
            f"PostgreSQL {_gb(snapshot.db_bytes)}",
        ]
        if snapshot.db_bytes is not None and snapshot.db_bytes > self._db_alert:
            parts.append(
                f"DB přerostla práh {_gb(self._db_alert)} — plní WSL disk Dockeru"
            )
        if eaters:
            parts.append(f"největší tabulky: {eaters}")
        parts.append("úklid řeší #757")
        return "; ".join(parts) + "."

    # ── pole do /status ──────────────────────────────────────────────

    def status_fields(self, disk_limit_bytes: int) -> dict[str, object]:
        """Vyplní existující pole patičky (`disk — / —`) + přesná čísla dohledu.

        Klíče chybí, dokud neproběhlo první měření — konvence #517 A
        (nepřítomnost = neměří se, ne „nula").
        """
        if self.last is None:
            return {}
        fields: dict[str, object] = {
            "disk_usage_bytes": self.last.data_dir_bytes,
            "disk_limit_bytes": disk_limit_bytes,
        }
        if self.last.disk_free_bytes is not None:
            fields["disk_free_bytes"] = self.last.disk_free_bytes
        if self.last.db_bytes is not None:
            fields["db_size_bytes"] = self.last.db_bytes
        return fields


def utcnow_ts() -> float:
    """Wall-clock pro snímky (monotonic tu nemá smysl — jde o hlášení, ne měření trvání)."""
    return dt.datetime.now(dt.UTC).timestamp()
