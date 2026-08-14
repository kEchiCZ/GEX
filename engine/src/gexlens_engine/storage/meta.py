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
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

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

# Deník tradera (#673 fáze A, #709 rev. 2). PG navždy, stejná třída dat jako
# anotace: záznamy vázané na okamžik (ts_ref) a symbol, volné tagy, volitelná
# vazba na setup/news event.
JOURNAL_TYPES = ("pozorovani", "hypoteza", "retro_dne", "obchod")

# Profil deníku (#709): řídí, která pole formulář ukazuje. Ukládá se
# U ZÁZNAMU, ne globálně — historické zápisy si drží profil, pod kterým
# vznikly, takže změna výchozí volby nikdy nepřepíše minulost.
JOURNAL_PROFILES = ("smb", "futures")

# Symboly, pro které se předvyplňuje profil `futures`. Ostatní dostanou `smb`.
FUTURES_SYMBOLS = ("ES", "NQ", "MES", "MNQ", "RTY", "YM", "M2K", "MYM")

# Číselník chyb (#709). Záměrně uzavřený výčet, ne volný text — jen tak jde
# spočítat, kolik která chyba stojí (Σ P/L per tag). Roste přidáním položky.
MISTAKE_TAGS = (
    "chased_entry",
    "moved_stop",
    "oversized",
    "undersized",
    "revenge_trade",
    "fomo",
    "early_exit",
    "late_exit",
    "no_plan",
    "off_plan",
    "overtrading",
)

# Známky: kvalita setupu a kvalita exekuce se hodnotí ODDĚLENĚ od výsledku
# (SMB, Steenbarger) — dobrý obchod a ziskový obchod jsou dvě různé věci.
JOURNAL_GRADES = ("A", "B", "C")

TRADE_DIRECTIONS = ("long", "short")


def default_profile(symbol: str) -> str:
    """Výchozí profil podle symbolu; volba zůstává na uživateli."""
    return "futures" if symbol.upper() in FUTURES_SYMBOLS else "smb"


journal_table = Table(
    "journal_entries",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts_ref", DateTime(timezone=True), nullable=False),
    Column("symbol", String(16), nullable=False),
    Column("entry_type", String(16), nullable=False),  # JOURNAL_TYPES
    Column("text", Text, nullable=False),
    Column("tags", JSON, nullable=False, default=list),
    Column("setup_id", Integer, nullable=True),
    Column("news_event_id", Integer, nullable=True),
    # Aditivní od #709 — řádky z fáze A mají NULL a čtou se jako `smb`
    # (viz `_migrate_journal_columns`); tichý přepis historie by falšoval,
    # pod jakým profilem záznam vznikl.
    Column("profile", String(16), nullable=True),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("updated_ts", DateTime(timezone=True), nullable=True),
)

# Strukturovaný obchod (#709) — oddělená tabulka 1:1 k záznamu typu `obchod`.
# Proč zvlášť: `journal_entries` zůstává lehké pro pozorování a hypotézy,
# kterých bude drtivá většina, a nenese dvacet věčně prázdných sloupců.
#
# Odvozené hodnoty (planned R:R, realized R) se ZÁMĚRNĚ neukládají — jsou
# funkcí uložených polí a druhá kopie by se rozešla při editaci.
journal_trades_table = Table(
    "journal_trades",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "entry_id",
        Integer,
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("direction", String(8), nullable=False),  # TRADE_DIRECTIONS
    Column("planned_entry", Float, nullable=True),
    Column("planned_stop", Float, nullable=True),
    Column("planned_target", Float, nullable=True),
    Column("actual_entry", Float, nullable=True),
    Column("actual_exit", Float, nullable=True),
    Column("size", Float, nullable=True),
    Column("opened_ts", DateTime(timezone=True), nullable=True),
    Column("closed_ts", DateTime(timezone=True), nullable=True),
    Column("setup_key", String(64), nullable=True),  # playbook (#710)
    Column("setup_grade", String(1), nullable=True),  # JOURNAL_GRADES
    Column("execution_grade", String(1), nullable=True),
    Column("mistake_tags", JSON, nullable=False, default=list),  # MISTAKE_TAGS
    Column("emotion", Integer, nullable=True),  # 1–5
    Column("mfe", Float, nullable=True),
    Column("mae", Float, nullable=True),
    Column("gross_pnl", Float, nullable=True),
    Column("net_pnl", Float, nullable=True),
    Column("fees", Float, nullable=True),
)

settings_table = Table(
    "settings",
    meta_metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON, nullable=False),
)


def ensure_meta_schema(engine: Engine) -> None:
    """Založí metadata tabulky a doplní aditivní sloupce (#709).

    `create_all` existující tabulku NEMĚNÍ, takže sloupce přidané později
    musí dostat ruční ALTER — stejný vzor jako `oi_archive` (#519) nebo
    `setups_store` (#311); repo nemá alembic. Voláno z obou stran (API
    repository i engine), aby se tvar schématu nerozešel.
    """
    meta_metadata.create_all(engine)
    inspector = inspect(engine)
    if not inspector.has_table(journal_table.name):
        return
    columns = {col["name"] for col in inspector.get_columns(journal_table.name)}
    # Deník jsou naměřená data — jen aditivní ADD COLUMN, nikdy DROP.
    # Staré řádky zůstanou s NULL: `profile` se dopočítá až při čtení,
    # zpětný UPDATE by tvrdil, že záznam vznikl pod profilem, který tehdy
    # neexistoval.
    additive = {"profile": "VARCHAR(16)"}
    missing = {name: sql for name, sql in additive.items() if name not in columns}
    if not missing:
        return
    with engine.begin() as conn:
        for name, sql_type in missing.items():
            conn.execute(text(f"ALTER TABLE {journal_table.name} ADD COLUMN {name} {sql_type}"))
