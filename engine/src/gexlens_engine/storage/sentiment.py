"""Schéma SentimentLensu (#269) — všechny tabulky modulu na jednom místě.

Zdroj pravdy je `docs/Sentiment/sentiment-SPEC-v1.md` kap. 2, 5.6, 6.3 a 7.
Tabulky pro pozdější milestones (N6–N8) se zakládají **už teď**, aby migrace
byly dopředné a nemusely couvat (SPEC kap. 11).

Umístění: schéma sdílí engine, API i budoucí `news-engine` (SPEC S1 — samostatný
proces nad stejnou PostgreSQL), proto žije v `gexlens_engine.storage` vedle
ostatních repozitářů, ne v samostatném balíčku.

Klíčové invarianty, které schéma vynucuje:

* **S11 point-in-time** — klasifikace se nikdy nepřepisuje, jen přidává verzi
  (`news_classifications`); predikce i signály nesou verzi, ze které vznikly,
  a jsou immutable.
* **S5 věčná retence** — nic z těchto tabulek nemá mazací API; jsou to trénovací
  data (výjimka z 14denní retence stejně jako OI archiv).
* **Anti-šum** — reakce nesou `contaminated` a `deferred`, aby se kontaminovaná
  okna a víkendové gapy daly z trénovacích statistik vyloučit (SPEC 5.1).
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine

sentiment_metadata = MetaData()

# Povolené hodnoty výčtů držíme jako tuple v Pythonu a v DB je vedeme jako text.
# Důvod: nativní PG enum se špatně rozšiřuje (ALTER TYPE v migraci) a modul
# počítá s přibýváním kategorií i zdrojů. Validaci dělá pydantic ve vrstvě
# normalizace (SPEC 3.2), ne databáze.
NEWS_KINDS = ("scheduled", "headline", "social", "broker")
NEWS_CATEGORIES = (
    "FED",
    "MACRO_INFLATION",
    "MACRO_LABOR",
    "MACRO_GROWTH",
    "GEOPOLITICS",
    "ENERGY",
    "TECH",
    "EARNINGS",
    "CRYPTO",
    "OTHER",
)
CLASSIFICATION_SOURCES = ("rule", "llm", "manual")
PREDICTORS = ("llm", "learned")
SIGNAL_MODES = ("NEWS", "COMBINED")
REACTION_WINDOWS = (1, 5, 15, 60)


# ── Jádro: události a jejich klasifikace ───────────────────────────

news_events = Table(
    "news_events",
    sentiment_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Čas události, ne ingestu — u scheduled je to čas release (SPEC 2.1)
    Column("ts_event", DateTime(timezone=True), nullable=False),
    # Kdy jsme ji získali; rozdíl proti ts_event měří vlastní latenci zdroje
    Column("ts_ingested", DateTime(timezone=True), nullable=False),
    Column("source", String(32), nullable=False),
    Column("source_uid", String(128), nullable=True),
    Column("kind", String(16), nullable=False),
    Column("category", String(24), nullable=True),
    Column("importance", SmallInteger, nullable=True),
    Column("title", Text, nullable=False),
    Column("summary", Text, nullable=True),
    Column("symbols", JSON, nullable=False, default=list),
    # Jen scheduled (Tier A)
    Column("forecast", Numeric, nullable=True),
    Column("previous", Numeric, nullable=True),
    Column("actual", Numeric, nullable=True),
    # (actual − forecast) / historická σ překvapení řady
    Column("surprise_z", Numeric, nullable=True),
    # Denormalizace poslední verze z news_classifications (rychlé čtení feedu);
    # zdrojem pravdy zůstává historie verzí (S11)
    Column("sentiment_dir", SmallInteger, nullable=True),
    Column("sentiment_score", Numeric, nullable=True),
    Column("sentiment_source", String(8), nullable=True),
    # Trhy zavřené v čase události → reakce se měří deferred (SPEC 5.1)
    Column("market_closed", Boolean, nullable=False, default=False),
    Column("dedup_hash", String(64), nullable=False, unique=True),
    Column("raw", JSON, nullable=False, default=dict),
    Index("ix_news_events_ts", "ts_event"),
    Index("ix_news_events_cat_ts", "category", "ts_event"),
    Index("ix_news_events_source_uid", "source", "source_uid"),
)

# Append-only verzování (S11): každý průchod klasifikace přidá řádek, nikdy
# nepřepisuje. Umožňuje rekonstruovat, co systém věděl v libovolném okamžiku —
# bez toho by zpětná reklasifikace tiše měnila minulé predikce.
news_classifications = Table(
    "news_classifications",
    sentiment_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("news_events.id"), nullable=False),
    Column("version", SmallInteger, nullable=False),
    Column("source", String(8), nullable=False),
    Column("category", String(24), nullable=True),
    Column("importance", SmallInteger, nullable=True),
    Column("direction", SmallInteger, nullable=True),
    Column("strength", Numeric, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_id", "version", name="uq_news_classification_version"),
)


# ── Měření reakce trhu ─────────────────────────────────────────────

news_reactions = Table(
    "news_reactions",
    sentiment_metadata,
    Column("event_id", Integer, ForeignKey("news_events.id"), primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    Column("ret_bp", Float, nullable=False),
    Column("range_bp", Float, nullable=False),
    Column("vol_z", Float, nullable=True),
    # Do okna spadl jiný event s importance ≥ 2 → mimo trénovací statistiky.
    # Bez toho by se všem headlines z Fed day přičetl tentýž pohyb (SPEC 5.1).
    Column("contaminated", Boolean, nullable=False, default=False),
    # Trhy byly zavřené → okno se měří od prvního obchodovaného baru; dynamika
    # gapu na open je jiná, proto vlastní buckety
    Column("deferred", Boolean, nullable=False, default=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)

# Empirický model fáze 1 (SPEC 5.2): agregáty per bucket, přepočet nočním jobem.
# Kontaminovaná okna se sem nezapočítávají.
news_model_stats = Table(
    "news_model_stats",
    sentiment_metadata,
    Column("category", String(24), primary_key=True),
    Column("importance", SmallInteger, primary_key=True),
    Column("surprise_bucket", String(16), primary_key=True),
    Column("deferred", Boolean, primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("n", Integer, nullable=False),
    Column("ret_mean_bp", Float, nullable=False),
    Column("ret_median_bp", Float, nullable=False),
    Column("ret_sigma_bp", Float, nullable=False),
    Column("hit_rate", Float, nullable=True),
    # Wilson dolní mez — bodová hit-rate při malém n je nerozlišitelná od mince
    Column("hit_rate_lb", Float, nullable=True),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


# ── Predikce a jejich vyhodnocení ──────────────────────────────────

# Immutable (S11): nese verzi klasifikace, ze které vznikla. Zpětná
# reklasifikace minulé predikce nikdy nemění.
news_predictions = Table(
    "news_predictions",
    sentiment_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("news_events.id"), nullable=False),
    Column("predicted_dir", SmallInteger, nullable=False),
    Column("predicted_strength", Numeric, nullable=True),
    Column("predictor", String(8), nullable=False),
    Column("classification_version", SmallInteger, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_news_predictions_event", "event_id"),
)

# Jeden řádek na okno: predikce „nahoru" může po 1 min sedět a po 60 min ne,
# takže jediný sloupec `correct` by neřekl, vůči čemu platí (SPEC 2.5).
news_prediction_outcomes = Table(
    "news_prediction_outcomes",
    sentiment_metadata,
    Column("prediction_id", Integer, ForeignKey("news_predictions.id"), primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    Column("realized_dir", SmallInteger, nullable=False),
    Column("correct", Boolean, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


# ── Odvozené řady a stav ───────────────────────────────────────────

# Denní OHLC kontinuálního SentIndexu (SPEC 7.1) — zdroj pro vlny.
# Díky absenci session resetu je `open` smysluplný: co z overnight zpráv zbylo.
sentiment_daily = Table(
    "sentiment_daily",
    sentiment_metadata,
    Column("date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("update_time", DateTime(timezone=True), nullable=False),
)

# Historie vln (SPEC 5.6) — statistika hloubek slouží jako adaptivní práh
sentiment_waves = Table(
    "sentiment_waves",
    sentiment_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False),
    Column("direction", String(8), nullable=False),  # RiskOn / RiskOff
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=True),  # null = probíhající vlna
    Column("depth", Float, nullable=False),
    Column("length_days", Integer, nullable=False),
    UniqueConstraint("symbol", "direction", "start_date", name="uq_sentiment_wave"),
)

# Crowd sentiment (SPEC 2.6, 5.8): kontinuální řady, NE diskrétní eventy —
# proto vlastní tabulka a záměrně mimo SentIndex (vlna WSB postů by index
# utopila víc než CPI a crowd bývá kontrariánský).
crowd_sentiment = Table(
    "crowd_sentiment",
    sentiment_metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("source", String(24), primary_key=True),
    Column("metric", String(32), primary_key=True),
    Column("symbol", String(16), primary_key=True, default=""),
    Column("value", Float, nullable=False),
    Column("raw", JSON, nullable=True),
)


# ── Signály, review a track record ─────────────────────────────────

# Immutable (S11). `inputs` drží kompletní snapshot zdůvodnění — každý signál
# musí být zpětně vysvětlitelný (SPEC 6.3). Počítají se vždy, i když je
# zobrazení vypnuté (S9), jinak by track record neměl data.
signals = Table(
    "signals",
    sentiment_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("symbol", String(16), nullable=False),
    Column("direction", String(8), nullable=False),
    Column("strength", Float, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("inputs", JSON, nullable=False, default=dict),
    Column("expiry_ts", DateTime(timezone=True), nullable=False),
    Index("ix_signals_ts", "symbol", "ts"),
)

# Human-in-the-loop (SPEC 5.7): rozpor LLM × empirický model nebo nízká jistota.
# Neopravené položky se stejně vyhodnotí automaticky — systém funguje i bez
# ručních zásahů.
review_queue = Table(
    "review_queue",
    sentiment_metadata,
    Column("event_id", Integer, ForeignKey("news_events.id"), primary_key=True),
    Column("reason", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
)

# Mechanické backtestové křivky (SPEC 7.3) — point-in-time, kalibrační období
# se z reportu vylučuje; vstup na následující open po potvrzovacím close.
track_record = Table(
    "track_record",
    sentiment_metadata,
    Column("date", Date, primary_key=True),
    Column("strategy", String(32), primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("equity", Float, nullable=False),
    Column("drawdown", Float, nullable=True),
)


def ensure_sentiment_schema(engine: Engine) -> None:
    """Založí všechny tabulky SentimentLensu (idempotentní).

    Volá se při startu news-engine i API, aby modul fungoval nad čerstvou DB
    bez ručního kroku. Tabulky pozdějších milestones vznikají rovnou — migrace
    mají být dopředné (SPEC kap. 11).
    """
    sentiment_metadata.create_all(engine)
