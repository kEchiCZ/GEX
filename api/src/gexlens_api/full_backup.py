"""Úplná záloha jedním balíčkem (F2 #1799, epika #1094, ADR-0063).

Balíček je nekomprimovaný tar (parquety i `pg_dump -Fc` jsou už komprimované
a `tar.exe` ve Windows ho otevře bez dalších nástrojů):

    RESTORE.md                návod k obnově (šablona `restore_template.md`)
    config/env.template       klíče `.env` s hodnotami, tajemství vyprázdněná
    config/secret-keys.txt    jen jména tajemství k doplnění ze správce hesel
    postgres/gexlens.dump     `pg_dump -Fc` celé databáze
    data/…                    datový adresář bez `ticks/`, `logs/`, `local/`,
                              `backup/tmp/` a rozpracovaných `*.tmp`
    manifest.json             POSLEDNÍ člen: velikost a sha256 každého souboru

Manifest jde na konec, protože sha256 se počítá během streamu — data (~13 GB
na PC 25. 11. 2026) se tak čtou jen jednou. Hash má pevnou délku, takže
velikost manifestu, a tím `Content-Length` celého balíčku, je známá předem.

Konzistence za otevřeného trhu: engine dnešní partice každou minutu přepisuje
(`os.replace`, `parquet_store._PartitionBuffer._write`). Soubory změněné
za posledních 24 h se proto na začátku zkopírují do `backup/tmp/full-*/live/`
a do taru jdou z kopie (velikost člena sedí, manifest je označí `live`).
Starší soubor, který se přesto změní během streamu, je explicitní chyba:
balíček s jiným obsahem, než slibuje manifest, by obnova odhalila až
v nejhorší chvíli.

Režim `--meta-dir` (noční job F7a, snímek synchronizace #1851) zapíše jen
dump, konfiguraci, RESTORE.md a manifest; `data/` si vezme restic přímo.
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
import re
import shutil
import socket
import sys
import tarfile
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from starlette.types import Receive, Scope, Send

from gexlens_api.backup import dump_command, dump_env
from gexlens_api.security import TOKEN_HEADER, OneTimeTickets
from gexlens_engine.compute.marketclock import is_market_closed

logger = logging.getLogger(__name__)

FORMAT_NAME = "gexlens-zaloha"
FORMAT_VERSION = 1
TAR_MEDIA_TYPE = "application/x-tar"
CHUNK_BYTES = 1024 * 1024
# Okno „živých" souborů: dnešní partice, průběžně doplňované JSONL a stav jobů.
# 25. 11. 2026 na PC 296 souborů / 200 MB, kopie trvá jednotky sekund.
LIVE_WINDOW = dt.timedelta(hours=24)
WORK_DIR = PurePosixPath("backup/tmp")
RUN_PREFIX = "full-"
RUN_STAMP = "%Y%m%dT%H%M%SZ"
STALE_RUN_AGE = dt.timedelta(hours=24)
# Relativně k data/: dopočitatelné tisky (ADR-0029), logy, pracovní adresář
# zálohy a stav stroje (#1847/#1848 — patří stroji, ne datům)
EXCLUDED_DIRS = frozenset({"ticks", "logs", "local", str(WORK_DIR)})
TMP_SUFFIX = ".tmp"
SHA_PLACEHOLDER = "0" * 64
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ENV_PREFIXES = ("GEXLENS_", "IBKR_")
BUILD_SHA_ENV = "GEXLENS_BUILD_SHA"
# Proměnné, které nastavuje image, ne `.env` — do šablony nepatří
IMAGE_KEYS = frozenset({BUILD_SHA_ENV})

RESTORE_NAME = "RESTORE.md"
ENV_TEMPLATE_NAME = "config/env.template"
SECRET_KEYS_NAME = "config/secret-keys.txt"
DUMP_NAME = "postgres/gexlens.dump"
MANIFEST_NAME = "manifest.json"
DATA_PREFIX = "data/"
TAR_BLOCK = 512
TAR_END = b"\0" * (2 * TAR_BLOCK)


class KeyKind(StrEnum):
    """Zařazení klíče `.env` pro šablonu konfigurace v balíčku."""

    SECRET = "secret"  # hodnota se do balíčku nikdy nedostane
    CONFIG = "config"  # hodnota se přenáší
    COMPOSE = "compose"  # nastavuje compose; v šabloně jen jako komentář


_S, _C, _K = KeyKind.SECRET, KeyKind.CONFIG, KeyKind.COMPOSE

# Jediný zdroj klasifikace: každý klíč z `.env.example` (i zakomentovaný)
# v jeho pořadí. Test shodí nový klíč bez zařazení i zařazení klíče, který
# z `.env.example` zmizel. Klíč v prostředí, který tu není, se chová jako
# tajemství — neznámá hodnota se do balíčku nedostane.
ENV_KEY_KINDS: dict[str, KeyKind] = {
    "GEXLENS_IBKR_HOST": _K,  # compose: host.docker.internal / ib-gateway
    "GEXLENS_IBKR_PORT": _C,
    "GEXLENS_IBKR_CLIENT_ID": _C,
    "GEXLENS_MARKET_DATA_TYPE": _C,
    "GEXLENS_CONNECT_TIMEOUT_S": _C,
    "GEXLENS_RECONNECT_BACKOFF_BASE_S": _C,
    "GEXLENS_RECONNECT_BACKOFF_MAX_S": _C,
    "GEXLENS_SYMBOLS": _C,
    "GEXLENS_MAX_INSTRUMENTS": _C,
    "GEXLENS_WATCHLIST_POLL_CYCLES": _C,
    "GEXLENS_OI_ARCHIVE_EXPIRIES": _C,
    "GEXLENS_SWEEP_NEXT_EXPIRY": _C,
    "GEXLENS_NEXT_EXPIRY_SWEEP_EVERY": _C,
    "GEXLENS_QUOTE_MAX_AGE_S": _C,
    "GEXLENS_GREEKS_STALL_SHARE": _C,
    "GEXLENS_GREEKS_STALL_CYCLES": _C,
    "GEXLENS_SETUPS_ENABLED": _C,
    "GEXLENS_SETUP_MIN_WALL_DOMINANCE": _C,
    "GEXLENS_SETUP_COUNTER_FLOW_LOOKBACK": _C,
    "GEXLENS_SETUP_COUNTER_STOP_COOLDOWN_MINUTES": _C,
    "GEXLENS_SETUP_DISABLED_TEMPLATES": _C,
    "GEXLENS_SETUP_MIN_RISK_ATR": _C,
    "GEXLENS_SETUP_MAX_RR": _C,
    "GEXLENS_SETUP_MAX_STOPS_PER_DIRECTION": _C,
    "GEXLENS_SETUP_DIRECTION_BLOCK_MINUTES": _C,
    "GEXLENS_SETUP_SELFCHECK_DAYS": _C,
    "GEXLENS_SETUP_SELFCHECK_MIN_SAMPLES": _C,
    "GEXLENS_SETUP_SELFCHECK_MAX_DRAWDOWN_R": _C,
    "GEXLENS_FLOW_OI_ALPHA": _C,
    "GEXLENS_VOL_LEADER_RATIO": _C,
    "GEXLENS_VOL_LEADER_MIN_VOLUME": _C,
    "GEXLENS_LADDER_TOP_N": _C,
    "GEXLENS_LADDER_MIN_SHARE": _C,
    "GEXLENS_STRIKE_RANGE_POINTS": _C,
    "GEXLENS_STRIKE_RANGE_EXPAND_THRESHOLD": _C,
    "GEXLENS_STRIKE_RANGE_MAX_POINTS": _C,
    "GEXLENS_BATCH_SIZE": _C,
    "GEXLENS_BATCH_TIMEOUT_S": _C,
    "GEXLENS_BS_FALLBACK_RECONNECT": _C,
    "GEXLENS_OI_PUBLICATION_TIME_LOCAL": _C,
    "GEXLENS_OI_PUBLICATION_TZ": _C,
    "GEXLENS_OI_PUBLICATION_HOUR_UTC": _C,
    "GEXLENS_OI_FINAL_MIN_COVERAGE": _C,
    "GEXLENS_WINGS_SWEEP_EVERY": _C,
    "GEXLENS_ATM_SWEEP_WIDTH": _C,
    "GEXLENS_REPAIR_MAX_ATTEMPTS": _C,
    "GEXLENS_REPAIR_BACKOFF_BASE_S": _C,
    "GEXLENS_REPAIR_BACKOFF_MAX_S": _C,
    "GEXLENS_REPAIR_STALL_ROUNDS": _C,
    "GEXLENS_GREEKS_FALLBACK_SWEEPS": _C,
    "GEXLENS_MARKET_DATA_LINES": _C,
    "GEXLENS_BARS_STALL_ALERT_MINUTES": _C,
    "GEXLENS_PG_PASSWORD": _S,
    "GEXLENS_API_TOKEN": _S,
    "GEXLENS_BIND_ADDR": _C,
    "IBKR_DATA_USERID": _S,  # username IBKR — do správce hesel k heslu Gateway
    "IBKR_TRADING_MODE": _C,
    "IBKR_GATEWAY_TZ": _C,
    "GEXLENS_DATABASE_URL": _K,  # nese heslo PG
    "GEXLENS_DATA_DIR": _K,
    "GEXLENS_RETENTION_DAYS": _C,
    "GEXLENS_BARS_BACKFILL_DAYS": _C,
    "GEXLENS_KEEP_BARS_FOREVER": _C,
    "GEXLENS_DISK_LIMIT_GB": _C,
    "GEXLENS_NEWS_DATABASE_URL": _K,  # nese heslo PG
    "GEXLENS_NEWS_FINNHUB_API_KEY": _S,
    "GEXLENS_NEWS_FRED_API_KEY": _S,
    "GEXLENS_NEWS_GEMINI_API_KEY": _S,
    "GEXLENS_NEWS_LLM_ENABLED": _C,
    "GEXLENS_NEWS_EXPLAIN_ENABLED": _C,
    "GEXLENS_SCENARIO_AUTO_MINUTES_BEFORE_OPEN": _C,
    "GEXLENS_PUSH_TELEGRAM_TOKEN": _S,
    "GEXLENS_PUSH_TELEGRAM_CHAT_ID": _S,  # osobní identifikátor
    "GEXLENS_PUSH_QUIET_HOURS": _C,
    "GEXLENS_PUSH_DAILY_CAP": _C,
    "GEXLENS_PUSH_SETUP_MIN_CONFIDENCE": _C,
    "GEXLENS_NEWS_EXPLAIN_MODEL": _C,
    "GEXLENS_NEWS_EXPLAIN_DAILY_TOKENS": _C,
    "GEXLENS_NEWS_NGRAM_SHADOW_ENABLED": _C,
    "GEXLENS_NEWS_GEMINI_MODEL": _C,
    "GEXLENS_NEWS_LLM_INTERVAL_S": _C,
    "GEXLENS_NEWS_LLM_DAILY_LIMIT": _C,
    "GEXLENS_NEWS_LLM_BATCH_LIMIT": _C,
    "GEXLENS_NEWS_REDDIT_CLIENT_ID": _S,
    "GEXLENS_NEWS_REDDIT_CLIENT_SECRET": _S,
    "GEXLENS_NEWS_UA_CONTACT": _S,  # jméno a e-mail — osobní údaj
    "GEXLENS_NEWS_FF_ACTUAL_REFRESH_S": _C,
    "GEXLENS_NEWS_FF_BACKFILL_WEEKS": _C,
    "GEXLENS_NEWS_SIGNAL_SYMBOLS": _C,
    "GEXLENS_NEWS_PCR_INTERVAL_S": _C,
    "GEXLENS_NEWS_PCR_SYMBOLS": _C,
    "GEXLENS_NEWS_DEDUP_WINDOW_MINUTES": _C,
    "GEXLENS_NEWS_DATA_DIR": _K,
    "GEXLENS_NEWS_REACTION_INTERVAL_S": _C,
    "GEXLENS_NEWS_API_BASE": _K,
    "GEXLENS_NEWS_RETRO_PASS_HOUR": _C,
    "GEXLENS_NEWS_RETRO_PASS_MINUTE": _C,
    "GEXLENS_IBKR_NEWS_ENABLED": _C,
    "GEXLENS_NEWS_ALPACA_KEY_ID": _S,
    "GEXLENS_NEWS_ALPACA_SECRET": _S,
    "GEXLENS_TASTY_ENABLED": _C,
    "GEXLENS_TASTY_COMPARISON_WRITE": _C,
    "GEXLENS_TASTY_SHADOW": _C,
    "GEXLENS_TASTY_CLIENT_SECRET": _S,
    "GEXLENS_TASTY_REFRESH_TOKEN": _S,
    "GEXLENS_TASTY_OI_FILL": _C,
    "GEXLENS_TASTY_MAX_SUBSCRIPTIONS": _C,
    "GEXLENS_TASTY_MAX_ENTRIES": _C,
    "GEXLENS_TASTY_ADHOC_RESERVE_ENTRIES": _C,
    "GEXLENS_TASTY_SPOT_FALLBACK": _C,
    "GEXLENS_TASTY_SPOT_STALE_AFTER_S": _C,
    "GEXLENS_TASTY_SPOT_RECOVER_AFTER_S": _C,
    "GEXLENS_TASTY_SPOT_MAX_AGE_S": _C,
    "GEXLENS_TASTY_CHAIN_FALLBACK": _C,
    "GEXLENS_TASTY_CHAIN_RECOVER_MINUTES": _C,
    "GEXLENS_TASTY_CHAIN_MAX_AGE_S": _C,
    "GEXLENS_STARTUP_CONNECT_WAIT_S": _C,
    "GEXLENS_CROSSCHECK_ENABLED": _C,
    "GEXLENS_CROSSCHECK_SHARE_THRESHOLD": _C,
    "GEXLENS_CROSSCHECK_MINUTES": _C,
    "GEXLENS_CROSSCHECK_COOLDOWN_MINUTES": _C,
    "GEXLENS_CROSSCHECK_CHANGE_THRESHOLD": _C,
    "GEXLENS_PROBE_ENABLED": _C,
    "GEXLENS_DISK_FREE_WARN_GB": _C,
    "GEXLENS_DISK_FREE_CRIT_GB": _C,
    "GEXLENS_DB_SIZE_ALERT_GB": _C,
    "GEXLENS_FRONT_ROLL_DAYS": _C,
    "GEXLENS_MEMORY_TRACE": _C,
    "GEXLENS_MEMORY_TRACE_FRAMES": _C,
    "GEXLENS_IMAGE_TAG": _C,
    "GEXLENS_ENGINE_TAG": _C,
    "GEXLENS_API_TAG": _C,
    "GEXLENS_NEWS_TAG": _C,
    "GEXLENS_FRONTEND_TAG": _C,
}


class BackupCancelled(Exception):
    """Klient se odpojil během přípravy — záloha se ukončí a uklidí."""


class PackageChangedError(RuntimeError):
    """Soubor mimo kopii živých souborů se změnil během streamu."""


# ── Konfigurace bez tajemství ─────────────────────────────────────────


def classify(key: str) -> KeyKind:
    """Zařazení klíče; neznámý klíč je tajemství (hodnota se nepřenáší)."""
    return ENV_KEY_KINDS.get(key, KeyKind.SECRET)


def _env_keys(environ: Mapping[str, str]) -> list[str]:
    """Klíče aplikace v prostředí: pořadí `.env.example`, pak neznámé abecedně."""
    present = {key for key in environ if key.startswith(ENV_PREFIXES) and key not in IMAGE_KEYS}
    known = [key for key in ENV_KEY_KINDS if key in present]
    return known + sorted(present - set(ENV_KEY_KINDS))


_SAFE_VALUE = re.compile(r"[A-Za-z0-9_./:,@+=*%-]*")


def _env_value(value: str) -> str:
    """Hodnota pro `.env`: holá jen bezpečná, jinak v uvozovkách (lessons-learned 9. 10.)."""
    if _SAFE_VALUE.fullmatch(value):
        return value
    if "'" not in value:
        return f"'{value}'"  # jednoduché uvozovky compose bere doslova
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def secret_keys(environ: Mapping[str, str]) -> list[str]:
    """Jména tajemství nastavených na zdroji — ta je při obnově potřeba doplnit."""
    return [
        key
        for key in _env_keys(environ)
        if classify(key) is KeyKind.SECRET and environ[key].strip()
    ]


def render_env_template(environ: Mapping[str, str], created_utc: str) -> str:
    lines = [
        f"# GEXLens — konfigurace ze zálohy {created_utc} (formát {FORMAT_NAME} v{FORMAT_VERSION})",
        "# Tajemství jsou vyprázdněná: doplň je ze správce hesel (config/secret-keys.txt).",
        "# Klíče, které nastavuje compose, jsou jen jako komentář.",
        "",
    ]
    for key in _env_keys(environ):
        kind = classify(key)
        if kind is KeyKind.CONFIG:
            lines.append(f"{key}={_env_value(environ[key])}")
        elif kind is KeyKind.COMPOSE:
            lines.append(f"# {key} — nastavuje compose, hodnota se nepřenáší")
        elif key in ENV_KEY_KINDS:
            lines.append(f"{key}=")
        else:
            lines.append("# neklasifikovaný klíč — hodnota vynechána jako tajemství")
            lines.append(f"{key}=")
    return "\n".join(lines) + "\n"


def render_secret_keys(environ: Mapping[str, str]) -> str:
    lines = [
        "# Tajemství nastavená na zdrojovém stroji — hodnoty nejsou v balíčku.",
        "# Doplň je ze správce hesel do .env (heslo IB Gateway do secrets/ibkr_password).",
    ]
    lines += secret_keys(environ)
    return "\n".join(lines) + "\n"


# ── Prohledání data/ ──────────────────────────────────────────────────


@dataclass(frozen=True)
class DataFile:
    rel: str  # POSIX cesta relativně k data/
    source: Path  # odkud se čte: originál, nebo kopie v live/
    size: int
    mtime_ns: int  # originálu
    live: bool


def scan_data(
    data_dir: Path,
    now: dt.datetime,
    live_dir: Path | None,
    cancel: threading.Event | None = None,
) -> list[DataFile]:
    """Soubory data/ k zálohování; živé zkopíruje do `live_dir` (None = nekopírovat).

    Soubor, který mezi výpisem adresáře a čtením zmizí, se přeskočí — v balíčku
    nemá být, co už na disku není.
    """
    cutoff_ns = int((now - LIVE_WINDOW).timestamp() * 1_000_000_000)
    found: list[DataFile] = []
    pending: list[tuple[str, Path]] = [("", data_dir)]
    while pending:
        rel_dir, directory = pending.pop()
        with os.scandir(directory) as entries:
            listing = list(entries)
        for entry in listing:
            if cancel is not None and cancel.is_set():
                raise BackupCancelled
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir(follow_symlinks=False):
                if rel not in EXCLUDED_DIRS:
                    pending.append((rel, Path(entry.path)))
                continue
            if not entry.is_file(follow_symlinks=False) or entry.name.endswith(TMP_SUFFIX):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                logger.info("Soubor %s zmizel během přípravy zálohy — vynechán", rel)
                continue
            live = stat.st_mtime_ns >= cutoff_ns
            source = Path(entry.path)
            size = stat.st_size
            if live and live_dir is not None:
                source = live_dir / rel
                if not _copy_live(Path(entry.path), source):
                    logger.info("Soubor %s zmizel během přípravy zálohy — vynechán", rel)
                    continue
                size = source.stat().st_size
            found.append(DataFile(rel, source, size, stat.st_mtime_ns, live))
    found.sort(key=lambda item: item.rel)
    return found


def _copy_live(source: Path, target: Path) -> bool:
    """Kopie živého souboru; False = zdroj mezitím zmizel.

    Čtení otevřeného souboru drží starý inode, takže kopie je celá i při
    souběžném `os.replace` enginu. Chyba zápisu kopie (plný disk, délka cesty)
    je výjimka, ne tiše vynechaný soubor.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, target)
    except FileNotFoundError:
        if source.exists():
            raise
        return False
    return True


def day_ranges(files: list[DataFile]) -> dict[str, dict[str, str]]:
    """Rozsah dní (podle data v názvu souboru) po adresářích první úrovně data/."""
    ranges: dict[str, dict[str, str]] = {}
    for item in files:
        top, _, rest = item.rel.partition("/")
        match = DAY_RE.search(PurePosixPath(rest).name) if rest else None
        if match is None:
            continue
        day = match.group(0)
        current = ranges.setdefault(top, {"from": day, "to": day})
        current["from"] = min(current["from"], day)
        current["to"] = max(current["to"], day)
    return dict(sorted(ranges.items()))


# ── Balíček ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Member:
    arcname: str
    size: int
    mtime: int  # celé sekundy: desetinná část by přidala PAX hlavičku navíc
    source: Path | None = None
    data: bytes | None = None
    # Soubor čtený přímo z data/: změna velikosti nebo mtime během streamu = chyba
    check_mtime_ns: int | None = None
    live: bool = False

    def header(self) -> bytes:
        info = tarfile.TarInfo(self.arcname)
        info.size = self.size
        info.mtime = self.mtime
        info.mode = 0o644
        return info.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")


def _padding(size: int) -> int:
    return -size % TAR_BLOCK


@dataclass(frozen=True)
class SourceInfo:
    created: dt.datetime  # UTC
    host: str
    image_sha: str
    image_tag: str
    pg_version: str
    market_open: bool

    @property
    def created_utc(self) -> str:
        return self.created.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def sha7(self) -> str:
        return self.image_sha[:7] if self.image_sha != "dev" else "dev"


def source_info(environ: Mapping[str, str], now: dt.datetime, pg_version: str) -> SourceInfo:
    tag = environ.get("GEXLENS_API_TAG") or environ.get("GEXLENS_IMAGE_TAG") or "latest"
    return SourceInfo(
        created=now.astimezone(dt.UTC),
        # Kontejner API má hostname stroje z compose (compose.yml, compose.server.yml)
        host=socket.gethostname(),
        image_sha=environ.get(BUILD_SHA_ENV, "").strip() or "dev",
        image_tag=tag,
        pg_version=pg_version,
        market_open=not is_market_closed(now),
    )


def package_filename(info: SourceInfo) -> str:
    return f"gexlens-zaloha-{info.created:%Y-%m-%d_%H%M}-{info.sha7}.tar"


@dataclass(frozen=True)
class Package:
    members: tuple[Member, ...]  # v pořadí taru, bez manifestu
    head: dict[str, object]  # pole manifestu kromě `files`
    manifest_mtime: int

    def manifest_bytes(self, hashes: Mapping[str, str]) -> bytes:
        files = [
            {
                "path": member.arcname,
                "size": member.size,
                "sha256": hashes[member.arcname],
                "live": member.live,
            }
            for member in self.members
            if member.arcname in hashes
        ]
        document = {**self.head, "files": files}
        # Jen ASCII: jméno souboru s ne-UTF-8 bajty (surrogateescape) se zapíše
        # jako JSON escape a encode nespadne; délka zůstává daná předem
        return (json.dumps(document, indent=1) + "\n").encode("ascii")

    def manifest_size(self) -> int:
        return len(self.manifest_bytes(dict.fromkeys(self._arcnames(), SHA_PLACEHOLDER)))

    def _arcnames(self) -> list[str]:
        return [member.arcname for member in self.members]

    def manifest_header(self) -> bytes:
        return Member(MANIFEST_NAME, self.manifest_size(), self.manifest_mtime).header()

    def content_length(self) -> int:
        total = sum(
            len(member.header()) + member.size + _padding(member.size) for member in self.members
        )
        manifest = self.manifest_size()
        return total + len(self.manifest_header()) + manifest + _padding(manifest) + len(TAR_END)


def _restore_text(
    info: SourceInfo,
    environ: Mapping[str, str],
    ranges: Mapping[str, Mapping[str, str]],
    mode: str,
) -> str:
    template = resources.files("gexlens_api").joinpath("restore_template.md").read_text("utf-8")
    secrets_list = "\n".join(f"- `{key}`" for key in secret_keys(environ)) or "- (žádná)"
    days = (
        "\n".join(
            f"- `data/{top}/`: {span['from']} až {span['to']}" for top, span in ranges.items()
        )
        or "- (žádné denní soubory)"
    )
    values = {
        "created_utc": info.created_utc,
        "source_host": info.host,
        "image_sha": info.image_sha,
        "image_tag": info.image_tag,
        "checkout": (
            f"`git checkout {info.image_sha}`"
            if info.sha7 != "dev"
            else "zůstaň na `main` (záloha z lokálního buildu bez revize)"
        ),
        "image_pin": (
            f"`GEXLENS_IMAGE_TAG=sha-{info.sha7}` vrátí stejnou revizi image"
            if info.sha7 != "dev"
            else "pin image nejde (lokální build), poběží `latest`"
        ),
        "data_step": (
            "Přesuň `<rozbalený>/data` do kořene repozitáře"
            if mode == "full"
            else "Obnov `data/` z repa restic (`restic restore`) do kořene repozitáře"
        ),
        "pg_version": info.pg_version,
        "mode": mode,
        "market_note": (
            "Záloha vznikla za **otevřeného trhu**: soubory s `live: true` v manifestu "
            "(dnešní partice) mohou být neúplné."
            if info.market_open
            else "Záloha vznikla při zavřeném trhu."
        ),
        "secret_keys": secrets_list,
        "day_ranges": days,
    }

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Šablona RESTORE.md: neznámé pole {key}")
        return values[key]

    return re.sub(r"\{\{(\w+)\}\}", substitute, template)


def build_package(
    *,
    data_files: list[DataFile],
    dump: Path,
    info: SourceInfo,
    environ: Mapping[str, str],
    mode: str = "full",
) -> Package:
    """Seznam členů balíčku a hlavička manifestu — vše známé před streamem."""
    stamp = int(info.created.timestamp())
    ranges = day_ranges(data_files)
    texts = [
        (RESTORE_NAME, _restore_text(info, environ, ranges, mode)),
        (ENV_TEMPLATE_NAME, render_env_template(environ, info.created_utc)),
        (SECRET_KEYS_NAME, render_secret_keys(environ)),
    ]
    members = [Member(name, len(body), stamp, data=body) for name, body in _encoded(texts)]
    members.append(Member(DUMP_NAME, dump.stat().st_size, stamp, source=dump))
    members += [
        Member(
            DATA_PREFIX + item.rel,
            item.size,
            item.mtime_ns // 1_000_000_000,
            source=item.source,
            check_mtime_ns=None if item.live else item.mtime_ns,
            live=item.live,
        )
        for item in data_files
    ]
    head: dict[str, object] = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "mode": mode,
        "created_utc": info.created_utc,
        "source_host": info.host,
        "image_sha": info.image_sha,
        "image_tag": info.image_tag,
        "pg_server_version": info.pg_version,
        "market_open": info.market_open,
        "live_window_hours": int(LIVE_WINDOW.total_seconds() // 3600),
        "live_note": (
            "Soubory s live=true se změnily během posledních 24 h (dnešní partice): "
            "za otevřeného trhu mohou být neúplné; sha256 platí pro stav v okamžiku zálohy."
        ),
        "day_ranges": ranges,
    }
    return Package(tuple(members), head, stamp)


def _encoded(texts: list[tuple[str, str]]) -> Iterator[tuple[str, bytes]]:
    for name, body in texts:
        yield name, body.encode("utf-8")


def _member_content(member: Member) -> Iterator[bytes]:
    if member.data is not None:
        yield member.data
        return
    assert member.source is not None
    try:
        handle = open(member.source, "rb")  # noqa: SIM115 — zavírá with níže
    except FileNotFoundError as exc:
        raise PackageChangedError(f"{member.arcname} zmizel během zálohy") from exc
    with handle:
        stat = os.fstat(handle.fileno())
        changed = stat.st_size != member.size or (
            member.check_mtime_ns is not None and stat.st_mtime_ns != member.check_mtime_ns
        )
        if changed:
            raise PackageChangedError(
                f"{member.arcname} se změnil během zálohy (velikost {member.size} → "
                f"{stat.st_size}) — spusť zálohu znovu"
            )
        remaining = member.size
        while remaining:
            piece = handle.read(min(CHUNK_BYTES, remaining))
            if not piece:
                raise PackageChangedError(f"{member.arcname} se během čtení zkrátil")
            remaining -= len(piece)
            yield piece


def iter_package(package: Package) -> Generator[bytes, None, None]:
    """Tar po blocích ~1 MiB; paměť nezávisí na velikosti dat (akceptační kritérium #1799)."""
    buffer = bytearray()
    hashes: dict[str, str] = {}
    for member in package.members:
        buffer += member.header()
        digest = hashlib.sha256()
        for piece in _member_content(member):
            digest.update(piece)
            buffer += piece
            if len(buffer) >= CHUNK_BYTES:
                yield bytes(buffer)
                buffer.clear()
        buffer += b"\0" * _padding(member.size)
        hashes[member.arcname] = digest.hexdigest()
    manifest = package.manifest_bytes(hashes)
    if len(manifest) != package.manifest_size():
        raise RuntimeError("Velikost manifestu nesedí s předpovědí — Content-Length by lhal")
    buffer += package.manifest_header() + manifest + b"\0" * _padding(len(manifest)) + TAR_END
    yield bytes(buffer)


# ── Dump, pracovní adresář, příprava ──────────────────────────────────


async def dump_to_file(
    database_url: str, target: Path, cancel: threading.Event | None = None
) -> None:
    """`pg_dump -Fc` do souboru (tar potřebuje velikost člena předem).

    Odpojení klienta (`cancel`) proces ukončí; stderr se čte souběžně (#497).
    """
    command = dump_command(database_url)
    command = [*command[:-1], f"--file={target}", command[-1]]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **dump_env(database_url)},
    )
    assert process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        while True:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
                break
            except TimeoutError:
                if cancel is not None and cancel.is_set():
                    raise BackupCancelled from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
            logger.info("Úplná záloha přerušena — pg_dump ukončen")
        stderr = await stderr_task
    if process.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"pg_dump skončil s kódem {process.returncode}: {stderr.decode(errors='replace')}"
        )


def pg_server_version(database_url: str) -> str:
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("SHOW server_version")).scalar_one())
    finally:
        engine.dispose()


def work_root(data_dir: Path) -> Path:
    return data_dir / WORK_DIR


def new_run_dir(data_dir: Path, now: dt.datetime) -> Path:
    run_dir = (
        work_root(data_dir) / f"{RUN_PREFIX}{now.astimezone(dt.UTC):{RUN_STAMP}}-{os.getpid()}"
    )
    run_dir.mkdir(parents=True)
    return run_dir


def cleanup_stale_runs(data_dir: Path, now: dt.datetime) -> None:
    """Uklidí pracovní adresáře přerušených běhů (pád API uprostřed streamu)."""
    root = work_root(data_dir)
    if not root.is_dir():
        return
    for path in root.glob(f"{RUN_PREFIX}*"):
        stamp = path.name[len(RUN_PREFIX) :].split("-", 1)[0]
        try:
            started = dt.datetime.strptime(stamp, RUN_STAMP).replace(tzinfo=dt.UTC)
        except ValueError:
            continue  # cizí adresář — nesahat
        if now - started > STALE_RUN_AGE:
            logger.warning("Mažu pracovní adresář přerušené zálohy %s", path.name)
            shutil.rmtree(path, ignore_errors=True)


def ensure_outside_backup(target: Path, data_dir: Path) -> None:
    """Cíl uvnitř zálohovaných částí data/ by zabalil sám sebe do další zálohy."""
    try:
        rel = target.resolve().relative_to(data_dir.resolve())
    except ValueError:
        return
    parts = rel.parts
    prefixes = {"/".join(parts[: index + 1]) for index in range(len(parts))}
    if not prefixes & EXCLUDED_DIRS:
        raise ValueError(
            f"Cíl {target} leží v zálohovaných datech — použij data/local/… nebo cestu mimo data/"
        )


def prepare_full(
    data_dir: Path,
    run_dir: Path,
    info: SourceInfo,
    environ: Mapping[str, str],
    cancel: threading.Event | None = None,
) -> Package:
    files = scan_data(data_dir, info.created, run_dir / "live", cancel)
    return build_package(
        data_files=files, dump=run_dir / "gexlens.dump", info=info, environ=environ
    )


# ── Režim --meta-dir ──────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while piece := handle.read(CHUNK_BYTES):
            digest.update(piece)
    return digest.hexdigest()


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_bytes(body)
    os.replace(tmp, path)


def write_meta(package: Package, meta_dir: Path) -> dict[str, str]:
    """Zapíše meta soubory a manifest (poslední = značka dokončení); data/ jen hashuje.

    Data čte restic přímo, takže soubor, který mezitím zmizel, z manifestu
    vypadne a živý soubor má hash stavu v okamžiku hashování.
    """
    hashes: dict[str, str] = {}
    for member in package.members:
        if member.data is not None:
            _write_atomic(meta_dir / member.arcname, member.data)
            hashes[member.arcname] = hashlib.sha256(member.data).hexdigest()
            continue
        assert member.source is not None
        try:
            hashes[member.arcname] = _hash_file(member.source)
        except FileNotFoundError:
            logger.info("Soubor %s zmizel před hashováním — vynechán z manifestu", member.arcname)
    _write_atomic(meta_dir / MANIFEST_NAME, package.manifest_bytes(hashes))
    return hashes


# ── Endpoint ──────────────────────────────────────────────────────────

# Jedna úplná záloha naráz: dva streamy po ~13 GB by jen zdvojily IO disku
_RUN_LOCK = threading.Lock()


async def _watch_disconnect(request: Request, cancel: threading.Event) -> None:
    while not cancel.is_set():
        if await request.is_disconnected():
            logger.info("Klient se odpojil během přípravy úplné zálohy")
            cancel.set()
            return
        await asyncio.sleep(1)


def _next_chunk(chunks: Iterator[bytes]) -> bytes | None:
    return next(chunks, None)


async def _stream(package: Package) -> AsyncGenerator[bytes, None]:
    """Stream z vlákna. `anyio.to_thread` čeká na doběhnutí vlákna i při zrušení,
    takže `chunks.close()` nikdy nepotká běžící generátor."""
    chunks = iter_package(package)
    completed = False
    try:
        while (chunk := await anyio.to_thread.run_sync(_next_chunk, chunks)) is not None:
            yield chunk
        completed = True
    except PackageChangedError as exc:
        logger.error("Úplná záloha selhala: %s", exc)
        raise
    finally:
        chunks.close()
        if not completed:
            logger.info("Stahování úplné zálohy nedokončeno")


class _BackupResponse(StreamingResponse):
    """Stream s úklidem pracovního adresáře a zámku za všech okolností.

    Úklid ve `finally` async generátoru nestačí: když spojení spadne už při
    odeslání hlaviček, Starlette tělo vůbec nezačne číst a generátor se
    nikdy nespustí — zámek by zůstal držený do restartu API.
    """

    def __init__(
        self,
        chunks: AsyncGenerator[bytes, None],
        cleanup: Callable[[], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(chunks, **kwargs)
        self._chunks = chunks
        self._cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._chunks.aclose()
            await self._cleanup()


def build_full_backup_router(
    database_url: str,
    data_dir: Path,
    require_token: Callable[..., None] | None = None,
    environ: Mapping[str, str] | None = None,
    tickets: OneTimeTickets | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/backup", tags=["backup"])
    token_guard = [Depends(require_token)] if require_token is not None else []
    issued = OneTimeTickets() if tickets is None else tickets

    def token_or_ticket(
        ticket: str | None = Query(default=None),
        supplied: str | None = Header(default=None, alias=TOKEN_HEADER),
    ) -> None:
        """Hlavička s tokenem (fetch z UI, curl), nebo jednorázový lístek (nativní stažení)."""
        if ticket is not None:
            if not issued.redeem(ticket):
                raise HTTPException(
                    401, "Lístek ke stažení neplatí — je použitý, prošlý (5 min) nebo neznámý"
                )
            return
        if require_token is not None:
            require_token(supplied)

    @router.post("/full/ticket", dependencies=token_guard)
    async def full_backup_ticket() -> dict[str, str]:
        """Jednorázový lístek pro `GET /backup/full?ticket=…` (#1801) a navržené jméno souboru."""
        env = os.environ if environ is None else environ
        # Jméno jen pro dialog „Uložit jako“ před požadavkem; verze PG v něm není
        info = source_info(env, dt.datetime.now(dt.UTC), pg_version="")
        return {"ticket": issued.issue(), "filename": package_filename(info)}

    @router.get("/full", dependencies=[Depends(token_or_ticket)])
    async def full_backup(request: Request) -> Response:
        """Úplná záloha (celá PG + data/ + konfigurace bez tajemství) jako tar."""
        if shutil.which("pg_dump") is None:
            raise HTTPException(503, "pg_dump není v image k dispozici — přebuduj API kontejner")
        if not _RUN_LOCK.acquire(blocking=False):
            raise HTTPException(409, "Úplná záloha už běží — počkej na její dokončení")
        env = os.environ if environ is None else environ
        run_dir: Path | None = None

        async def cleanup() -> None:
            if run_dir is not None:
                await anyio.to_thread.run_sync(shutil.rmtree, run_dir, True)
            _RUN_LOCK.release()

        cancel = threading.Event()
        watcher = asyncio.create_task(_watch_disconnect(request, cancel))
        try:
            now = dt.datetime.now(dt.UTC)
            await asyncio.to_thread(cleanup_stale_runs, data_dir, now)
            run_dir = await asyncio.to_thread(new_run_dir, data_dir, now)
            version = await asyncio.to_thread(pg_server_version, database_url)
            info = source_info(env, now, version)
            await dump_to_file(database_url, run_dir / "gexlens.dump", cancel)
            package = await anyio.to_thread.run_sync(
                prepare_full, data_dir, run_dir, info, env, cancel
            )
            # Desítky tisíc hlaviček a manifest (~1 s na PC) — mimo event loop
            length = await anyio.to_thread.run_sync(package.content_length)
            filename = package_filename(info)
        except BackupCancelled:
            await cleanup()
            return Response(status_code=499)
        except Exception as exc:
            await cleanup()
            # Text výjimky do logu, ne klientovi (cesty a stderr pg_dump)
            logger.exception("Příprava úplné zálohy selhala")
            raise HTTPException(500, "Příprava úplné zálohy selhala — viz log API") from exc
        except BaseException:
            await cleanup()
            raise
        finally:
            cancel.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        return _BackupResponse(
            _stream(package),
            cleanup,
            media_type=TAR_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(length),
                # GZipMiddleware by komprimoval už komprimovaná data a zahodil
                # Content-Length (průběh v UI, F4 #1801)
                "Content-Encoding": "identity",
            },
        )

    return router


# ── CLI ───────────────────────────────────────────────────────────────


async def _run_cli(args: argparse.Namespace) -> Path:
    from gexlens_engine.config import load_settings

    settings = load_settings()
    data_dir = Path(settings.data_dir)
    now = dt.datetime.now(dt.UTC)
    version = pg_server_version(settings.database_url)
    info = source_info(os.environ, now, version)
    if args.meta_dir is not None:
        meta_dir = Path(args.meta_dir)
        ensure_outside_backup(meta_dir, data_dir)
        meta_dir.mkdir(parents=True, exist_ok=True)
        dump = meta_dir / DUMP_NAME
        dump.parent.mkdir(parents=True, exist_ok=True)
        part = dump.with_name(dump.name + TMP_SUFFIX)
        await dump_to_file(settings.database_url, part)
        os.replace(part, dump)
        files = scan_data(data_dir, now, live_dir=None)
        package = build_package(
            data_files=files, dump=dump, info=info, environ=os.environ, mode="meta"
        )
        write_meta(package, meta_dir)
        return meta_dir / MANIFEST_NAME
    out = Path(args.out)
    ensure_outside_backup(out, data_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    cleanup_stale_runs(data_dir, now)
    run_dir = new_run_dir(data_dir, now)
    part = out.with_name(out.name + TMP_SUFFIX)
    try:
        await dump_to_file(settings.database_url, run_dir / "gexlens.dump")
        package = prepare_full(data_dir, run_dir, info, os.environ)
        with open(part, "wb") as handle:
            for chunk in iter_package(package):
                handle.write(chunk)
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(run_dir, ignore_errors=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gexlens_api.full_backup",
        description="Úplná záloha GEXLens (#1799): celá PG + data/ + konfigurace bez tajemství.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--out", help="celý balíček .tar do tohoto souboru")
    target.add_argument(
        "--meta-dir", help="jen dump, config, manifest a RESTORE.md (data/ bere restic přímo)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        result = asyncio.run(_run_cli(args))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Úplná záloha selhala: {exc}", file=sys.stderr)
        return 1
    print(f"Úplná záloha hotová: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
