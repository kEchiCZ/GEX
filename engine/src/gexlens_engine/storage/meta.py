"""Metadata tabulky (SPEC 5.3): watchlist, alerts, annotations, settings.

Schéma je definované v enginu (vlastník PostgreSQL storage); CRUD nad ním
poskytuje API server (issue #21). JSON sloupce fungují na PostgreSQL i SQLite
(testy).
"""

import datetime as dt

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
    func,
    inspect,
    select,
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
    # (viz `ensure_meta_schema`); tichý přepis historie by falšoval,
    # pod jakým profilem záznam vznikl.
    Column("profile", String(16), nullable=True),
    # Snímek GEX kontextu k `ts_ref` (#711). Ukládá se HODNOTA, ne odkaz na
    # přepočet: Parquet má retenci 90 dní (ADR-0022) a mechanika se verzuje,
    # takže zpětný přepočet by dal jiná čísla než ta, podle kterých se
    # rozhodovalo. NULL = kontext se nepodařilo složit (starý záznam, výpadek).
    Column("context", JSON, nullable=True),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("updated_ts", DateTime(timezone=True), nullable=True),
)

# Proč teze selhala (#711, jen profil `futures` a jen u ztrátových obchodů).
# Taxonomie podle SpotGamma; `map_moved` jde doložit PROTI uloženému snímku —
# přesně to, co bez kontextu nešlo rozhodnout.
FAILURE_MODES = (
    "customer_held_wall",
    "vol_regime_shift",
    "non_hedging_actor",
    "level_as_target",
    "map_moved",
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
    Column("failure_mode", String(32), nullable=True),  # FAILURE_MODES (#711)
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

# PlayBook setupů (#710) — jádro SMB metodiky: archiv pojmenovaných,
# opakovatelných setupů, kde každý má vlastní kartu a vlastní statistiku.
# Obchoduje se jen to, co je v playbooku; bez pojmenovaného setupu jako
# povinného pole není co agregovat.
playbook_table = Table(
    "journal_playbook",
    meta_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("profile", String(16), nullable=False, default="both"),  # PLAYBOOK_SCOPES
    Column("thesis", Text, nullable=False, default=""),
    Column("entry_conditions", Text, nullable=False, default=""),
    Column("invalidation", Text, nullable=False, default=""),
    Column("management", Text, nullable=False, default=""),
    # Vyřazený setup se NEMAŽE — historické záznamy na něj odkazují.
    Column("active", Boolean, nullable=False, default=True),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("updated_ts", DateTime(timezone=True), nullable=True),
)

PLAYBOOK_SCOPES = ("smb", "futures", "both")

# Výchozí sada pro futures profil. Klíče se ZÁMĚRNĚ kryjí se šablonami
# detektoru (`compute/setups.py`), aby šlo porovnat „co detektor nabídl"
# s „co jsem vzal" (#627). Uživatel si je smí přejmenovat i vyřadit.
DEFAULT_PLAYBOOK: tuple[dict[str, str], ...] = (
    {
        "key": "wall_bounce",
        "name": "Odraz od zdi",
        "thesis": (
            "U silné zdi drží dealeři velkou gammu — hedging tlačí cenu zpět, dokud zeď vydrží."
        ),
        "entry_conditions": "Cena testuje zeď s dostatečnou dominancí, režim tlumící.",
        "invalidation": "Průraz zdi se zavřením za ní, nebo dominance zdi klesne.",
        "management": "Stop za zeď, cíl první protilehlá úroveň.",
    },
    {
        "key": "failed_break",
        "name": "Neúspěšný průraz",
        "thesis": "Průraz bez pokračování ukazuje, že za úrovní není poptávka.",
        "entry_conditions": "Cena projde úroveň a vrátí se zpět pod ni.",
        "invalidation": "Znovudobytí úrovně.",
        "management": "Stop za extrém průrazu, cíl opačná strana rozsahu.",
    },
    {
        "key": "max_pain_pin",
        "name": "Pin k Max Pain",
        "thesis": "Do expirace stahuje hedging cenu k striku s nejmenší vyplacenou hodnotou.",
        "entry_conditions": "Blízko expirace, cena v dosahu Max Pain, tlumící režim.",
        "invalidation": "Odchod z pásma, nebo skok volatility.",
        "management": "Malé cíle, těsné stopy — večer je brzda nejsilnější.",
    },
    {
        "key": "flip_cross",
        "name": "Průchod flip zónou",
        "thesis": "Za flipem se mění znaménko gammy: z tlumení na zesilování pohybu.",
        "entry_conditions": "Cena protne flip zónu se souhlasným tokem.",
        "invalidation": "Návrat do původního režimu.",
        "management": "Jít s pohybem, cíl další jasné pásmo.",
    },
    {
        "key": "gamma_momentum",
        "name": "Gamma momentum",
        "thesis": "V záporné gammě dealeři pohyb zesilují — pullbacky jsou mělké.",
        "entry_conditions": "Cena pod flipem, vzduchoprázdno k dalšímu pásmu.",
        "invalidation": "Vstup do tlumící zóny.",
        "management": "Nestavět nic proti pohybu.",
    },
    {
        "key": "zone_edge_reversion",
        "name": "Návrat k okraji zóny",
        "thesis": "Okraj tlumící zóny funguje jako mean-reversion hrana.",
        "entry_conditions": "Cena drží nad okrajem, pullback k němu.",
        "invalidation": "Ztráta okraje.",
        "management": "Cíl Max Pain nebo první zelené pásmo.",
    },
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
    _seed_playbook(engine)
    inspector = inspect(engine)
    if not inspector.has_table(journal_table.name):
        return
    columns = {col["name"] for col in inspector.get_columns(journal_table.name)}
    # Deník jsou naměřená data — jen aditivní ADD COLUMN, nikdy DROP.
    # Staré řádky zůstanou s NULL: `profile` se dopočítá až při čtení,
    # zpětný UPDATE by tvrdil, že záznam vznikl pod profilem, který tehdy
    # neexistoval.
    json_type = "JSONB" if engine.dialect.name == "postgresql" else "JSON"
    additive = {"profile": "VARCHAR(16)", "context": json_type}
    missing = {name: sql for name, sql in additive.items() if name not in columns}
    if missing:
        with engine.begin() as conn:
            for name, sql_type in missing.items():
                conn.execute(
                    text(f"ALTER TABLE {journal_table.name} ADD COLUMN {name} {sql_type}")
                )
    if not inspector.has_table(journal_trades_table.name):
        return
    trade_columns = {col["name"] for col in inspector.get_columns(journal_trades_table.name)}
    if "failure_mode" not in trade_columns:
        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {journal_trades_table.name} ADD COLUMN failure_mode VARCHAR(32)")
            )


def _seed_playbook(engine: Engine) -> None:
    """Naplní výchozí sadu setupů, jen když je playbook úplně prázdný (#710).

    Prázdný playbook by znamenal, že typ `obchod` nejde uložit (setup je
    povinný). Jednou naplněná tabulka se už nikdy nedoplňuje — uživatel si
    setupy přejmenovává i vyřazuje a návrat smazaného by ho jen otravoval.
    """
    with engine.begin() as conn:
        existing = conn.execute(select(func.count()).select_from(playbook_table)).scalar_one()
        if existing:
            return
        now = dt.datetime.now(dt.UTC)
        conn.execute(
            playbook_table.insert(),
            [
                {
                    **item,
                    "profile": "futures",
                    "active": True,
                    "created_ts": now,
                }
                for item in DEFAULT_PLAYBOOK
            ],
        )
