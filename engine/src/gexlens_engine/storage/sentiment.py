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

# Kategorie ze SPEC 2.1 — jediný povolený slovník; sdílené news-enginem
# (klasifikace) i API (validace ruční korekce, #293)
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
    # Plné znění článku (#743), pokud ho zdroj dodá — Alpaca `content`, IBKR
    # `reqNewsArticle`. Sloupec je vlastní, ne rozšířený `summary`: perex má
    # limit 500 znaků a jinou sémantiku, přepsat ho by rozbilo čtenáře feedu.
    # Model z něj bere jen titulek + první odstavec (celý článek by při dnešní
    # velikosti korpusu přeučoval), zbytek se drží pro pozdější přeměření.
    Column("body", Text, nullable=True),
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
    # Denní okna nikdy nepůjdou spočítat — event předchází pokrytí barů (#655).
    # NULL = nerozhodnuto; nastavuje výhradně ReactionJob, pending dotaz
    # označené eventy vynechává (jinak se přescanovávaly donekonečna).
    Column("daily_uncomputable", Boolean, nullable=True),
    Column("dedup_hash", String(64), nullable=False, unique=True),
    Column("raw", JSON, nullable=False, default=dict),
    Index("ix_news_events_ts", "ts_event"),
    Index("ix_news_events_cat_ts", "category", "ts_event"),
    Index("ix_news_events_source_uid", "source", "source_uid"),
)

# Append-only verzování (S11): každý průchod klasifikace přidá řádek, nikdy
# nepřepisuje. Umožňuje rekonstruovat, co systém věděl v libovolném okamžiku —
# bez toho by zpětná reklasifikace tiše měnila minulé predikce.
# Registr zdrojů (#578 A): popis reality zdrojů — tier se ZATÍM nikam
# nepropisuje do vah (nejdřív popis, pak měření auditem B, teprve pak váhy).
news_sources = Table(
    "news_sources",
    sentiment_metadata,
    Column("source", String(32), primary_key=True),
    # core = páteřní, extra = doplňkový, test = testovací (nový zdroj na zkoušku)
    Column("tier", String(8), nullable=False),
    Column("expected_daily_volume", Integer, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("notes", Text, nullable=True),
)

#: Výchozí registr — insert-if-missing, ruční úpravy v DB se nepřepisují
NEWS_SOURCE_SEED: tuple[tuple[str, str, int | None, str], ...] = (
    ("forexfactory", "core", 40, "makro kalendář Tier A (forecast/previous/actual)"),
    ("fed_rss", "core", 5, "oficiální Fed feedy — statements, projevy"),
    ("rss_news", "core", 300, "agenturní headline redundance (CNBC/MarketWatch/Yahoo)"),
    # Názvy dle reality (#922): engine zapisuje `ibkr_{provider}`, ne „ibkr"
    ("ibkr_brfg", "extra", 50, "broker páska Briefing.com (tick 292)"),
    ("ibkr_djnl", "extra", 50, "broker páska Dow Jones Newsletters (tick 292)"),
    ("alpaca", "extra", 800, "Benzinga WS push — vyžaduje klíče"),
    ("finnhub", "extra", 200, "doplňkový headline zdroj — vyžaduje klíč"),
    ("bluesky", "test", 200, "Jetstream firehose, přímá komunikace osob (#578, 27. 8.)"),
    ("reddit_rss", "test", 50, "r/wallstreetbets + r/stocks hot přes nativní RSS (#578)"),
    ("rss_user", "test", None, "vlastní RSS feedy uživatele (#578, editace v záložce News)"),
)


def seed_news_sources(engine: Engine) -> int:
    """Doplní chybějící řádky registru; existující (ručně editované) nechává."""
    from sqlalchemy import select as _select

    with engine.begin() as conn:
        existing = {row.source for row in conn.execute(_select(news_sources.c.source))}
        rows = [
            {
                "source": source,
                "tier": tier,
                "expected_daily_volume": volume,
                "enabled": True,
                "notes": notes,
            }
            for source, tier, volume, notes in NEWS_SOURCE_SEED
            if source not in existing
        ]
        if rows:
            conn.execute(news_sources.insert(), rows)
    return len(rows)


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
    # GEX režim v čase eventu (#402): positive/negative; None = levels toho dne
    # nejsou (starší data mimo retenci) — podmíněná větev roste od nasazení
    Column("gex_regime", String(16), nullable=True),
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
    # Režimová dimenze (#402): 'all' = nepodmíněný agregát (původní chování),
    # 'RiskOn'/'RiskOff'/'Neutral' = podmíněno stavem sentimentu v čase eventu,
    # 'gamma_positive'/'gamma_negative' = podmíněno GEX režimem reakce.
    Column("regime", String(16), primary_key=True, server_default="all"),
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


# Váhy predictorů (SPEC 5.3): hit-rate per kategorie a predictor v klouzavém
# okně → w_cat ve skóre eventu. Tabulka v #269 chyběla, doplněna v #282.
news_weights = Table(
    "news_weights",
    sentiment_metadata,
    Column("category", String(24), primary_key=True),
    Column("predictor", String(8), primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    # Váhy per symbol (ADR-0026, #579): outcomes ES a NQ se nesmí míchat,
    # jinak je „NQ index" jen kopie ES křivky
    Column("symbol", String(16), primary_key=True, server_default="ES"),
    Column("n", Integer, nullable=False),
    Column("hit_rate", Float, nullable=False),
    Column("hit_rate_lb", Float, nullable=False),
    # Váha odvozená z dolní meze — edge nad mincí, ne bodová úspěšnost
    Column("weight", Float, nullable=False),
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
    # Škálová normalizace (#640, rozhodnuto 24. 8.): σ = std(close) předchozích
    # 100 seancí (kauzálně, bez dneška), close_z = close/σ. NULL = málo historie
    # (< 30 seancí) nebo řádek před dopočtem. Surový close zůstává zdrojem pravdy.
    Column("sigma", Float, nullable=True),
    Column("close_z", Float, nullable=True),
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
    # Hloubka v jednotkách σ(100 seancí) — #640: éry řady mají různá měřítka
    # (backfill ±0,4 vs. živá do −3,9), σ je sjednocuje. NULL = σ ještě není.
    Column("depth_z", Float, nullable=True),
    # Verzování odvozené řady (#640): 'zscore_100'; kalibrace se nemíchají
    Column("series_variant", String(12), nullable=True),
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

# Vyhodnocení signálů (SPEC 6.3) — stejný mechanismus jako prediction
# outcomes: realizovaný pohyb v oknech po signálu, podklad pro srovnání
# NEWS vs. COMBINED. Ve schématu z N1 chyběla (doplněno v #294, precedens
# `news_weights` z #282).
signal_outcomes = Table(
    "signal_outcomes",
    sentiment_metadata,
    Column("signal_id", Integer, ForeignKey("signals.id"), primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    Column("ret_bp", Float, nullable=False),
    Column("realized_dir", SmallInteger, nullable=False),
    Column("correct", Boolean, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)

# Stínové vyhodnocení ngram magnitudové hlavy (#740 fáze 2): lift na živém
# subsetu vs. backfillu vs. celku — brána stejné konstrukce jako Wilson gate.
# Plně derivovaná (full-replace při každém vyhodnocení); hlava se nezapne,
# dokud live lift neporazí kategorie baseline na dostatečném vzorku (#749).
news_ngram_shadow = Table(
    "news_ngram_shadow",
    sentiment_metadata,
    Column("symbol", String(16), primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    # live (ts_ingested − ts_event ≤ 1 den) | backfill | all
    Column("subset", String(8), primary_key=True),
    Column("n", Integer, nullable=False),
    # Lift = průměrná |reakce| horního decilu podle P(velký pohyb) / celkový průměr
    Column("lift", Float, nullable=False),
    # Totéž řazené podle průměru |reakce| kategorie — baseline z #749
    Column("baseline_lift", Float, nullable=False),
    Column("top_decile_mean_bp", Float, nullable=False),
    Column("mean_bp", Float, nullable=False),
    Column("model_n_train", Integer, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
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
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    # Dopředná migrace #402: `news_model_stats` je plně derivovaná (noční
    # full-replace) — při chybějícím sloupci `regime` se tabulka zahodí
    # a create_all ji založí v novém tvaru; data doplní příští přepočet.
    if "news_model_stats" in existing:
        columns = {column["name"] for column in inspector.get_columns("news_model_stats")}
        if "regime" not in columns:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE news_model_stats"))
    # `news_weights` jsou plně derivované (noční full-replace) — při chybějícím
    # sloupci `symbol` (ADR-0026) se tabulka zahodí a založí v novém tvaru;
    # hodnoty doplní příští přepočet vah.
    if "sentiment_daily" in existing:
        columns = {column["name"] for column in inspector.get_columns("sentiment_daily")}
        for name in ("sigma", "close_z"):
            if name not in columns:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE sentiment_daily ADD COLUMN {name} FLOAT"))
    # `sentiment_waves` jsou plně derivované (full-replace per symbol) — při
    # chybějícím sloupci `depth_z` (#640) se tabulka zahodí a založí v novém
    # tvaru; vlny doplní příští přepočet WavesJob.
    if "sentiment_waves" in existing:
        columns = {column["name"] for column in inspector.get_columns("sentiment_waves")}
        if "depth_z" not in columns:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE sentiment_waves"))
    if "news_weights" in existing:
        columns = {column["name"] for column in inspector.get_columns("news_weights")}
        if "symbol" not in columns:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE news_weights"))
    # `news_events` jsou naměřená data — jen aditivní ADD COLUMN (#655).
    if "news_events" in existing:
        columns = {column["name"] for column in inspector.get_columns("news_events")}
        if "daily_uncomputable" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE news_events ADD COLUMN daily_uncomputable BOOLEAN"))
        if "body" not in columns:  # #743: plné znění článku
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE news_events ADD COLUMN body TEXT"))
    # `news_reactions` jsou naměřená data — jen aditivní ADD COLUMN.
    if "news_reactions" in existing:
        columns = {column["name"] for column in inspector.get_columns("news_reactions")}
        if "gex_regime" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE news_reactions ADD COLUMN gex_regime VARCHAR(16)"))
    sentiment_metadata.create_all(engine)

    # Rozdíl reakcí NQ − ES per (event, okno) — míra „technologického
    # charakteru" zprávy (ADR-0026, #579). View, ne materializace: obě
    # reakce trvale žijí v `news_reactions`, druhá kopie by lhala při
    # doplnění oken. PG umí CREATE OR REPLACE, SQLite (testy) jen IF NOT
    # EXISTS — definice se nemění, takže obojí je idempotentní.
    view_sql = (
        "VIEW news_reaction_spread AS "
        "SELECT nq.event_id AS event_id, nq.window_min AS window_min, "
        "nq.ret_bp - es.ret_bp AS spread_bp, "
        "nq.ret_bp AS nq_ret_bp, es.ret_bp AS es_ret_bp, "
        "(nq.contaminated OR es.contaminated) AS contaminated "
        "FROM news_reactions nq "
        "JOIN news_reactions es ON es.event_id = nq.event_id "
        "AND es.window_min = nq.window_min AND es.symbol = 'ES' "
        "WHERE nq.symbol = 'NQ'"
    )
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("CREATE OR REPLACE " + view_sql))
        else:
            conn.execute(text("CREATE VIEW IF NOT EXISTS" + view_sql.removeprefix("VIEW")))
