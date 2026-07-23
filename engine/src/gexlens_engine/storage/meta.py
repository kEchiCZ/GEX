"""Metadata tabulky (SPEC 5.3): watchlist, alerts, annotations, settings.

Schéma je definované v enginu (vlastník PostgreSQL storage); CRUD nad ním
poskytuje API server (issue #21). JSON sloupce fungují na PostgreSQL i SQLite
(testy).
"""

from sqlalchemy import JSON, Boolean, Column, Date, Integer, MetaData, String, Table

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

settings_table = Table(
    "settings",
    meta_metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON, nullable=False),
)
