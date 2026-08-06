"""Testy zálohy PostgreSQL (#439) a úklidu podprocesu při přerušení (#497)."""

import asyncio
import datetime as dt
import logging
import sys
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

import gexlens_api.backup as backup
from gexlens_api.backup import backup_filename, dump_command, dump_env, stream_dump

DSN = "postgresql+psycopg://gexlens:tajne@postgres:5432/gexlens"


def test_dump_command_from_dsn() -> None:
    command = dump_command(DSN)
    assert command[0] == "pg_dump"
    assert "--format=custom" in command
    assert command[-1] == "gexlens"  # jméno databáze jde poslední
    assert "--host" in command and "postgres" in command
    assert "--port" in command and "5432" in command
    assert "--username" in command and "gexlens" in command


def test_password_never_lands_in_argv() -> None:
    """Heslo v argv by bylo vidět v `ps` — patří do prostředí."""
    assert "tajne" not in dump_command(DSN)
    assert dump_env(DSN) == {"PGPASSWORD": "tajne"}


def test_dsn_without_password_has_empty_env() -> None:
    assert dump_env("postgresql+psycopg://gexlens@postgres:5432/gexlens") == {}


def test_dsn_without_database_is_rejected() -> None:
    """Bez jména databáze nemá záloha co dumpovat — chyba hned, ne v podprocesu."""
    with pytest.raises(ValueError, match="jméno databáze"):
        dump_command("postgresql+psycopg://gexlens:tajne@postgres:5432/")


def test_backup_filename_is_sortable_and_timestamped() -> None:
    name = backup_filename(dt.datetime(2026, 8, 3, 22, 5))
    assert name == "gexlens-2026-08-03_2205.dump"


# ── Úklid podprocesu (#497) ──────────────────────────────────────────


def _fake_pg_dump(monkeypatch: pytest.MonkeyPatch, script: str) -> list[Any]:
    """Nahradí pg_dump python skriptem a sbírá spuštěné procesy pro asserty."""
    monkeypatch.setattr(backup, "dump_command", lambda url: [sys.executable, "-c", script])
    processes: list[Any] = []
    original = asyncio.create_subprocess_exec

    async def recording(*args: Any, **kwargs: Any) -> Any:
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    return processes


async def test_preruseni_stahovani_ukonci_pg_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """#497: `aclose()` v půlce stahování musí proces zabít — bez killu visí
    pg_dump na zaplněné stdout rouře, kterou už nikdo nečte, a s ním i task."""
    endless = (
        "import sys\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x' * 65536)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    processes = _fake_pg_dump(monkeypatch, endless)

    # `stream_dump` je anotovaný jako AsyncIterator, `aclose` má až generátor
    stream = cast(AsyncGenerator[bytes, None], stream_dump(DSN))
    first = await asyncio.wait_for(stream.__anext__(), timeout=10.0)
    assert first  # stahování začalo, proces žije a chrlí data

    # Klient se odpojil — Starlette generátor zavře; úklid nesmí viset
    await asyncio.wait_for(stream.aclose(), timeout=10.0)

    assert len(processes) == 1
    assert processes[0].returncode is not None  # žádný osiřelý proces


async def test_upovidany_stderr_nezablokuje_dokonceni(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#497: >64 KB stderr před EOF stdout nesmí vést k deadlocku — stderr se
    čte souběžně a chyba se po dokončení zaloguje."""
    noisy = (
        "import sys\n"
        "sys.stdout.buffer.write(b'data')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.write('e' * 200000)\n"
        "sys.exit(3)\n"
    )
    _fake_pg_dump(monkeypatch, noisy)

    chunks = []

    async def consume() -> None:
        async for chunk in stream_dump(DSN):
            chunks.append(chunk)

    with caplog.at_level(logging.ERROR, logger="gexlens_api.backup"):
        await asyncio.wait_for(consume(), timeout=10.0)

    assert b"".join(chunks) == b"data"
    assert any("kódem 3" in record.getMessage() for record in caplog.records)
