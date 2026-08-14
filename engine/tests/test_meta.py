"""Schéma metadata tabulek a jeho aditivní migrace (#709)."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from gexlens_engine.storage.meta import (
    JOURNAL_PROFILES,
    MISTAKE_TAGS,
    default_profile,
    ensure_meta_schema,
    journal_table,
    journal_trades_table,
)


def test_ensure_meta_schema_je_idempotentni(tmp_path: Path) -> None:
    """Opakované volání nesmí spadnout ani nic přepsat."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.db'}")
    ensure_meta_schema(engine)
    ensure_meta_schema(engine)

    inspector = inspect(engine)
    assert inspector.has_table(journal_table.name)
    assert inspector.has_table(journal_trades_table.name)
    columns = {col["name"] for col in inspector.get_columns(journal_table.name)}
    assert "profile" in columns


def test_migrace_doplni_profile_stare_tabulce(tmp_path: Path) -> None:
    """Deník z fáze A (#673) sloupec `profile` nemá — ALTER ho doplní.

    Data musí přežít: `create_all` existující tabulku nemění, takže bez
    migrace by API na produkční DB padalo na chybějícím sloupci.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE journal_entries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ref TIMESTAMP, symbol VARCHAR(16),"
                " entry_type VARCHAR(16), text TEXT, tags JSON, setup_id INTEGER,"
                " news_event_id INTEGER, created_ts TIMESTAMP, updated_ts TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO journal_entries (ts_ref, symbol, entry_type, text, tags, created_ts)"
                " VALUES ('2026-07-16 14:30:00', 'ES', 'pozorovani', 'Stary zaznam',"
                " '[]', '2026-07-16 14:31:00')"
            )
        )

    ensure_meta_schema(engine)  # nesmí spadnout na existující tabulce bez sloupce

    with engine.connect() as conn:
        rows = list(conn.execute(text("SELECT text, profile FROM journal_entries")))
    assert len(rows) == 1
    assert rows[0][0] == "Stary zaznam"
    # NULL, ne dosazená hodnota — v DB zůstane poznat, že profil nebyl zadaný
    assert rows[0][1] is None


def test_default_profile_podle_symbolu() -> None:
    assert default_profile("ES") == "futures"
    assert default_profile("nq") == "futures"
    assert default_profile("AAPL") == "smb"
    assert set(JOURNAL_PROFILES) == {"smb", "futures"}


def test_mistake_tagy_jsou_unikatni() -> None:
    """Číselník je uzavřený výčet — duplicita by rozbila Σ P/L per tag."""
    assert len(set(MISTAKE_TAGS)) == len(MISTAKE_TAGS)
