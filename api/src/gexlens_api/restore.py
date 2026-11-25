"""Obnova ze zálohy jedním příkazem (F3 #1800, epika #1094, ADR-0063).

    python -m gexlens_api.restore <zdroj> [--overwrite] [--swap-db]

Zdroj je balíček z `full_backup` (tar), nebo adresář se stejným rozložením:
rozbalený balíček, případně `--meta-dir` s `data/` po `restic restore` (F7a
#1804). Kód běží v image `gex-python` na serveru i na PC. Obaly
`scripts/restore.sh` a `scripts/restore.ps1` zastaví zapisující služby
a zdroj připojí do kontejneru.

Kroky jdou po sobě a každý začne až po úspěchu předchozího:
1. **ověření** celého zdroje proti `manifest.json`: formát a verze, bezpečné
   cesty, velikost a sha256 každého souboru. Do té doby se nic nezapisuje,
   takže po změně jediného bajtu zůstane cíl beze změny;
2. **pojistka cíle**: PG s tabulkami nebo soubory v `data/` znamenají cíl
   s daty. Bez `--overwrite` obnova skončí, s ním nejdřív vznikne pojistný
   `pg_dump` do `data/local/pred-obnovou/` (patří stroji, nesynchronizuje se);
3. **PG**: `pg_restore --clean --if-exists --single-transaction`, při selhání
   rollback a DB beze změny. Režim `--swap-db` (synchronizace #1851) obnoví
   do nové DB vedle běžící a pak v jedné transakci přejmenuje. Předchozí DB
   zůstává jako pojistná `<db>_pred_<čas>` a nic ji neodstraní; úklid přijde
   až s dumpem v repu restic (F7a #1804);
4. **data/**: soubor po souboru přes `*.tmp` a `os.replace` s kontrolou sha256.
   Shodné soubory se přeskočí a `data/local/` (stav a nastavení stroje,
   #1847/#1848) se nikdy nepřepisuje. Soubor cíle, který zdroj nemá, se nemaže,
   ale přesune do `data/quarantine/sync-<den>/` (Rozhodnuto 19. 11. v #1800);
5. **výpis**: chybějící tajemství jen jmény, připomínka nastavení IBKR.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from gexlens_api.backup import connection_args, dump_env
from gexlens_api.full_backup import (
    BUILD_SHA_ENV,
    CHUNK_BYTES,
    DATA_PREFIX,
    DUMP_NAME,
    ENV_TEMPLATE_NAME,
    EXCLUDED_DIRS,
    FORMAT_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    RESTORE_NAME,
    RUN_STAMP,
    SECRET_KEYS_NAME,
    TMP_SUFFIX,
    WORK_DIR,
    cleanup_stale_runs,
    dump_to_file,
    ensure_outside_backup,
    scan_data,
)

logger = logging.getLogger(__name__)

META_NAMES = (RESTORE_NAME, ENV_TEMPLATE_NAME, SECRET_KEYS_NAME)
MODES = frozenset({"full", "meta"})
SAFETY_DIR = "local/pred-obnovou"
INSTANCE_SETTINGS = "local/instance_settings.json"
QUARANTINE_DIR = "quarantine"
RUN_PREFIX = "restore-"
STAMP = "%Y%m%d_%H%M%S"
# Manifest PC 25. 11. 2026 (~15 tisíc souborů) má ~3 MB; strop jen proti nesmyslu
MAX_META_BYTES = 64 * 1024 * 1024
MAINTENANCE_DB = "postgres"
# Jedna výměna DB naráz na serveru PG (klíč pg_advisory_lock)
SWAP_LOCK_KEY = 1800
SWAP_ATTEMPTS = 3
OBJECT_IN_USE = "55006"
LIST_LIMIT = 20
PROGRESS_BYTES = 1 << 30


class RestoreError(RuntimeError):
    """Obnova skončila a cíl zůstal beze změny (kromě pojistného dumpu)."""


class PartialRestoreError(RestoreError):
    """PG je obnovená, `data/` jen zčásti: obnovu je potřeba zopakovat (CLI kód 2)."""


# ── Manifest a cesty ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    sha256: str
    live: bool


@dataclass(frozen=True)
class Manifest:
    mode: str
    created_utc: str
    source_host: str
    image_sha: str
    entries: dict[str, Entry]

    def expected(self, name: str) -> Entry | None:
        """Co má soubor obsahovat; None = nelze ověřit (živý soubor v režimu meta, #1799)."""
        entry = self.entries.get(name)
        if entry is None or (self.mode == "meta" and entry.live):
            return None
        return entry


def _excluded(rel: str) -> bool:
    """Totéž vyloučení jako záloha: `ticks/`, `logs/`, `local/`, `backup/tmp/`, `*.tmp`."""
    parts = rel.split("/")
    directories = {"/".join(parts[:index]) for index in range(1, len(parts))}
    return bool(directories & EXCLUDED_DIRS) or rel.endswith(TMP_SUFFIX)


def check_member_name(name: str) -> None:
    """Jen jména, která umí vytvořit `full_backup`; jiné je poškozený nebo cizí zdroj."""
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise RestoreError(f"Nebezpečná cesta ve zdroji: {name!r}")
    if name in META_NAMES or name in (DUMP_NAME, MANIFEST_NAME):
        return
    if not name.startswith(DATA_PREFIX):
        raise RestoreError(f"Neznámý soubor ve zdroji: {name!r}")
    if _excluded(name[len(DATA_PREFIX) :]):
        raise RestoreError(
            f"Zdroj obsahuje {name!r}, který záloha nikdy nebere "
            "(data/local/, logs/, ticks/, backup/tmp/, *.tmp)"
        )


def parse_manifest(body: bytes) -> Manifest:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"{MANIFEST_NAME} není platný JSON") from exc
    if not isinstance(document, dict) or document.get("format") != FORMAT_NAME:
        raise RestoreError(f"{MANIFEST_NAME} není ve formátu {FORMAT_NAME}")
    version = document.get("format_version")
    if version != FORMAT_VERSION:
        raise RestoreError(
            f"Formát zálohy verze {version!r}, tento kód umí jen {FORMAT_VERSION} — "
            "obnov kódem revize zálohy (image_sha v manifestu)"
        )
    mode = document.get("mode")
    if mode not in MODES:
        raise RestoreError(f"Neznámý režim zálohy {mode!r}")
    entries: dict[str, Entry] = {}
    try:
        for item in document["files"]:
            entry = Entry(
                path=str(item["path"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
                live=item.get("live") is True,
            )
            check_member_name(entry.path)
            if entry.path == MANIFEST_NAME or entry.path in entries or len(entry.sha256) != 64:
                raise RestoreError(f"{MANIFEST_NAME}: neplatná položka {entry.path!r}")
            entries[entry.path] = entry
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RestoreError(f"{MANIFEST_NAME}: neplatný seznam souborů") from exc
    for required in (DUMP_NAME, SECRET_KEYS_NAME):
        if required not in entries:
            raise RestoreError(f"{MANIFEST_NAME} nemá {required} — záloha je neúplná")
    return Manifest(
        mode=mode,
        created_utc=str(document.get("created_utc", "?")),
        source_host=str(document.get("source_host", "?")),
        image_sha=str(document.get("image_sha") or "dev"),
        entries=entries,
    )


# ── Ověření zdroje (nic se nezapisuje) ────────────────────────────────


@dataclass(frozen=True)
class Verification:
    manifest: Manifest
    texts: dict[str, bytes]  # RESTORE.md a config/*, pár KB
    warnings: list[str]


class _Progress:
    def __init__(self, label: str) -> None:
        self.label = label
        self.bytes = 0
        self.files = 0
        self._next = PROGRESS_BYTES

    def add(self, size: int) -> None:
        self.bytes += size
        self.files += 1
        if self.bytes >= self._next:
            logger.info("%s: %.1f GB, %d souborů", self.label, self.bytes / 1e9, self.files)
            self._next += PROGRESS_BYTES


def _chunks(handle: IO[bytes]) -> Iterator[bytes]:
    while piece := handle.read(CHUNK_BYTES):
        yield piece


def _file_chunks(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        yield from _chunks(handle)


def _digest(chunks: Iterable[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for piece in chunks:
        digest.update(piece)
        size += len(piece)
    return size, digest.hexdigest()


def compare(
    manifest: Manifest, found: Mapping[str, tuple[int, str]]
) -> tuple[list[str], list[str]]:
    """Nesoulady zdroje s manifestem jako (chyby, upozornění).

    V režimu meta restic zálohuje `data/` až po manifestu (#1799): soubor
    s `live: true` se mezitím smí změnit a soubor navíc v `data/` vznikl
    po manifestu. Obojí je upozornění, všechno ostatní je chyba.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for entry in manifest.entries.values():
        got = found.get(entry.path)
        if got is None:
            problem = "chybí"
        elif got[0] != entry.size:
            problem = f"velikost {got[0]} místo {entry.size}"
        elif got[1] != entry.sha256:
            problem = "sha256 nesouhlasí"
        else:
            continue
        tolerated = manifest.expected(entry.path) is None
        (warnings if tolerated else errors).append(f"{entry.path}: {problem}")
    for name in sorted(found.keys() - manifest.entries.keys()):
        if manifest.mode == "meta" and name.startswith(DATA_PREFIX):
            warnings.append(f"{name}: není v manifestu (vznikl po něm), obnoví se bez ověření")
        else:
            errors.append(f"{name}: není v manifestu")
    return errors, warnings


def _listing(lines: list[str]) -> str:
    shown = "\n".join(f"  - {line}" for line in lines[:LIST_LIMIT])
    if len(lines) > LIST_LIMIT:
        shown += f"\n  … a dalších {len(lines) - LIST_LIMIT}"
    return shown


def _finish_verification(
    manifest: Manifest, found: Mapping[str, tuple[int, str]], texts: dict[str, bytes]
) -> Verification:
    errors, warnings = compare(manifest, found)
    if errors:
        raise RestoreError(
            f"Zdroj nesouhlasí s {MANIFEST_NAME} ({len(errors)}×), cíl zůstal beze změny:\n"
            + _listing(errors)
        )
    return Verification(manifest, texts, warnings)


def _tar_members(path: Path) -> Iterator[tuple[tarfile.TarInfo, IO[bytes]]]:
    """Členy taru v pořadí streamu (balíček ~14 GB se čte jen sekvenčně).

    Data členu čte až volající, proto chyby taru převádí `_tar_errors` kolem
    celé smyčky, ne tady.
    """
    with tarfile.open(path, mode="r|") as archive:
        for info in archive:
            if not info.isreg():
                raise RestoreError(f"{info.name!r} v balíčku není obyčejný soubor")
            handle = archive.extractfile(info)
            if handle is None:
                raise RestoreError(f"{info.name!r} v balíčku nejde přečíst")
            yield info, handle


@contextlib.contextmanager
def _tar_errors(path: Path) -> Iterator[None]:
    """Useknutý nebo poškozený tar (hlavička i data členu) = `RestoreError`."""
    try:
        yield
    except (tarfile.TarError, EOFError) as exc:
        raise RestoreError(
            f"Balíček {path.name} je poškozený nebo neúplný (přerušené stahování?): {exc}"
        ) from exc


def _read_small(handle: IO[bytes], size: int, name: str) -> bytes:
    if size > MAX_META_BYTES:
        raise RestoreError(f"{name} má {size} B — to záloha nevytváří")
    return handle.read()


def verify_tar(path: Path) -> Verification:
    found: dict[str, tuple[int, str]] = {}
    texts: dict[str, bytes] = {}
    manifest_body: bytes | None = None
    progress = _Progress("Ověřeno")
    with _tar_errors(path):
        for info, handle in _tar_members(path):
            if manifest_body is not None:
                raise RestoreError(
                    f"{info.name!r} leží za {MANIFEST_NAME}, ten musí být poslední člen"
                )
            if info.name == MANIFEST_NAME:
                manifest_body = _read_small(handle, info.size, MANIFEST_NAME)
                continue
            _verify_member(info, handle, found, texts, progress)
    if manifest_body is None:
        raise RestoreError(
            f"Balíčku chybí {MANIFEST_NAME} (poslední člen) — je neúplný, stáhni ho znovu"
        )
    return _finish_verification(parse_manifest(manifest_body), found, texts)


def _verify_member(
    info: tarfile.TarInfo,
    handle: IO[bytes],
    found: dict[str, tuple[int, str]],
    texts: dict[str, bytes],
    progress: _Progress,
) -> None:
    name = info.name
    if name.startswith(DATA_PREFIX) and DUMP_NAME not in found:
        # Obnova jde streamem: PG se musí obnovit dřív, než se začne psát data/
        raise RestoreError(f"{name!r} leží v balíčku před {DUMP_NAME}")
    if name in found:
        raise RestoreError(f"{name!r} je v balíčku dvakrát")
    check_member_name(name)
    if name in META_NAMES:
        body = _read_small(handle, info.size, name)
        texts[name] = body
        found[name] = (len(body), hashlib.sha256(body).hexdigest())
        return
    found[name] = _digest(_chunks(handle))
    progress.add(info.size)


def verify_dir(root: Path) -> Verification:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RestoreError(f"V adresáři {root} chybí {MANIFEST_NAME}")
    manifest = parse_manifest(_read_small_file(manifest_path))
    found: dict[str, tuple[int, str]] = {}
    texts: dict[str, bytes] = {}
    for name in (*META_NAMES, DUMP_NAME):
        candidate = root / name
        if not candidate.is_file():
            continue
        if name in META_NAMES:
            body = _read_small_file(candidate)
            texts[name] = body
            found[name] = (len(body), hashlib.sha256(body).hexdigest())
        else:
            found[name] = _digest(_file_chunks(candidate))
    data_root = root / DATA_PREFIX.rstrip("/")
    has_data = any(name.startswith(DATA_PREFIX) for name in manifest.entries)
    if not data_root.is_dir():
        if has_data:
            raise RestoreError(
                f"V adresáři {root} chybí data/ (restic restore data/ ještě neproběhl?)"
            )
    else:
        progress = _Progress("Ověřeno")
        for item in scan_data(data_root, dt.datetime.now(dt.UTC), live_dir=None):
            found[DATA_PREFIX + item.rel] = _digest(_file_chunks(item.source))
            progress.add(item.size)
    return _finish_verification(manifest, found, texts)


def _read_small_file(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return _read_small(handle, os.fstat(handle.fileno()).st_size, path.name)


# ── Cíl ───────────────────────────────────────────────────────────────


def _engine(url: URL | str, *, autocommit: bool = False) -> Engine:
    return create_engine(
        url, poolclass=NullPool, isolation_level="AUTOCOMMIT" if autocommit else "READ COMMITTED"
    )


def pg_has_tables(database_url: str) -> bool:
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relkind IN ('r', 'p') "
                        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "AND n.nspname NOT LIKE 'pg\\_toast%')"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def data_has_files(data_dir: Path) -> bool:
    """Soubor tam, kam obnova zapisuje (vyloučené části jako záloha se nepočítají)."""
    if not data_dir.is_dir():
        return False
    for current, dirs, files in os.walk(data_dir):
        rel_dir = Path(current).relative_to(data_dir).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        dirs[:] = [name for name in dirs if f"{prefix}{name}" not in EXCLUDED_DIRS]
        if any(not name.endswith(TMP_SUFFIX) for name in files):
            return True
    return False


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RestoreError(f"{name} není k dispozici — obnova běží v image gex-python")


def _run(command: list[str], database_url: str) -> None:
    result = subprocess.run(
        command,
        env={**os.environ, **dump_env(database_url)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[-2000:]
        raise RestoreError(f"{command[0]} skončil s kódem {result.returncode}: {detail}")


def check_dump_readable(dump: Path, database_url: str) -> None:
    """Celé čtení dumpu (`--list` čte jen hlavičku a TOC, useknutá data by prošla, #1566)."""
    _run(["pg_restore", f"--file={os.devnull}", str(dump)], database_url)


def safety_dump(database_url: str, data_dir: Path, stamp: str) -> Path:
    """Pojistný dump cíle před přepisem; patří stroji (`data/local/`)."""
    target = data_dir / SAFETY_DIR / f"{stamp}.dump"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + TMP_SUFFIX)
    try:
        asyncio.run(dump_to_file(database_url, part))
        check_dump_readable(part, database_url)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target


def pg_restore(database_url: str, database: str, dump: Path, *, clean: bool) -> None:
    """Celý dump v jedné transakci: chyba = rollback, DB zůstane, jak byla."""
    command = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--single-transaction",
        "--exit-on-error",
        *connection_args(database_url),
        f"--dbname={database}",
    ]
    if clean:
        command += ["--clean", "--if-exists"]
    command.append(str(dump))
    _run(command, database_url)


# ── Výměna DB (#1851) ─────────────────────────────────────────────────


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _database_names(connection: Connection, prefix: str) -> list[str]:
    rows = connection.execute(text("SELECT datname FROM pg_database")).scalars()
    return sorted(str(name) for name in rows if str(name).startswith(prefix))


def swap_names(url: URL, database: str, fresh: str, previous: str) -> None:
    """V jedné transakci: odpojit klienty, `<db>` → pojistná, nová → `<db>`.

    Klient, který se připojí mezi odpojením a přejmenováním, přejmenování
    shodí (object_in_use); transakce se vrátí a zkouší se znovu.
    """
    engine = _engine(url.set(database=MAINTENANCE_DB))
    try:
        for attempt in range(1, SWAP_ATTEMPTS + 1):
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :db AND pid <> pg_backend_pid()"
                        ),
                        {"db": database},
                    )
                    connection.execute(
                        text(f"ALTER DATABASE {_quote(database)} RENAME TO {_quote(previous)}")
                    )
                    connection.execute(
                        text(f"ALTER DATABASE {_quote(fresh)} RENAME TO {_quote(database)}")
                    )
                return
            except DBAPIError as exc:
                in_use = getattr(exc.orig, "sqlstate", None) == OBJECT_IN_USE
                if not in_use or attempt == SWAP_ATTEMPTS:
                    raise RestoreError(
                        f"Výměna DB selhala, {database} zůstala beze změny: {exc.orig}"
                    ) from exc
                logger.warning("DB %s má nové spojení, výměna znovu (%d)", database, attempt)
                time.sleep(1)
    finally:
        engine.dispose()


@dataclass(frozen=True)
class SwapResult:
    previous: str  # pojistná DB z této výměny
    kept: list[str]  # všechny pojistné DB cíle (nic se neodstraňuje)


def swap_database(database_url: str, dump: Path, stamp: str) -> SwapResult:
    """Obnova do `<db>_sync_<čas>` a přejmenování; přerušení před ním = cíl beze změny.

    Pojistné `<db>_pred_*` se neodstraňují: smazat je smí až úklid, který je
    předtím uloží jako ověřený dump do repa restic (F7a #1804).
    """
    url = make_url(database_url)
    database = url.database
    if not database:
        raise RestoreError("database_url neobsahuje jméno databáze")
    fresh = f"{database}_sync_{stamp}"
    previous = f"{database}_pred_{stamp}"
    admin = _engine(url.set(database=MAINTENANCE_DB), autocommit=True)
    try:
        with admin.connect() as connection:
            locked = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": SWAP_LOCK_KEY}
            ).scalar_one()
            if not locked:
                raise RestoreError("Jiná výměna DB právě běží")
            # Nedokončené `_sync_` DB přerušených běhů jsou jen polotovar obnovy
            for stale in _database_names(connection, f"{database}_sync_"):
                logger.warning("Mažu nedokončenou DB přerušené obnovy %s", stale)
                connection.execute(text(f"DROP DATABASE IF EXISTS {_quote(stale)} WITH (FORCE)"))
            connection.execute(text(f"CREATE DATABASE {_quote(fresh)} TEMPLATE template0"))
            swapped = False
            try:
                pg_restore(database_url, fresh, dump, clean=False)
                swap_names(url, database, fresh, previous)
                swapped = True
            finally:
                if not swapped:
                    connection.execute(
                        text(f"DROP DATABASE IF EXISTS {_quote(fresh)} WITH (FORCE)")
                    )
            kept = _database_names(connection, f"{database}_pred_")
    finally:
        admin.dispose()
    return SwapResult(previous, kept)


# ── Zápis souborů ─────────────────────────────────────────────────────


def _write_verified(
    target: Path, chunks: Iterable[bytes], mtime_ns: int, expected: Entry | None
) -> int:
    """Zápis přes `*.tmp` + `os.replace`; obsah jiný než v manifestu = chyba, cíl beze změny."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.restore{TMP_SUFFIX}")
    digest = hashlib.sha256()
    written = 0
    try:
        with open(part, "wb") as out:
            for piece in chunks:
                digest.update(piece)
                out.write(piece)
                written += len(piece)
        if expected is not None and (
            written != expected.size or digest.hexdigest() != expected.sha256
        ):
            raise RestoreError(
                f"{expected.path} se při zápisu liší od manifestu (zdroj se změnil po ověření) "
                "— obnova přerušena, spusť ji znovu"
            )
        os.utime(part, ns=(mtime_ns, mtime_ns))
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return written


def _same_content(target: Path, expected: Entry) -> bool:
    try:
        if target.stat().st_size != expected.size:
            return False
    except FileNotFoundError:
        return False
    return _digest(_file_chunks(target))[1] == expected.sha256


# ── Průběh obnovy ─────────────────────────────────────────────────────


@dataclass
class Report:
    source_kind: str  # balíček | adresář
    manifest: Manifest | None = None
    warnings: list[str] = field(default_factory=list)
    safety_dump: Path | None = None
    swap: SwapResult | None = None
    written: int = 0
    skipped: int = 0
    quarantined: list[str] = field(default_factory=list)  # relativně k data/
    quarantine_dir: str = ""
    missing_secrets: list[str] = field(default_factory=list)
    instance_settings_missing: bool = False
    seconds: dict[str, float] = field(default_factory=dict)


def quarantine_missing(
    data_dir: Path, source_files: set[str], now: dt.datetime
) -> tuple[str, list[str]]:
    """Soubory cíle, které ve snímku zdroje chybí, přesune do `quarantine/sync-<den>/`.

    Rozhodnuto 19. 11. v #1800 (ADR-0063 bod 4, varianta A): nemazat (data se
    nemažou) ani nenechat na místě (podezřelá partice, kterou zdroj přesunul
    do karantény, by se vrátila do UI). Už přesunuté soubory (`quarantine/`)
    a části, které záloha nebere, se nepočítají. Synchronizace (#1851) to volá
    po `restic restore`. Vrací adresář karantény a přesunuté cesty k `data/`.
    """
    utc = now.astimezone(dt.UTC)
    folder = f"{QUARANTINE_DIR}/sync-{utc:%Y-%m-%d}"
    moved: list[str] = []
    for item in scan_data(data_dir, utc, live_dir=None):
        if item.rel.startswith(f"{QUARANTINE_DIR}/") or item.rel in source_files:
            continue
        destination = data_dir / folder / item.rel
        if destination.exists():  # druhá obnova téhož dne
            destination = destination.with_name(f"{destination.name}.{utc:%H%M%S}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(item.source, destination)
        moved.append(item.rel)
    return folder, moved


def missing_secrets(secret_keys_text: bytes, environ: Mapping[str, str]) -> list[str]:
    """Jména tajemství zdroje, která cíli chybí (hodnoty se nikdy nečtou ani nevypisují)."""
    names = [line.strip() for line in secret_keys_text.decode("utf-8").splitlines()]
    return [
        name
        for name in names
        if name and not name.startswith("#") and not environ.get(name, "").strip()
    ]


def image_warning(backup_sha: str, environ: Mapping[str, str]) -> str | None:
    running = environ.get(BUILD_SHA_ENV, "").strip() or "dev"
    if running == backup_sha:
        return None
    if "dev" in (running, backup_sha):
        return f"Revize image ({running}) a zálohy ({backup_sha}) nejde porovnat (lokální build)"
    return (
        f"Image běží z revize {running[:7]}, záloha je z {backup_sha[:7]}. Je-li image starší "
        f"než záloha, nastav v .env GEXLENS_IMAGE_TAG=sha-{backup_sha[:7]} a stack "
        "nastartuj až s ním (starší kód nemusí znát novější schéma)"
    )


class _Apply:
    """Zápis do cíle: PG jednou, pak soubory `data/`."""

    def __init__(
        self,
        *,
        database_url: str,
        data_dir: Path,
        work_dir: Path,
        stamp: str,
        swap_db: bool,
        report: Report,
    ) -> None:
        self.database_url = database_url
        self.data_dir = data_dir
        self.work_dir = work_dir
        self.stamp = stamp
        self.swap_db = swap_db
        self.report = report
        self.pg_done = False
        self.restored: set[str] = set()  # cesty zdroje relativně k data/ (i přeskočené)
        self._data_started: float | None = None

    def restore_pg(self, dump: Path) -> None:
        started = time.monotonic()
        if self.swap_db:
            self.report.swap = swap_database(self.database_url, dump, self.stamp)
        else:
            database = make_url(self.database_url).database
            if not database:
                raise RestoreError("database_url neobsahuje jméno databáze")
            pg_restore(self.database_url, database, dump, clean=True)
        self.report.seconds["pg_restore"] = time.monotonic() - started
        logger.info("PG obnovena za %.0f s", self.report.seconds["pg_restore"])
        self.pg_done = True

    def write_data(
        self, rel: str, chunks: Iterable[bytes], mtime_ns: int, expected: Entry | None
    ) -> None:
        if self._data_started is None:
            self._data_started = time.monotonic()
        if _excluded(rel):  # pojistka i pro adresář: data/local/ se nikdy nepřepíše
            raise RestoreError(f"{DATA_PREFIX}{rel} do obnovy nepatří")
        target = self.data_dir / rel
        self.restored.add(rel)
        if expected is not None and _same_content(target, expected):
            self.report.skipped += 1
            return
        _write_verified(target, chunks, mtime_ns, expected)
        self.report.written += 1

    def finish(self) -> None:
        if not self.pg_done:
            raise RestoreError(f"Zdroj nemá {DUMP_NAME}")  # po ověření nenastane
        if self._data_started is not None:
            self.report.seconds["data"] = time.monotonic() - self._data_started


def _apply_tar(path: Path, verification: Verification, apply: _Apply) -> None:
    manifest = verification.manifest
    with _tar_errors(path):
        for info, handle in _tar_members(path):
            name = info.name
            if name == MANIFEST_NAME or name in META_NAMES:
                continue
            if name not in manifest.entries:
                raise RestoreError(f"{name!r} v balíčku přibyl po ověření")
            mtime_ns = int(info.mtime) * 1_000_000_000
            if name == DUMP_NAME:
                dump = apply.work_dir / Path(DUMP_NAME).name
                _write_verified(dump, _chunks(handle), mtime_ns, manifest.entries[name])
                apply.restore_pg(dump)
                continue
            rel = name[len(DATA_PREFIX) :]
            apply.write_data(rel, _chunks(handle), mtime_ns, manifest.expected(name))


def _apply_dir(root: Path, verification: Verification, apply: _Apply) -> None:
    manifest = verification.manifest
    apply.restore_pg(root / DUMP_NAME)
    data_root = root / DATA_PREFIX.rstrip("/")
    if not data_root.is_dir():
        return
    for item in scan_data(data_root, dt.datetime.now(dt.UTC), live_dir=None):
        name = DATA_PREFIX + item.rel
        if name not in manifest.entries and manifest.mode != "meta":
            raise RestoreError(f"{name!r} v adresáři přibyl po ověření")
        apply.write_data(
            item.rel,
            _file_chunks(item.source),
            item.source.stat().st_mtime_ns,
            manifest.expected(name),
        )


def run_restore(
    source: Path,
    *,
    database_url: str,
    data_dir: Path,
    environ: Mapping[str, str],
    overwrite: bool = False,
    swap_db: bool = False,
    now: dt.datetime | None = None,
    on_verified: Callable[[Report], None] | None = None,
) -> Report:
    """Celá obnova; `on_verified` dostane report po ověření (výpis upozornění hned)."""
    if not source.exists():
        raise RestoreError(f"Zdroj {source} neexistuje")
    try:
        ensure_outside_backup(source, data_dir)
    except ValueError as exc:
        raise RestoreError(
            f"Zdroj {source} leží v data/ cíle — přesuň ho do data/local/ nebo mimo data/"
        ) from exc
    for tool in ("pg_restore", "pg_dump"):
        _require_tool(tool)
    is_tar = source.is_file()
    report = Report("balíček" if is_tar else "adresář")
    started_at = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    stamp = started_at.strftime(STAMP)

    # Pojistka cíle je jen čtení — chybějící --overwrite se ozve před ověřením ~14 GB
    has_tables = pg_has_tables(database_url)
    has_files = data_has_files(data_dir)
    if (has_tables or has_files) and not overwrite:
        what = " a ".join(
            part
            for part, present in (("PG má tabulky", has_tables), ("data/ má soubory", has_files))
            if present
        )
        raise RestoreError(
            f"Cíl už má data ({what}) a obnova by je přepsala: PG celou, v data/ změněné "
            "soubory bez kopie a soubory, které zdroj nemá, přesune do "
            "data/quarantine/sync-<den>/. Spusť ji s --overwrite (v restore.ps1 -Overwrite): "
            "před obnovou PG vznikne "
            f"pojistný dump v data/{SAFETY_DIR}/."
        )

    started = time.monotonic()
    verification = verify_tar(source) if is_tar else verify_dir(source)
    report.seconds["ověření"] = time.monotonic() - started
    report.manifest = verification.manifest
    report.warnings += verification.warnings
    warning = image_warning(verification.manifest.image_sha, environ)
    if warning is not None:
        report.warnings.insert(0, warning)
    if on_verified is not None:
        on_verified(report)

    # Od tohoto bodu se zapisuje
    if has_tables and not swap_db:
        started = time.monotonic()
        report.safety_dump = safety_dump(database_url, data_dir, stamp)
        report.seconds["pojistný dump"] = time.monotonic() - started
    # Pracovní adresáře zabitých obnov (dump ~230 MB) uklidí další běh
    cleanup_stale_runs(data_dir, started_at, prefix=RUN_PREFIX)
    work_dir = data_dir / WORK_DIR / f"{RUN_PREFIX}{started_at:{RUN_STAMP}}-{os.getpid()}"
    work_dir.mkdir(parents=True)
    try:
        apply = _Apply(
            database_url=database_url,
            data_dir=data_dir,
            work_dir=work_dir,
            stamp=stamp,
            swap_db=swap_db,
            report=report,
        )
        try:
            if is_tar:
                _apply_tar(source, verification, apply)
            else:
                _apply_dir(source, verification, apply)
            apply.finish()
            if has_files:  # do prázdného cíle nemůže přebývat nic
                source_files = apply.restored | {
                    name[len(DATA_PREFIX) :]
                    for name in verification.manifest.entries
                    if name.startswith(DATA_PREFIX)
                }
                report.quarantine_dir, report.quarantined = quarantine_missing(
                    data_dir, source_files, started_at
                )
        except Exception as exc:
            if not apply.pg_done:
                raise
            raise PartialRestoreError(
                f"{exc} — PG už je obnovená, data/ jen zčásti: spusť obnovu znovu "
                "s --overwrite (shodné soubory se přeskočí)"
            ) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    report.missing_secrets = missing_secrets(verification.texts.get(SECRET_KEYS_NAME, b""), environ)
    report.instance_settings_missing = not (data_dir / INSTANCE_SETTINGS).is_file()
    return report


# ── Výpis a CLI ───────────────────────────────────────────────────────


def render_warnings(report: Report) -> str:
    if not report.warnings:
        return ""
    return f"Upozornění ({len(report.warnings)}):\n" + _listing(report.warnings)


def render_report(report: Report) -> str:
    manifest = report.manifest
    assert manifest is not None
    lines = [
        f"Obnova hotová: {report.source_kind}, režim {manifest.mode}, záloha "
        f"{manifest.created_utc} ze stroje {manifest.source_host}, "
        f"revize {manifest.image_sha[:7]}.",
        "- doby: " + ", ".join(f"{name} {value:.0f} s" for name, value in report.seconds.items()),
    ]
    if report.swap is not None:
        lines.append(
            f"- PG: výměna DB, předchozí je teď {report.swap.previous}. Pojistné DB "
            f"({len(report.swap.kept)}): {', '.join(report.swap.kept)}; nic se neodstraňuje, "
            "úklid přijde s repem restic (F7a #1804)"
        )
    else:
        safety = f", pojistný dump {report.safety_dump}" if report.safety_dump else ""
        lines.append(f"- PG: obnovena celá DB{safety}")
    lines.append(
        f"- data/: zapsáno {report.written} souborů, {report.skipped} shodných přeskočeno, "
        "data/local/ beze změny"
    )
    if report.quarantined:
        lines.append(
            f"- karanténa: {len(report.quarantined)} souborů cíle, které zdroj nemá, přesunuto "
            f"do data/{report.quarantine_dir}/ (nic se nesmazalo):\n" + _listing(report.quarantined)
        )
    if report.missing_secrets:
        lines.append(
            "Tajemství k doplnění do .env ze správce hesel: " + ", ".join(report.missing_secrets)
        )
    if report.instance_settings_missing:
        lines.append(
            f"Chybí data/{INSTANCE_SETTINGS}: host a port IBKR se teď berou z obnovené DB "
            "zdroje (past #446/#992, #1847/#1848) — zkontroluj je v Settings."
        )
    return "\n".join(lines)


def _print_warnings(report: Report) -> None:
    text_block = render_warnings(report)
    if text_block:
        print(text_block, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gexlens_api.restore",
        description=(
            "Obnova GEXLens ze zálohy (#1800): ověření manifestu, PG a data/ "
            "bez mazání; soubory, které zdroj nemá, jdou do data/quarantine/sync-<den>/; "
            "data/local/ se nepřepisuje."
        ),
    )
    parser.add_argument(
        "source", help="balíček .tar z úplné zálohy, nebo adresář se stejným rozložením"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=f"cíl s daty přepsat; předtím pojistný pg_dump do data/{SAFETY_DIR}/",
    )
    parser.add_argument(
        "--swap-db",
        action="store_true",
        help=(
            "výměna DB (synchronizace #1851): obnova do nové DB vedle běžící a přejmenování; "
            "předchozí zůstane jako <db>_pred_<čas>"
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from gexlens_engine.config import load_settings

    try:
        settings = load_settings()
        report = run_restore(
            Path(args.source),
            database_url=settings.database_url,
            data_dir=Path(settings.data_dir),
            environ=os.environ,
            overwrite=args.overwrite,
            swap_db=args.swap_db,
            on_verified=_print_warnings,
        )
    except PartialRestoreError as exc:
        print(f"Obnova nedokončena: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError, OSError, SQLAlchemyError) as exc:
        print(f"Obnova selhala, cíl zůstal beze změny: {exc}", file=sys.stderr)
        return 1
    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
