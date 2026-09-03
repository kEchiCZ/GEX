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

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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

# Denní okna (#564) se identifikují jako N × 1440 — hodnota je identifikátor
# okna, ne doslovný počet minut (10 obchodních dní = 14400).
REACTION_DAILY_WINDOWS = (1440, 2880, 7200, 14400)
REACTION_ALL_WINDOWS = REACTION_WINDOWS + REACTION_DAILY_WINDOWS
REACTION_PHASES = ("min", "daily")


def reaction_phase(window_min: int) -> str:
    """Fáze výpočtu okna: `min` (1–60 min, ReactionJob hned) / `daily` (#564)."""
    if window_min in REACTION_WINDOWS:
        return "min"
    if window_min in REACTION_DAILY_WINDOWS:
        return "daily"
    raise ValueError(f"Neznámé reakční okno {window_min}; povolená: {REACTION_ALL_WINDOWS}")


def _reaction_window_columns() -> list[Column[Any]]:
    """Sloupce per okno v pevném pořadí: ret, range, vol_z (jen minutová), cont."""
    columns: list[Column[Any]] = []
    for window in REACTION_ALL_WINDOWS:
        columns.append(Column(f"ret_{window}", Float, nullable=True))
        columns.append(Column(f"range_{window}", Float, nullable=True))
        if reaction_phase(window) == "min":
            columns.append(Column(f"vol_z_{window}", Float, nullable=True))
        columns.append(Column(f"cont_{window}", Boolean, nullable=True))
    return columns


# Jeden řádek per (event, symbol) — široký tvar (#998, ADR-0031). Do #998 byl
# řádek per okno (PK event_id, symbol, window_min); 8 oken × 2 symboly dělalo
# 145 B/okno vč. indexu a index bobtnal vkládáním denních oken doprostřed
# klíče. Široký tvar je bezeztrátový: `deferred`, `gex_regime` i `computed_at`
# jsou konstantní per (event, symbol, fáze) — ověřeno nad produkcí — proto
# stačí dvojice sloupců per fázi; `contaminated` se liší per okno, proto
# `cont_<w>`. Chybějící okno (bez baru) = NULL v celé čtveřici sloupců okna.
news_reactions = Table(
    "news_reactions",
    sentiment_metadata,
    Column("event_id", Integer, ForeignKey("news_events.id"), primary_key=True),
    Column("symbol", String(16), primary_key=True),
    # ret_<w>: změna ceny v bps od ts_event do konce okna; range_<w>: high−low
    # okna v bps; vol_z_<w>: objem okna vs. baseline téže minuty dne (jen
    # minutová okna, denní baseline nemá smysl); cont_<w>: do okna spadl jiný
    # event s importance ≥ 2 → mimo trénovací statistiky (SPEC 5.1). Bez toho
    # by se všem headlines z Fed day přičetl tentýž pohyb.
    *_reaction_window_columns(),
    # Trhy byly zavřené → okna se měří od prvního obchodovaného baru; dynamika
    # gapu na open je jiná, proto vlastní buckety. Per fáze (minutová/denní).
    Column("deferred_min", Boolean, nullable=True),
    Column("deferred_daily", Boolean, nullable=True),
    # GEX režim v čase eventu (#402): positive/negative; None = levels toho dne
    # nejsou (starší data mimo retenci) — podmíněná větev roste od nasazení
    Column("regime_min", String(16), nullable=True),
    Column("regime_daily", String(16), nullable=True),
    # Kdy byla fáze spočítána; NULL = fáze zatím nespočítaná (pending dotazy)
    Column("computed_at_min", DateTime(timezone=True), nullable=True),
    Column("computed_at_daily", DateTime(timezone=True), nullable=True),
)


@dataclass(frozen=True)
class ReactionWindow:
    """Jedno okno reakce — tvar, ve kterém s reakcí pracují joby i API.

    Odpovídá řádku `news_reactions` z doby před #998; široký řádek se na
    tenhle tvar rozkládá přes `unpivot_reaction` a z něj skládá přes
    `reaction_row_values`, aby konzumenti neznali názvy sloupců per okno.
    """

    window_min: int
    ret_bp: float
    range_bp: float
    vol_z: float | None
    contaminated: bool
    deferred: bool
    gex_regime: str | None
    computed_at: dt.datetime


def _window_column(prefix: str, window_min: int) -> Column[Any]:
    reaction_phase(window_min)  # validace okna — neznámé okno je chyba volajícího
    return news_reactions.c[f"{prefix}_{window_min}"]


def reaction_ret(window_min: int) -> Column[Any]:
    """Sloupec `ret_<w>` — návrat v bps daného okna."""
    return _window_column("ret", window_min)


def reaction_range(window_min: int) -> Column[Any]:
    return _window_column("range", window_min)


def reaction_vol_z(window_min: int) -> Column[Any] | None:
    """`vol_z_<w>` minutového okna; denní okna sloupec nemají → None."""
    if reaction_phase(window_min) != "min":
        return None
    return _window_column("vol_z", window_min)


def reaction_contaminated(window_min: int) -> Column[Any]:
    """Sloupec `cont_<w>` — NULL u nezměřeného okna, jinak příznak kontaminace."""
    return _window_column("cont", window_min)


def reaction_deferred(window_min: int) -> Column[Any]:
    return news_reactions.c[f"deferred_{reaction_phase(window_min)}"]


def reaction_regime(window_min: int) -> Column[Any]:
    return news_reactions.c[f"regime_{reaction_phase(window_min)}"]


def reaction_computed_at(window_min: int) -> Column[Any]:
    return news_reactions.c[f"computed_at_{reaction_phase(window_min)}"]


def reaction_row_values(windows: Sequence[ReactionWindow]) -> dict[str, object]:
    """Sloupce širokého řádku (bez `event_id`/`symbol`) z naměřených oken.

    Okna smí být z obou fází; okna téže fáze musí sdílet `deferred`,
    `gex_regime` a `computed_at` (invariant širokého tvaru) — porušení je
    chyba volajícího, ne něco, co by se mělo tiše zprůměrovat. Chybějící okno
    fáze zůstává NULL; fáze bez jediného okna nedostane ani `computed_at_*`.
    """
    values: dict[str, object] = {}
    phase_meta: dict[str, tuple[bool, str | None, dt.datetime]] = {}
    seen: set[int] = set()
    for window in windows:
        if window.window_min in seen:
            raise ValueError(f"Okno {window.window_min} je v řádku dvakrát")
        seen.add(window.window_min)
        phase = reaction_phase(window.window_min)
        meta = (window.deferred, window.gex_regime, window.computed_at)
        if phase_meta.setdefault(phase, meta) != meta:
            raise ValueError(
                f"Okno {window.window_min}: deferred/regime/computed_at se liší od "
                f"ostatních oken fáze {phase}"
            )
        values[f"ret_{window.window_min}"] = window.ret_bp
        values[f"range_{window.window_min}"] = window.range_bp
        values[f"cont_{window.window_min}"] = window.contaminated
        if phase == "min":
            values[f"vol_z_{window.window_min}"] = window.vol_z
        elif window.vol_z is not None:
            raise ValueError(f"Denní okno {window.window_min} nemá sloupec vol_z")
    for phase, (deferred, regime, computed_at) in phase_meta.items():
        values[f"deferred_{phase}"] = deferred
        values[f"regime_{phase}"] = regime
        values[f"computed_at_{phase}"] = computed_at
    return values


def unpivot_reaction(row: Mapping[Any, Any]) -> list[ReactionWindow]:
    """Široký řádek → naměřená okna (seřazená podle okna); NULL okna vynechá."""
    windows: list[ReactionWindow] = []
    for window_min in REACTION_ALL_WINDOWS:
        ret_bp = row.get(f"ret_{window_min}")
        if ret_bp is None:
            continue
        phase = reaction_phase(window_min)
        computed_at = row.get(f"computed_at_{phase}")
        if computed_at is None:
            raise ValueError(
                f"Řádek má ret_{window_min}, ale computed_at_{phase} je NULL — porušený invariant"
            )
        vol_z = row.get(f"vol_z_{window_min}") if phase == "min" else None
        windows.append(
            ReactionWindow(
                window_min=window_min,
                ret_bp=float(ret_bp),
                range_bp=float(row[f"range_{window_min}"]),
                vol_z=float(vol_z) if vol_z is not None else None,
                contaminated=bool(row[f"cont_{window_min}"]),
                deferred=bool(row[f"deferred_{phase}"]),
                gex_regime=row.get(f"regime_{phase}"),
                computed_at=computed_at,
            )
        )
    return windows


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
    # `news_reactions` jsou naměřená data. Starý tvar (řádek per okno, do #998)
    # se NEmigruje za běhu: 1,85 M řádků pivotuje samostatný skript s kontrolou
    # bezeztrátovosti. Proces nesmí do starého tvaru tiše psát ani vedle plné
    # staré tabulky založit prázdnou novou — proto tvrdá chyba s návodem.
    if "news_reactions" in existing:
        columns = {column["name"] for column in inspector.get_columns("news_reactions")}
        if "window_min" in columns or "computed_at_min" not in columns:
            raise LegacyNewsReactionsError(
                "Tabulka news_reactions je ve starém tvaru (řádek per okno, #998). "
                "Spusť `scripts/migrate_news_reactions_wide.py` (nejdřív se zálohou PG); "
                "do té doby se do reakcí nesmí psát."
            )
    sentiment_metadata.create_all(engine)
    ensure_news_reaction_spread_view(engine)


class LegacyNewsReactionsError(RuntimeError):
    """`news_reactions` je v tvaru před #998 — vyžaduje ruční migraci skriptem."""


def news_reaction_spread_view_sql() -> str:
    """Definice view `news_reaction_spread` bez úvodního `CREATE …`.

    Rozdíl reakcí NQ − ES per (event, okno) — míra „technologického
    charakteru" zprávy (ADR-0026, #579). View, ne materializace: obě reakce
    trvale žijí v `news_reactions`, druhá kopie by lhala při doplnění oken.
    Nad širokým tvarem (#998) je to UNION ALL per okno; sloupce a jejich typy
    drží tvar z ADR-0026, aby CREATE OR REPLACE v PG prošlo.
    """
    parts = []
    for window in REACTION_ALL_WINDOWS:
        parts.append(
            f"SELECT nq.event_id AS event_id, CAST({window} AS SMALLINT) AS window_min, "
            f"nq.ret_{window} - es.ret_{window} AS spread_bp, "
            f"nq.ret_{window} AS nq_ret_bp, es.ret_{window} AS es_ret_bp, "
            f"(nq.cont_{window} OR es.cont_{window}) AS contaminated "
            "FROM news_reactions nq "
            "JOIN news_reactions es ON es.event_id = nq.event_id AND es.symbol = 'ES' "
            f"WHERE nq.symbol = 'NQ' AND nq.ret_{window} IS NOT NULL "
            f"AND es.ret_{window} IS NOT NULL"
        )
    return "VIEW news_reaction_spread AS " + " UNION ALL ".join(parts)


def ensure_news_reaction_spread_view(engine: Engine) -> None:
    """Založí/obnoví view nad `news_reactions` (idempotentní).

    PG umí CREATE OR REPLACE, SQLite (testy) jen IF NOT EXISTS — definice se
    nemění, takže obojí je idempotentní.
    """
    from sqlalchemy import text

    view_sql = news_reaction_spread_view_sql()
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("CREATE OR REPLACE " + view_sql))
        else:
            conn.execute(text("CREATE VIEW IF NOT EXISTS" + view_sql.removeprefix("VIEW")))
