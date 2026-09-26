"""Reklasifikace historie klasifikátorem v2 a přepočet kontaminace K1 (#1293, ADR-0045).

Skript cílí na PostgreSQL, ale zápisy jsou dialektově neutrální — tady nad
SQLite: dry-run nic nezapíše, ostrý běh přidá verzi max+1 jen změněným
pravidlovým eventům (LLM, stín ngram beze změny), druhý běh má 0 změn,
rollback obnoví předchozí klasifikaci a kontaminace se shoduje s
`reactions.compute_reactions` včetně deferred reakce.
"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.meta import settings_table
from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_classifications,
    news_events,
    news_reactions,
    reaction_row_values,
)
from gexlens_news.bars import BarsRepository
from gexlens_news.reactions import DEFAULT_WINDOWS, compute_reactions
from gexlens_news.user_sources import SETTING_BLUESKY_AUTHORS

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reclassify_news_rules.py"
NOW = dt.datetime(2026, 9, 27, 10, 0, tzinfo=dt.UTC)
#: Úterý — trh otevřený celý den (bary 00:00–23:59)
T_OPEN = dt.datetime(2026, 9, 22, 12, 30, tzinfo=dt.UTC)
#: Den, kdy bary končí ve 12:00 (zavřený trh) — zpráva ve 13:00 je deferred
CLOSED_DAY = dt.date(2026, 9, 24)
T_CLOSED = dt.datetime(2026, 9, 24, 13, 0, tzinfo=dt.UTC)
YAHOO: dict[str, object] = {
    "feed": "https://finance.yahoo.com/news/rssindex",
    "link": "https://www.wsj.com/a",
}


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("reclassify_news_rules", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_bars(data_dir: Path, symbol: str, day: dt.date, *, until_hour: int = 24) -> None:
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
    rows = [
        {
            "ts_min": start + dt.timedelta(minutes=minute),
            "open": 7000.0,
            "high": 7001.0,
            "low": 6999.0,
            "close": 7000.0,
            "volume": 10.0,
        }
        for minute in range(until_hour * 60)
    ]
    pq.write_table(pa.Table.from_pylist(rows), directory / f"{day.isoformat()}.parquet")


def add_event(
    engine: Engine,
    ts: dt.datetime,
    title: str,
    *,
    source: str = "finnhub",
    kind: str = "headline",
    category: str | None,
    importance: int | None,
    sentiment_source: str = "rule",
    raw: dict[str, object] | None = None,
    direction: int = 0,
    strength: float = 0.0,
    shadow: bool = False,
) -> int:
    """Event + jeho klasifikační historie (rule v1, volitelně stín ngram v2)."""
    with engine.begin() as conn:
        key = conn.execute(
            insert(news_events).values(
                ts_event=ts,
                ts_ingested=ts,
                source=source,
                kind=kind,
                title=title,
                category=category,
                importance=importance,
                sentiment_dir=direction,
                sentiment_score=direction * strength,
                sentiment_source=sentiment_source,
                symbols=[],
                market_closed=False,
                dedup_hash=f"{source}-{title}-{ts.isoformat()}",
                raw=raw or {},
            )
        ).inserted_primary_key
        assert key is not None
        event_id = int(key[0])
        history = [(1, sentiment_source, category, importance)]
        if shadow:
            history.append((2, "ngram", "OTHER", 1))
        for version, source_name, cat, imp in history:
            conn.execute(
                insert(news_classifications).values(
                    event_id=event_id,
                    version=version,
                    source=source_name,
                    category=cat,
                    importance=imp,
                    direction=direction,
                    strength=strength,
                    created_at=ts,
                )
            )
    return event_id


def add_reaction(engine: Engine, event_id: int, symbol: str, *, deferred: bool, cont: bool) -> None:
    values = reaction_row_values(
        [
            ReactionWindow(
                window_min=window,
                ret_bp=1.0,
                range_bp=2.0,
                vol_z=None,
                contaminated=cont,
                deferred=deferred,
                gex_regime=None,
                computed_at=NOW,
            )
            for window in DEFAULT_WINDOWS
        ]
    )
    with engine.begin() as conn:
        conn.execute(insert(news_reactions).values(event_id=event_id, symbol=symbol, **values))


@pytest.fixture
def env(tmp_path: Path) -> tuple[Engine, Path, dict[str, int]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    settings_table.create(engine)  # seznam kurátorů (prázdný)
    data_dir = tmp_path / "data"
    for symbol in ("ES", "NQ"):
        write_bars(data_dir, symbol, T_OPEN.date())
        write_bars(data_dir, symbol, CLOSED_DAY, until_hour=12)
        write_bars(data_dir, symbol, CLOSED_DAY + dt.timedelta(days=1))
    ids = {
        # Agregátor jen události: FED/3 → FED/2; stín ngram v2 → nová verze 3
        "yahoo": add_event(
            engine,
            T_OPEN - dt.timedelta(hours=3),
            "Fed raises interest rates by a quarter point",
            source="rss_news",
            category="FED",
            importance=3,
            raw=YAHOO,
            direction=1,
            strength=0.4,
            shadow=True,
        ),
        # Sociální síť bez kurátora → strop 1
        "crowd": add_event(
            engine,
            T_OPEN - dt.timedelta(hours=2),
            "Iran strikes tanker near Hormuz",
            source="bluesky",
            kind="social",
            category="GEOPOLITICS",
            importance=3,
            raw={"did": "did:plc:x"},
            direction=-1,
            strength=0.4,
        ),
        # Beze změny (v2 dá totéž)
        "same": add_event(
            engine,
            T_OPEN - dt.timedelta(hours=1),
            "Chipmaker unveils new AI accelerator",
            source="alpaca",
            category="TECH",
            importance=1,
        ),
        # LLM verze se nepřepisuje
        "llm": add_event(
            engine,
            T_OPEN - dt.timedelta(minutes=30),
            "Why a doctor can cut her payroll tax",
            category="MACRO_LABOR",
            importance=3,
            sentiment_source="llm",
        ),
        # Kalendář: regex v1 dal High PPI importance 1 → FF impact USD High 3
        "cpi": add_event(
            engine,
            T_OPEN,
            "USD CPI m/m",
            source="forexfactory",
            kind="scheduled",
            category="MACRO_INFLATION",
            importance=1,
            raw={"impact": "High", "country": "USD"},
        ),
        # Pokrytí téže události minutu po kalendáři (stejná kategorie)
        "cpi_line": add_event(
            engine,
            T_OPEN + dt.timedelta(minutes=1),
            "USA CPI (MoM) For August 0.4% Vs 0.3% Est.",
            source="alpaca",
            category="MACRO_INFLATION",
            importance=3,
        ),
        # Jiná kategorie ve 12. minutě
        "oil": add_event(
            engine,
            T_OPEN + dt.timedelta(minutes=12),
            "Oil surges 5% on supply fears",
            category="ENERGY",
            importance=3,
        ),
        # Zpráva při zavřeném trhu (deferred) a jiná kategorie 30 min po ní
        "closed": add_event(
            engine,
            T_CLOSED,
            "Iran attacks Saudi oil facility with drones",
            category="GEOPOLITICS",
            importance=3,
        ),
        "closed_other": add_event(
            engine,
            T_CLOSED + dt.timedelta(minutes=30),
            "Fed holds rates steady",
            category="FED",
            importance=3,
        ),
    }
    # v1 kontaminace: řádek releasu kazil všechna okna CPI; deferred bez kontaminace
    add_reaction(engine, ids["cpi"], "ES", deferred=False, cont=True)
    add_reaction(engine, ids["closed"], "NQ", deferred=True, cont=False)
    return engine, data_dir, ids


def snapshot(engine: Engine) -> tuple[int, dict[int, tuple[str | None, int | None]]]:
    with engine.connect() as conn:
        versions = int(
            conn.execute(select(func.count()).select_from(news_classifications)).scalar_one()
        )
        rows = conn.execute(
            select(news_events.c.id, news_events.c.category, news_events.c.importance)
        ).all()
    return versions, {int(row.id): (row.category, row.importance) for row in rows}


def cont(engine: Engine, event_id: int, symbol: str) -> dict[int, bool]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(news_reactions).where(
                    news_reactions.c.event_id == event_id, news_reactions.c.symbol == symbol
                )
            )
            .mappings()
            .one()
        )
    return {window: bool(row[f"cont_{window}"]) for window in DEFAULT_WINDOWS}


def run(
    script: Any, engine: Engine, data_dir: Path, *, dry_run: bool, now: dt.datetime = NOW
) -> str:
    report = script.run(
        engine,
        data_dir,
        dry_run=dry_run,
        phases=("classify", "contamination"),
        batch=3,  # víc dávek i na malém vzorku
        now=now,
        log=lambda _message: None,
    )
    return "\n".join(report)


def test_dry_run_nic_nezapise_a_vypise_matici(env: tuple[Engine, Path, dict[str, int]]) -> None:
    engine, data_dir, _ = env
    script = load_script()
    before = snapshot(engine)
    with engine.connect() as conn:
        reactions_before = conn.execute(select(news_reactions)).all()

    report = run(script, engine, data_dir, dry_run=True)

    assert snapshot(engine) == before
    with engine.connect() as conn:
        assert conn.execute(select(news_reactions)).all() == reactions_before
    assert "DRY-RUN" in report
    assert "změna čehokoli **4**" in report  # yahoo, crowd, cpi, closed (viz ostrý běh)
    assert "Matice importance" in report and "kontaminace K1" in report


def test_ostry_beh_verze_max_plus_1_jen_zmenene_a_idempotence(
    env: tuple[Engine, Path, dict[str, int]],
) -> None:
    engine, data_dir, ids = env
    script = load_script()
    versions_before, before = snapshot(engine)

    run(script, engine, data_dir, dry_run=False)

    versions_after, after = snapshot(engine)
    changed = {key for key, event_id in ids.items() if before[event_id] != after[event_id]}
    assert changed == {"yahoo", "crowd", "cpi", "closed"}
    assert versions_after == versions_before + len(changed)
    assert after[ids["yahoo"]] == ("FED", 2)
    # Tanker = ENERGY (ENERGY před GEOPOLITICS), nekurátorovaný post strop 1
    assert after[ids["crowd"]] == ("ENERGY", 1)
    assert after[ids["closed"]] == ("ENERGY", 3)
    assert after[ids["cpi"]] == ("MACRO_INFLATION", 3)
    assert after[ids["llm"]] == before[ids["llm"]]  # LLM verze se nepřepisuje
    with engine.connect() as conn:
        newest = conn.execute(
            select(news_classifications)
            .where(news_classifications.c.event_id == ids["yahoo"])
            .order_by(news_classifications.c.version.desc())
        ).first()
    assert newest is not None
    # max+1 přes všechny zdroje (stín ngram má verzi 2), směr z poslední pravidlové verze
    assert (newest.version, newest.source, newest.direction) == (3, "rule", 1)
    assert float(newest.strength) == pytest.approx(0.4)

    # Druhý běh: nic nového
    report = run(script, engine, data_dir, dry_run=False, now=NOW + dt.timedelta(hours=1))
    assert "změna čehokoli **0**" in report
    assert "změněných řádků **0**" in report
    assert snapshot(engine)[0] == versions_after


def test_kontaminace_k1_shodna_s_compute_reactions(
    env: tuple[Engine, Path, dict[str, int]],
) -> None:
    engine, data_dir, ids = env
    script = load_script()

    run(script, engine, data_dir, dry_run=False)

    # Řádek releasu (stejná kategorie) už nekazí; ropa ve 12. minutě kazí 15 a 60
    assert cont(engine, ids["cpi"], "ES") == {1: False, 5: False, 15: True, 60: True}
    # Deferred: okna běží od prvního baru dalšího dne — zpráva jiné kategorie
    # 30 min po zprávě spadne do všech oken
    assert cont(engine, ids["closed"], "NQ") == {1: True, 5: True, 15: True, 60: True}

    # Parita s měřením jobu nad týmiž bary a kontaminujícími eventy
    bars = BarsRepository(data_dir)
    for event_id, ts, others, symbol in (
        (ids["cpi"], T_OPEN, [T_OPEN + dt.timedelta(minutes=12)], "ES"),
        (ids["closed"], T_CLOSED, [T_CLOSED + dt.timedelta(minutes=30)], "NQ"),
    ):
        measured = compute_reactions(
            ts,
            bars.load_range(symbol, ts - dt.timedelta(days=5), ts + dt.timedelta(days=5, hours=2)),
            other_event_ts=others,
        )
        assert {r.window_min: r.contaminated for r in measured} == cont(engine, event_id, symbol)


def test_rollback_obnovi_predchozi_klasifikaci_a_je_idempotentni(
    env: tuple[Engine, Path, dict[str, int]],
) -> None:
    engine, data_dir, ids = env
    script = load_script()
    _, before = snapshot(engine)
    run(script, engine, data_dir, dry_run=False)
    versions_after_run, _ = snapshot(engine)

    restored = script.rollback(engine, NOW, NOW + dt.timedelta(hours=2), batch=2)

    versions, after = snapshot(engine)
    assert restored == 4
    assert after == before
    # Historie se nemaže — rollback je další verze
    assert versions == versions_after_run + 4
    assert script.rollback(engine, NOW, NOW + dt.timedelta(hours=3), batch=2) == 0


def test_kurator_historie_podle_did_ze_seznamu(tmp_path: Path) -> None:
    """Posty kurátorů z doby před příznakem `raw.curated` (#1291) nedostanou strop 1:
    autor je v aktuálním seznamu kurátorů nebo už má post s příznakem."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    settings_table.create(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(settings_table).values(
                key=SETTING_BLUESKY_AUTHORS,
                value=["kurator.bsky.social", "#vypnuty.bsky.social", "rozbity.bsky.social"],
            )
        )
    title = "Oil prices surge 8% after attack on Saudi oil facilities"

    def post(did: str, ts: dt.datetime, *, flag: bool = False) -> int:
        raw: dict[str, object] = {"did": did, **({"curated": True} if flag else {})}
        return add_event(
            engine,
            ts,
            f"{title} ({did}, {ts.isoformat()})",
            source="bluesky",
            kind="social",
            category="ENERGY",
            importance=3,
            raw=raw,
        )

    ids = {
        "listed_old": post("did:plc:kurator", T_OPEN),
        "flag_new": post("did:plc:flag", NOW, flag=True),
        "flag_old": post("did:plc:flag", T_OPEN),
        "crowd": post("did:plc:dav", T_OPEN),
    }
    script = load_script()
    asked: list[str] = []

    def resolve(authors: Any) -> tuple[frozenset[str], list[str]]:
        asked.extend(authors)
        return frozenset({"did:plc:kurator", "did:plc:env"}), ["rozbity.bsky.social"]

    curated = script.load_curated(engine, " did:plc:env , ", resolve)
    # Vypnutá položka seznamu (#) se nepřekládá — stejně jako v collectoru
    assert asked == ["did:plc:env", "kurator.bsky.social", "rozbity.bsky.social"]
    assert curated.dids == {"did:plc:kurator", "did:plc:env", "did:plc:flag"}
    assert (curated.flagged, curated.listed) == (1, 2)

    report = "\n".join(
        script.run(
            engine,
            tmp_path,
            dry_run=False,
            phases=("classify",),
            batch=2,
            now=NOW,
            log=lambda _message: None,
            curated=curated,
        )
    )
    after = snapshot(engine)[1]
    assert after[ids["listed_old"]] == ("ENERGY", 3)
    assert after[ids["flag_old"]] == ("ENERGY", 3)
    assert after[ids["crowd"]] == ("ENERGY", 1)  # nekurátorovaný post strop 1
    assert "nepřeložené handly: rozbity.bsky.social" in report
    assert "Kurátoři Bluesky pro historii: 3 DID" in report


def test_cli_dry_run_ostry_beh_a_rollback_nad_sqlite(
    env: tuple[Engine, Path, dict[str, int]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Celý CLI (`main`) nad kopií v SQLite: dry-run → ostrý běh → 0 změn → rollback."""
    engine, data_dir, _ = env
    script = load_script()
    # Bez sítě: seznam kurátorů je prázdný, překlad handlů se nevolá
    monkeypatch.delenv("GEXLENS_NEWS_BLUESKY_CURATED_AUTHORS", raising=False)
    monkeypatch.setattr(script, "resolve_authors", lambda authors: (frozenset(), list(authors)))
    url = str(engine.url)
    _, before = snapshot(engine)
    common = ["--db", url, "--data-dir", str(data_dir)]

    dry = tmp_path / "dry.md"
    assert script.main([*common, "--dry-run", "--out", str(dry)]) == 0
    assert "změna čehokoli **4**" in dry.read_text(encoding="utf-8")
    assert snapshot(engine)[1] == before

    applied = tmp_path / "run.md"
    assert script.main([*common, "--out", str(applied)]) == 0
    text = applied.read_text(encoding="utf-8")
    run_at = text.split("RUN_AT pro rollback: `", 1)[1].split("`", 1)[0]
    assert snapshot(engine)[1] != before

    again = tmp_path / "again.md"
    assert script.main([*common, "--dry-run", "--out", str(again)]) == 0
    assert "změna čehokoli **0**" in again.read_text(encoding="utf-8")
    assert "změněných řádků **0**" in again.read_text(encoding="utf-8")

    assert script.main(["--db", url, "--rollback", run_at]) == 0
    assert snapshot(engine)[1] == before
