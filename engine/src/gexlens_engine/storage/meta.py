"""Metadata tabulky (SPEC 5.3): watchlist, alerts, annotations, settings.

Schéma je definované v enginu (vlastník PostgreSQL storage); CRUD nad ním
poskytuje API server (issue #21). JSON sloupce fungují na PostgreSQL i SQLite
(testy).
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

meta_metadata = MetaData()

# PG NOTIFY kanál změn watchlistu (#207): API po zápisu notifikuje, engine
# přes LISTEN probudí orchestrátor — nový symbol startuje do sekund, ne až
# za WATCHLIST_POLL_CYCLES minut. Mimo PostgreSQL zůstává jen poll.
WATCHLIST_CHANNEL = "gexlens_watchlist"

watchlist_table = Table(
    "watchlist",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False, unique=True),
)

alerts_table = Table(
    "alerts",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("params", JSON, nullable=False, default=dict),
    Column("enabled", Boolean, nullable=False, default=True),
)

annotations_table = Table(
    "annotations",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False),
    Column("day", Date, nullable=False),
    Column("payload", JSON, nullable=False),
)

# Deník tradera (#673, fáze A — manuální retrospektiva). PG navždy, stejná
# třída dat jako anotace: záznamy vázané na okamžik (ts_ref) a symbol,
# typy pozorovani/hypoteza/retro_dne (+ budoucí `obchod` z importu fillů,
# fáze B), volné tagy, volitelná vazba na setup/news event.
journal_table = Table(
    "journal_entries",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts_ref", DateTime(timezone=True), nullable=False),
    Column("symbol", String(16), nullable=False),
    Column("entry_type", String(16), nullable=False),  # pozorovani|hypoteza|retro_dne|obchod
    Column("text", Text, nullable=False),
    Column("tags", JSON, nullable=False, default=list),
    Column("setup_id", Integer, nullable=True),
    Column("news_event_id", Integer, nullable=True),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("updated_ts", DateTime(timezone=True), nullable=True),
)

settings_table = Table(
    "settings",
    meta_metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON, nullable=False),
)
