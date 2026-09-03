"""Migrace `news_reactions` na široký tvar (#998): pivot + kontroly nad SQLite.

Skript cílí na PostgreSQL (rename s PK/FK), ale pivot i ověřovací logika jsou
dialektově neutrální — tady se ověřuje, že pivot je bezeztrátový a že kontroly
neshodu skutečně zachytí (a nic nepřejmenují).
"""

import datetime as dt
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_events,
    news_reactions,
    unpivot_reaction,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_news_reactions_wide.py"
T_MIN = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)
T_DAILY = dt.datetime(2026, 8, 17, 3, 0, tzinfo=dt.UTC)

# Starý tvar (do #998) — definice žije jen tady, produkční schéma ho už nezná
legacy_metadata = MetaData()
legacy_reactions = Table(
    "news_reactions",
    legacy_metadata,
    Column("event_id", Integer, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("window_min", SmallInteger, primary_key=True),
    Column("ret_bp", Float, nullable=False),
    Column("range_bp", Float, nullable=False),
    Column("vol_z", Float, nullable=True),
    Column("gex_regime", String(16), nullable=True),
    Column("contaminated", Boolean, nullable=False),
    Column("deferred", Boolean, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("migrate_news_reactions_wide", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_row(
    event_id: int,
    symbol: str,
    window_min: int,
    ret_bp: float,
    *,
    contaminated: bool = False,
    deferred: bool = False,
    regime: str | None = None,
    vol_z: float | None = None,
    computed_at: dt.datetime | None = None,
) -> dict[str, object]:
    daily = window_min >= 1440
    return {
        "event_id": event_id,
        "symbol": symbol,
        "window_min": window_min,
        "ret_bp": ret_bp,
        "range_bp": abs(ret_bp) * 1.5 + 0.25,
        "vol_z": vol_z,
        "gex_regime": regime,
        "contaminated": contaminated,
        "deferred": deferred,
        "computed_at": computed_at or (T_DAILY if daily else T_MIN),
    }


def sample_rows() -> list[dict[str, object]]:
    """Reprezentativní vzorek: obě fáze, chybějící okno 1, jen-denní dvojice."""
    rows: list[dict[str, object]] = []
    # Event 1, ES: kompletních 8 oken, vol_z jen v okně 1, kontaminace od 15 min
    for window in (1, 5, 15, 60):
        rows.append(
            legacy_row(
                1,
                "ES",
                window,
                0.1 * window - 3.3,
                contaminated=window >= 15,
                regime="negative",
                vol_z=-1.2 if window == 1 else None,
            )
        )
    for window in (1440, 2880, 7200, 14400):
        rows.append(legacy_row(1, "ES", window, 12.5 + window / 1000, regime="negative"))
    # Event 1, NQ: bez okna 1 (chyběl bar), deferred minutová fáze, bez režimu
    for window in (5, 15, 60):
        rows.append(legacy_row(1, "NQ", window, -7.75, deferred=True))
    for window in (1440, 2880, 7200, 14400):
        rows.append(legacy_row(1, "NQ", window, 0.0, deferred=True, regime="positive"))
    # Event 2, ES: jen denní fáze (event před pokrytím minutových barů)
    for window in (1440, 2880, 7200, 14400):
        rows.append(legacy_row(2, "ES", window, -1605.3))
    # Event 3, NQ: jen minutová fáze (denní ještě neuzavřená), přesné nuly
    for window in (1, 5, 15, 60):
        rows.append(legacy_row(3, "NQ", window, 0.0, regime="positive"))
    return rows


def make_legacy_db(tmp_path: Path, rows: list[dict[str, object]]) -> Engine:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.sqlite'}")
    news_events.create(engine)
    legacy_metadata.create_all(engine)
    with engine.begin() as conn:
        for event_id in sorted({int(str(row["event_id"])) for row in rows}):
            conn.execute(
                insert(news_events).values(
                    id=event_id,
                    ts_event=T_MIN,
                    ts_ingested=T_MIN,
                    source="finnhub",
                    kind="headline",
                    title=f"event {event_id}",
                    symbols=[],
                    market_closed=False,
                    dedup_hash=f"h{event_id}",
                    raw={},
                )
            )
        conn.execute(insert(legacy_reactions), rows)
    return engine


def test_ensure_schema_refuses_legacy_shape(tmp_path: Path) -> None:
    from gexlens_engine.storage.sentiment import LegacyNewsReactionsError

    engine = make_legacy_db(tmp_path, sample_rows())
    with pytest.raises(LegacyNewsReactionsError):
        ensure_sentiment_schema(engine)


def test_pivot_is_lossless_and_verifies(tmp_path: Path) -> None:
    script = load_script()
    rows = sample_rows()
    engine = make_legacy_db(tmp_path, rows)
    with engine.begin() as conn:
        script.check_preconditions(conn)
        script.create_wide_table(conn)
        assert script.fill_wide_table(conn) == 4  # (1,ES) (1,NQ) (2,ES) (3,NQ)
        counts = script.verify(conn)
    assert counts == {"old_rows": len(rows), "pairs": 4, "new_rows": 4}

    # Rozpivotování přes produkční helper vrátí přesně původní řádky
    wide = news_reactions.to_metadata(MetaData(), name=script.TMP_TABLE)
    with engine.connect() as conn:
        wide_rows = conn.execute(select(wide).order_by(wide.c.event_id, wide.c.symbol)).mappings()
        rebuilt: dict[tuple[object, object, object], ReactionWindow] = {
            (row["event_id"], row["symbol"], window.window_min): window
            for row in wide_rows
            for window in unpivot_reaction(row)
        }
    assert len(rebuilt) == len(rows)
    for row in rows:
        key = (row["event_id"], row["symbol"], row["window_min"])
        window = rebuilt[key]
        assert isinstance(window, ReactionWindow)
        assert window.ret_bp == row["ret_bp"]
        assert window.range_bp == row["range_bp"]
        assert window.vol_z == row["vol_z"]
        assert window.contaminated is row["contaminated"]
        assert window.deferred is row["deferred"]
        assert window.gex_regime == row["gex_regime"]
        assert window.computed_at.replace(tzinfo=dt.UTC) == row["computed_at"]
    # Dvojice jen s denní fází má minutová metadata NULL (a naopak)
    with engine.connect() as conn:
        only_daily = (
            conn.execute(select(wide).where(wide.c.event_id == 2, wide.c.symbol == "ES"))
            .mappings()
            .one()
        )
        only_min = (
            conn.execute(select(wide).where(wide.c.event_id == 3, wide.c.symbol == "NQ"))
            .mappings()
            .one()
        )
    assert only_daily["computed_at_min"] is None and only_daily["deferred_min"] is None
    assert only_daily["ret_5"] is None and only_daily["cont_5"] is None
    assert only_min["computed_at_daily"] is None and only_min["ret_1440"] is None


def test_preconditions_reject_lossy_input(tmp_path: Path) -> None:
    script = load_script()
    # vol_z u denního okna nemá v širokém tvaru sloupec — musí zastavit
    rows = sample_rows()
    rows[4]["vol_z"] = 0.4  # (1, ES, 1440)
    engine = make_legacy_db(tmp_path / "a", rows)
    with engine.begin() as conn, pytest.raises(script.MigrationError, match="vol_z"):
        script.check_preconditions(conn)
    # Různé computed_at v jedné fázi — pivot by jednu hodnotu zahodil
    rows = sample_rows()
    rows[1]["computed_at"] = T_MIN + dt.timedelta(minutes=1)  # (1, ES, 5)
    engine = make_legacy_db(tmp_path / "b", rows)
    with engine.begin() as conn, pytest.raises(script.MigrationError, match="computed_at"):
        script.check_preconditions(conn)
    # Neznámé okno
    rows = sample_rows()
    rows.append(legacy_row(3, "NQ", 7, 1.0))
    engine = make_legacy_db(tmp_path / "c", rows)
    with engine.begin() as conn, pytest.raises(script.MigrationError, match="window_min"):
        script.check_preconditions(conn)


def test_verify_detects_tampered_target(tmp_path: Path) -> None:
    script = load_script()
    engine = make_legacy_db(tmp_path, sample_rows())
    with engine.begin() as conn:
        script.create_wide_table(conn)
        script.fill_wide_table(conn)
        conn.execute(text(f"UPDATE {script.TMP_TABLE} SET ret_5 = ret_5 + 1 WHERE event_id = 1"))
        with pytest.raises(script.MigrationError, match="okno 5"):
            script.verify(conn)
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {script.TMP_TABLE} SET ret_5 = ret_5 - 1 WHERE event_id = 1"))
        script.verify(conn)  # zpět v pořádku
        conn.execute(text(f"DELETE FROM {script.TMP_TABLE} WHERE event_id = 3"))
        with pytest.raises(script.MigrationError, match="řádků nového tvaru"):
            script.verify(conn)


def test_run_is_idempotent_on_wide_schema(tmp_path: Path) -> None:
    """Nad už migrovanou DB skript nic nedělá a končí 0."""
    script = load_script()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'wide.sqlite'}")
    ensure_sentiment_schema(engine)
    assert script.run(str(engine.url), dry_run=False) == 0


def test_dry_run_leaves_legacy_untouched(tmp_path: Path) -> None:
    script = load_script()
    rows = sample_rows()
    engine = make_legacy_db(tmp_path, rows)
    assert script.run(str(engine.url), dry_run=True) == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM news_reactions")).scalar() == len(rows)
        tables = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).fetchall()
    assert script.TMP_TABLE not in {row.name for row in tables}
