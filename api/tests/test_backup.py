"""Testy zálohy PostgreSQL (#439)."""

import datetime as dt

import pytest

from gexlens_api.backup import backup_filename, dump_command, dump_env

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
