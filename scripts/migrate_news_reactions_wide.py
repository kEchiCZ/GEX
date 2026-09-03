"""Migrace `news_reactions` na široký tvar — jeden řádek per (event, symbol) (#998).

Starý tvar: řádek per (event, symbol, okno), PK (event_id, symbol, window_min),
1,85 M řádků / 268 MB. Nový tvar: sloupce per okno (`ret_<w>`, `range_<w>`,
`vol_z_<w>` jen minutová okna, `cont_<w>`) a dvojice `deferred_*`, `regime_*`,
`computed_at_*` per fázi (minutová/denní) — ~56 MB, viz ADR-0031.

Postup (jedna transakce, při JAKÉKOLI neshodě rollback a nic se nepřejmenuje):

1. předpoklady bezeztrátovosti nad starou tabulkou: jen známá okna, `vol_z`
   denních oken 100 % NULL (SPEC 5.1), `deferred`/`gex_regime`/`computed_at`
   konstantní per (event, symbol, fáze) — jinak by pivot tiše vybral jednu
   z hodnot,
2. nová tabulka pod dočasným jménem, naplnění pivotem v SQL,
3. ověření: počet řádků = počet dvojic (event, symbol); počet buněk per okno
   a per sloupec = počet starých řádků; součty `ret`/`range`/`vol_z` per okno
   s tolerancí 1e-6 relativně, počty kontaminovaných přesně; metadata fází
   přesně; a nakonec EXCEPT v obou směrech nad rozpivotovaným novým tvarem
   (hodnoty se kopírují, ne počítají — rovnost musí být přesná),
4. přejmenování: stará → `news_reactions_legacy_<datum>`, nová →
   `news_reactions`, PK/FK na kanonická jména, view `news_reaction_spread`
   znovu nad novým tvarem.

Stará tabulka se NIKDY nemaže — po ověření provozu ji smaže ručně uživatel
(`DROP TABLE news_reactions_legacy_<datum>`). Rollback = rename zpět (ADR-0031).

Spuštění (kontejner má balíky i skript; news-engine v novém kódu nad starým
tvarem nenastartuje, proto `run`, ne `exec`):
    docker compose run --rm news-engine python scripts/migrate_news_reactions_wide.py --dry-run
    docker compose run --rm news-engine python scripts/migrate_news_reactions_wide.py
"""

import argparse
import datetime as dt
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Connection

# Lokálně z repa; v kontejneru je balík nainstalovaný a cesty neexistují
for _sub in ("engine",):
    _path = Path(__file__).resolve().parents[1] / _sub / "src"
    if _path.is_dir():
        sys.path.insert(0, str(_path))

from gexlens_engine.storage.sentiment import (  # noqa: E402
    REACTION_ALL_WINDOWS,
    REACTION_DAILY_WINDOWS,
    news_events,
    news_reaction_spread_view_sql,
    news_reactions,
    reaction_phase,
)

logger = logging.getLogger("migrate_news_reactions_wide")

TABLE = "news_reactions"
TMP_TABLE = "news_reactions_wide_tmp"
VIEW = "news_reaction_spread"
#: Relativní tolerance součtů float8 — sčítá se v jiném pořadí než originál
SUM_REL_TOL = 1e-6
#: Hranice fází ve starém tvaru: denní okna jsou N × 1440
DAILY_MIN = min(REACTION_DAILY_WINDOWS)


class MigrationError(RuntimeError):
    """Neshoda kontroly — migrace se zastaví a nic se nepřejmenuje."""


# ── SQL (dialektově neutrální — PG v provozu, SQLite v testech) ─────────


def _phase_condition(phase: str) -> str:
    return f"window_min < {DAILY_MIN}" if phase == "min" else f"window_min >= {DAILY_MIN}"


def pivot_select_sql(source: str = TABLE) -> str:
    """SELECT, který ze starého tvaru složí široké řádky (1 per event × symbol).

    Per okno je ve starém tvaru nejvýš jeden řádek (PK), takže `max(... CASE)`
    hodnotu jen vybere, nic nepočítá. Bool přes CAST na INTEGER a zpět —
    PG nemá max(boolean) a SQLite nemá bool_or.
    """
    columns = ["event_id", "symbol"]
    exprs = ["event_id", "symbol"]
    for window in REACTION_ALL_WINDOWS:
        when = f"CASE WHEN window_min = {window} THEN"
        columns += [f"ret_{window}", f"range_{window}"]
        exprs += [f"max({when} ret_bp END)", f"max({when} range_bp END)"]
        if reaction_phase(window) == "min":
            columns.append(f"vol_z_{window}")
            exprs.append(f"max({when} vol_z END)")
        columns.append(f"cont_{window}")
        exprs.append(f"CAST(max({when} CAST(contaminated AS INTEGER) END) AS BOOLEAN)")
    phase_exprs = {
        "deferred": "CAST(max(CASE WHEN {cond} THEN CAST(deferred AS INTEGER) END) AS BOOLEAN)",
        "regime": "max(CASE WHEN {cond} THEN gex_regime END)",
        "computed_at": "max(CASE WHEN {cond} THEN computed_at END)",
    }
    for field in ("deferred", "regime", "computed_at"):
        for phase in ("min", "daily"):
            columns.append(f"{field}_{phase}")
            exprs.append(phase_exprs[field].format(cond=_phase_condition(phase)))
    # Pořadí musí odpovídat INSERT seznamu = definici tabulky; posun sloupce
    # by prohodil hodnoty a kontroly by to sice chytily, ale lepší hned
    if columns != pivot_column_names():
        raise MigrationError(f"Pořadí sloupců pivotu {columns} ≠ tabulce {pivot_column_names()}")
    select = ", ".join(f"{expr} AS {name}" for name, expr in zip(columns, exprs, strict=True))
    return f"SELECT {select} FROM {source} GROUP BY event_id, symbol"


def pivot_column_names() -> list[str]:
    return [column.name for column in news_reactions.columns]


def unpivot_select_sql(source: str) -> str:
    """Široký tvar zpět na řádky (event, symbol, okno, ret, range, vol_z, cont) — pro EXCEPT."""
    parts = []
    for window in REACTION_ALL_WINDOWS:
        vol_z = f"vol_z_{window}" if reaction_phase(window) == "min" else "NULL"
        parts.append(
            f"SELECT event_id, symbol, {window} AS window_min, ret_{window} AS ret_bp, "
            f"range_{window} AS range_bp, {vol_z} AS vol_z, "
            f"CAST(cont_{window} AS INTEGER) AS contaminated "
            f"FROM {source} WHERE ret_{window} IS NOT NULL"
        )
    return " UNION ALL ".join(parts)


# ── Kontroly ───────────────────────────────────────────────────────────


def _scalar(conn: Connection, sql: str) -> Any:
    return conn.execute(text(sql)).scalar()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MigrationError(message)


def check_preconditions(conn: Connection, source: str = TABLE) -> None:
    """Invarianty starého tvaru, bez kterých pivot není bezeztrátový."""
    known = ", ".join(str(w) for w in REACTION_ALL_WINDOWS)
    unknown = _scalar(conn, f"SELECT count(*) FROM {source} WHERE window_min NOT IN ({known})")
    _require(unknown == 0, f"{unknown} řádků s neznámým window_min (mimo {known})")
    nulls = _scalar(conn, f"SELECT count(*) FROM {source} WHERE ret_bp IS NULL OR range_bp IS NULL")
    _require(nulls == 0, f"{nulls} řádků s NULL ret_bp/range_bp — starý tvar to nepřipouští")
    # vol_z denních oken je podle SPEC 5.1 vždy NULL — široký tvar pro ně
    # sloupec nemá; vyplněná hodnota by se ztratila (#1001)
    daily_vol = _scalar(
        conn,
        f"SELECT count(*) FROM {source} WHERE window_min >= {DAILY_MIN} AND vol_z IS NOT NULL",
    )
    _require(daily_vol == 0, f"{daily_vol} denních oken má vyplněné vol_z — nemají kam jít")
    for phase in ("min", "daily"):
        cond = _phase_condition(phase)
        varying = _scalar(
            conn,
            f"SELECT count(*) FROM (SELECT event_id, symbol FROM {source} WHERE {cond} "
            "GROUP BY event_id, symbol HAVING count(DISTINCT computed_at) > 1 "
            "OR count(DISTINCT deferred) > 1 "
            "OR count(DISTINCT COALESCE(gex_regime, '~')) > 1) AS varying",
        )
        _require(
            varying == 0,
            f"{varying} dvojic (event, symbol) má ve fázi {phase} různé "
            "computed_at/deferred/gex_regime — pivot by ztratil informaci",
        )


def _except_count(conn: Connection, left: str, right: str) -> int:
    """Počet řádků `left` chybějících v `right` (množinově).

    Obě strany jdou do poddotazu: UNION ALL a EXCEPT mají v PG i SQLite
    stejnou prioritu zleva doprava, bez obalení by se `EXCEPT` vztáhl jen
    na první větev rozpivotování.
    """
    sql = (
        f"SELECT count(*) FROM (SELECT * FROM ({left}) AS l "
        f"EXCEPT SELECT * FROM ({right}) AS r) AS d"
    )
    return int(_scalar(conn, sql) or 0)


def _sum_matches(old: float | None, new: float | None) -> bool:
    if old is None or new is None:
        return old is None and new is None
    return abs(float(old) - float(new)) <= SUM_REL_TOL * max(1.0, abs(float(old)))


def verify(conn: Connection, source: str = TABLE, target: str = TMP_TABLE) -> dict[str, Any]:
    """Porovná starý a nový tvar; při neshodě MigrationError. Vrací naměřené počty."""
    old_rows = _scalar(conn, f"SELECT count(*) FROM {source}")
    old_pairs = _scalar(
        conn, f"SELECT count(*) FROM (SELECT DISTINCT event_id, symbol FROM {source}) AS p"
    )
    new_rows = _scalar(conn, f"SELECT count(*) FROM {target}")
    _require(new_rows == old_pairs, f"řádků nového tvaru {new_rows} ≠ dvojic {old_pairs}")

    cells = 0
    for window in REACTION_ALL_WINDOWS:
        old = conn.execute(
            text(
                "SELECT count(*) AS n, sum(ret_bp) AS ret, sum(range_bp) AS rng, "
                "count(vol_z) AS n_vol, sum(vol_z) AS vol, "
                "sum(CASE WHEN contaminated THEN 1 ELSE 0 END) AS cont "
                f"FROM {source} WHERE window_min = {window}"
            )
        ).one()
        vol_expr = f"vol_z_{window}" if reaction_phase(window) == "min" else "NULL"
        new = conn.execute(
            text(
                f"SELECT count(ret_{window}) AS n, sum(ret_{window}) AS ret, "
                f"sum(range_{window}) AS rng, count(range_{window}) AS n_rng, "
                f"count({vol_expr}) AS n_vol, sum({vol_expr}) AS vol, "
                f"count(cont_{window}) AS n_cont, "
                f"sum(CASE WHEN cont_{window} THEN 1 ELSE 0 END) AS cont "
                f"FROM {target}"
            )
        ).one()
        _require(new.n == old.n, f"okno {window}: ret buněk {new.n} ≠ starých řádků {old.n}")
        _require(new.n_rng == old.n, f"okno {window}: range buněk {new.n_rng} ≠ {old.n}")
        _require(new.n_cont == old.n, f"okno {window}: cont buněk {new.n_cont} ≠ {old.n}")
        _require(new.n_vol == old.n_vol, f"okno {window}: vol_z buněk {new.n_vol} ≠ {old.n_vol}")
        _require(_sum_matches(old.ret, new.ret), f"okno {window}: Σret {old.ret} vs {new.ret}")
        _require(_sum_matches(old.rng, new.rng), f"okno {window}: Σrange {old.rng} vs {new.rng}")
        _require(_sum_matches(old.vol, new.vol), f"okno {window}: Σvol_z {old.vol} vs {new.vol}")
        _require((old.cont or 0) == (new.cont or 0), f"okno {window}: kontaminovaných se liší")
        cells += int(new.n)
    _require(cells == old_rows, f"buněk celkem {cells} ≠ starých řádků {old_rows}")

    # Metadata fází přesně (množinově, oběma směry)
    for phase in ("min", "daily"):
        cond = _phase_condition(phase)
        old_meta = (
            f"SELECT event_id, symbol, computed_at, CAST(deferred AS INTEGER) AS deferred, "
            f"gex_regime FROM {source} WHERE {cond} GROUP BY event_id, symbol, computed_at, "
            "deferred, gex_regime"
        )
        new_meta = (
            f"SELECT event_id, symbol, computed_at_{phase}, CAST(deferred_{phase} AS INTEGER), "
            f"regime_{phase} FROM {target} WHERE computed_at_{phase} IS NOT NULL"
        )
        for label, left, right in (
            ("stará−nová", old_meta, new_meta),
            ("nová−stará", new_meta, old_meta),
        ):
            diff = _except_count(conn, left, right)
            _require(diff == 0, f"fáze {phase}: metadata {label} se liší v {diff} dvojicích")
        stale = _scalar(
            conn,
            f"SELECT count(*) FROM {target} WHERE computed_at_{phase} IS NULL AND ("
            + " OR ".join(
                f"ret_{w} IS NOT NULL" for w in REACTION_ALL_WINDOWS if reaction_phase(w) == phase
            )
            + ")",
        )
        _require(stale == 0, f"fáze {phase}: {stale} řádků má okna bez computed_at_{phase}")

    # Buňky přesně: každý starý řádek je v novém tvaru a naopak
    old_cells = (
        "SELECT event_id, symbol, window_min, ret_bp, range_bp, vol_z, "
        f"CAST(contaminated AS INTEGER) AS contaminated FROM {source}"
    )
    new_cells = unpivot_select_sql(target)
    for label, left, right in (
        ("stará−nová", old_cells, new_cells),
        ("nová−stará", new_cells, old_cells),
    ):
        diff = _except_count(conn, left, right)
        _require(diff == 0, f"buňky {label} se liší v {diff} případech")

    return {"old_rows": int(old_rows), "pairs": int(old_pairs), "new_rows": int(new_rows)}


# ── Provedení ──────────────────────────────────────────────────────────


def create_wide_table(conn: Connection, name: str = TMP_TABLE) -> None:
    """Založí širokou tabulku pod daným jménem podle definice v `sentiment.py`."""
    scratch = MetaData()
    news_events.to_metadata(scratch)  # cíl FK musí být ve stejném MetaData
    news_reactions.to_metadata(scratch, name=name).create(conn)


def fill_wide_table(conn: Connection, source: str = TABLE, target: str = TMP_TABLE) -> int:
    columns = ", ".join(pivot_column_names())
    result = conn.execute(text(f"INSERT INTO {target} ({columns}) {pivot_select_sql(source)}"))
    return int(result.rowcount or 0)


def _pg_size(conn: Connection, table: str) -> int:
    return int(_scalar(conn, f"SELECT pg_total_relation_size('{table}')") or 0)


def _pg_constraints(conn: Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        text("SELECT conname, contype FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
        {"t": table},
    ).fetchall()
    return [(str(r.conname), str(r.contype)) for r in rows]


def swap_tables(conn: Connection, legacy_name: str) -> None:
    """Přejmenuje starou tabulku na legacy, novou na kanonické jméno (jen PG)."""
    conn.execute(text(f"DROP VIEW IF EXISTS {VIEW}"))
    # Jména PK/FK musí být ve schématu unikátní (PK je zároveň index) —
    # nejdřív uvolnit kanonická jména na staré tabulce
    for conname, _ in _pg_constraints(conn, TABLE):
        conn.execute(
            text(f"ALTER TABLE {TABLE} RENAME CONSTRAINT {conname} TO {legacy_name}_{conname}")
        )
    conn.execute(text(f"ALTER TABLE {TABLE} RENAME TO {legacy_name}"))
    conn.execute(text(f"ALTER TABLE {TMP_TABLE} RENAME TO {TABLE}"))
    for conname, contype in _pg_constraints(conn, TABLE):
        canonical = f"{TABLE}_pkey" if contype == "p" else f"{TABLE}_event_id_fkey"
        if conname != canonical:
            conn.execute(text(f"ALTER TABLE {TABLE} RENAME CONSTRAINT {conname} TO {canonical}"))
    conn.execute(text("CREATE " + news_reaction_spread_view_sql()))
    conn.execute(text(f"ANALYZE {TABLE}"))


def run(database_url: str, *, dry_run: bool, today: dt.date | None = None) -> int:
    engine = create_engine(database_url)
    legacy_name = f"{TABLE}_legacy_{(today or dt.date.today()).strftime('%Y%m%d')}"
    # Inspekce vlastním spojením — na pracovním spojení by autobegin
    # zablokoval explicitní transakci níže
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if TABLE not in tables:
        logger.error("Tabulka %s neexistuje — není co migrovat", TABLE)
        return 2
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    if "computed_at_min" in columns and "window_min" not in columns:
        logger.info("%s už je v širokém tvaru — nic k provedení", TABLE)
        return 0
    if legacy_name in tables:
        logger.error("%s už existuje — dnešní migrace už proběhla? Nepřepisuji.", legacy_name)
        return 2
    is_pg = engine.dialect.name == "postgresql"
    with engine.connect() as conn:
        # Jedna transakce: při výjimce (vč. MigrationError) se všechno vrátí,
        # tabulka pod dočasným jménem nezůstane a nic není přejmenované
        transaction = conn.begin()
        try:
            size_before = _pg_size(conn, TABLE) if is_pg else 0
            logger.info("Před migrací: %s = %.1f MB", TABLE, size_before / 1e6)
            check_preconditions(conn)
            logger.info("Předpoklady bezeztrátovosti splněny")
            if TMP_TABLE in tables:
                # Pozůstatek přerušeného běhu — odvozená kopie, bezpečně pryč
                conn.execute(text(f"DROP TABLE {TMP_TABLE}"))
            create_wide_table(conn)
            started = dt.datetime.now(dt.UTC)
            inserted = fill_wide_table(conn)
            logger.info(
                "Pivot: %d řádků za %.1f s",
                inserted,
                (dt.datetime.now(dt.UTC) - started).total_seconds(),
            )
            counts = verify(conn)
            logger.info(
                "Ověření OK: %d starých řádků → %d širokých řádků (%d dvojic event×symbol)",
                counts["old_rows"],
                counts["new_rows"],
                counts["pairs"],
            )
            if is_pg:
                size_new = _pg_size(conn, TMP_TABLE)
                logger.info(
                    "Nový tvar: %.1f MB (−%.0f %%)",
                    size_new / 1e6,
                    100 * (1 - size_new / max(size_before, 1)),
                )
            if dry_run:
                logger.info("--dry-run: rollback, nic se nepřejmenovalo")
                transaction.rollback()
                # PG vrátí i DDL; SQLite (testy) DDL netransakčně commitne —
                # dočasnou kopii proto uklidit výslovně, ať dry-run nic nenechá
                with conn.begin():
                    conn.execute(text(f"DROP TABLE IF EXISTS {TMP_TABLE}"))
                return 0
            if not is_pg:
                raise MigrationError("Přejmenování s FK/PK je jen pro PostgreSQL")
            swap_tables(conn, legacy_name)
            size_after = _pg_size(conn, TABLE)
            transaction.commit()
        except BaseException:
            transaction.rollback()
            raise
        logger.info(
            "Hotovo: %s → %s, %s je široký tvar (%.1f MB). Starou tabulku smaž ručně "
            "až po ověření provozu: DROP TABLE %s",
            TABLE,
            legacy_name,
            TABLE,
            size_after / 1e6,
            legacy_name,
        )
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="pivot + kontroly, pak rollback")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Pořadí jako v ostatních skriptech: v kontejneru news-engine míří na
    # službu `postgres` jen odvozená proměnná; hodnota se nikdy nevypisuje
    url = os.environ.get("GEXLENS_NEWS_DATABASE_URL") or os.environ.get("GEXLENS_DATABASE_URL")
    if not url:
        logger.error("Chybí GEXLENS_NEWS_DATABASE_URL ani GEXLENS_DATABASE_URL")
        return 2
    try:
        return run(url, dry_run=args.dry_run)
    except MigrationError as error:
        logger.error("NESHODA KONTROLY — migrace zastavena, nic se nepřejmenovalo: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
