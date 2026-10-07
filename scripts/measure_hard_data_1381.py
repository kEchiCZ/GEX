"""Tvrdá data I pro epiku #1385 (E-0.4a, #1392) — jen čtení PG a partic.

Oddíly: gamma režim seancí (`em_respect.negative_gamma_share`), zprávy podle
zdroje × měsíce, pokrytí `sentiment_daily` a retro přepočet SentIndexu po
reklasifikaci v2 (ADR-0045), bary podle `source` × měsíce a skoky ≥ 60 bp
(#1349) uvnitř seance i přes její hranici, seance s doplněnými bary, hloubka
archivu partic a `oi_eod`, velikost `data/` a PG.

Všechny SQL dotazy běží v jedné transakci `SET TRANSACTION READ ONLY`
(AGENTS.md: data v PG nejdou znovu pořídit); partice se jen čtou. Měsíc a
seance jsou obchodní den (`settle.trading_session_date`,
`settle.is_trading_session`), ne kalendářní.

Spuštění z hostitele (PG publikované na 55432; URL se nikdy nevypisuje):
    uv run python scripts/measure_hard_data_1381.py --db "$GEXLENS_HOST_DATABASE_URL" \\
        --data data --reclass-at 2026-09-26T22:14:46+00:00
`--reclass-at` = RUN_AT ostrého běhu `reclassify_news_rules.py`
(data/reports/reclass-run-20260927.txt). Výstup je Markdown na stdout.
"""

import argparse
import datetime as dt
import os
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from gexlens_engine.compute.expiry_calendar import roll_date
from gexlens_engine.compute.settle import (
    is_trading_session,
    quarterly_expiry,
    trading_session_date,
)
from gexlens_engine.storage.parquet_store import BAR_SOURCE_LIVE

#: Práh skoku mezi po sobě jdoucími bary téže seance (sken skoků z E-0.2).
JUMP_BP = 60.0
#: Seance s podílem minut pod flipem ≥ tomuto prahu je „převážně negativní".
NEGATIVE_MAJORITY = 0.5
#: Hlavní instrumenty s plným archivem barů; ad-hoc pohledy mají jen pár dní.
CORE_SYMBOLS = ("ES", "NQ")


def _month(day: dt.date) -> str:
    return f"{day:%Y-%m}"


def _table(header: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _day_of(path: Path) -> dt.date | None:
    try:
        return dt.date.fromisoformat(path.stem)
    except ValueError:
        return None


def gamma_regime(conn: Connection) -> str:
    rows = conn.execute(
        text("SELECT symbol, session_date, negative_gamma_share FROM em_respect")
    ).fetchall()
    cells: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for symbol, session, share in rows:
        cell = cells[(symbol, _month(session))]
        if not is_trading_session(session):
            cell["mimo obchodní den"] += 1
            continue
        cell["seance"] += 1
        if share is None:
            cell["bez levels"] += 1
        elif share >= NEGATIVE_MAJORITY:
            cell["neg"] += 1
            cell["z toho podíl ≥ 0,9"] += share >= 0.9
        else:
            cell["poz"] += 1
            cell["z toho podíl ≤ 0,1"] += share <= 0.1
    keys = ["seance", "bez levels", "neg", "z toho podíl ≥ 0,9", "poz", "z toho podíl ≤ 0,1"]
    keys.append("mimo obchodní den")
    body = [[sym, month, *(cells[(sym, month)][k] for k in keys)] for sym, month in sorted(cells)]
    return _table(["symbol", "měsíc", *keys], body)


def news_sources(conn: Connection) -> str:
    rows = conn.execute(
        text(
            "SELECT source, date_trunc('hour', ts_event) AS h, count(*)"
            " FROM news_events GROUP BY 1, 2"
        )
    ).fetchall()
    # Hodinový koš v UTC se mapuje na obchodní den přesně: Globex otevírá v celou hodinu CT.
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for source, hour, n in rows:
        counts[_month(trading_session_date(hour))][source] += n
    sources = sorted({s for c in counts.values() for s in c})
    body = [
        [m, *(counts[m][s] or "" for s in sources), sum(counts[m].values())] for m in sorted(counts)
    ]
    return _table(["měsíc", *sources, "celkem"], body)


def sentiment(conn: Connection, reclass_at: dt.datetime) -> str:
    out = []
    rows = conn.execute(
        text(
            "SELECT symbol, min(date), max(date), count(*), count(sigma),"
            " min(update_time), max(update_time) FROM sentiment_daily"
            " GROUP BY symbol ORDER BY symbol"
        )
    ).fetchall()
    out.append(
        _table(
            [
                "symbol",
                "od",
                "do",
                "dní",
                "se σ",
                "nejstarší update_time",
                "nejnovější update_time",
            ],
            [list(r) for r in rows],
        )
    )
    versions = conn.execute(
        text(
            "SELECT version, source, count(*), min(created_at), max(created_at)"
            " FROM news_classifications GROUP BY 1, 2 ORDER BY 1, 2"
        )
    ).fetchall()
    out.append(
        _table(["verze", "zdroj", "řádků", "první", "poslední"], [list(r) for r in versions])
    )
    batch = conn.execute(
        text("SELECT count(*) FROM news_classifications WHERE created_at = :t"), {"t": reclass_at}
    ).scalar_one()
    out.append(
        f"Řádků `news_classifications` s `created_at` = RUN_AT {reclass_at.isoformat()}: {batch}"
    )
    after = conn.execute(
        text(
            "SELECT symbol, count(*) FILTER (WHERE update_time >= :t),"
            " min(date) FILTER (WHERE update_time >= :t),"
            " max(date) FILTER (WHERE update_time >= :t),"
            " min(update_time) FILTER (WHERE update_time >= :t),"
            " count(*) FILTER (WHERE update_time < :t) FROM sentiment_daily"
            " WHERE date < :d GROUP BY symbol ORDER BY symbol"
        ),
        {"t": reclass_at, "d": reclass_at.date()},
    ).fetchall()
    out.append(
        _table(
            [
                "symbol",
                "dny před RUN_AT přepočtené po něm",
                "od",
                "do",
                "první update_time po RUN_AT",
                "dny před RUN_AT nepřepočtené",
            ],
            [list(r) for r in after],
        )
    )
    return "\n\n".join(out)


def storage(conn: Connection) -> str:
    oi = conn.execute(
        text(
            "SELECT symbol, min(date), max(date), count(DISTINCT date), count(*)"
            " FROM oi_eod GROUP BY symbol ORDER BY symbol"
        )
    ).fetchall()
    size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    table = _table(["symbol", "nejstarší den", "nejnovější", "dní", "řádků"], [list(r) for r in oi])
    return f"{table}\n\nVelikost databáze PG: {size / 1e9:.2f} GB"


def _roll_window(session: dt.date) -> bool:
    """Seance v týdnu rollu: od roll date − 3 dny do kvartální expirace."""
    for month in (3, 6, 9, 12):
        expiry = quarterly_expiry(session.year, month)
        if roll_date(expiry) - dt.timedelta(days=3) <= session <= expiry:
            return True
    return False


def bars(data: Path) -> str:
    out = []
    jumps: list[list[object]] = []
    boundary: list[list[object]] = []
    supplemented: list[str] = []
    for bars_dir in sorted(data.glob("derived/*/bars")):
        symbol = bars_dir.parent.name
        by_ts: dict[dt.datetime, tuple[float, str]] = {}
        duplicates = 0
        months: dict[str, Counter[str]] = defaultdict(Counter)
        for path in sorted(bars_dir.glob("*.parquet")):
            # Starší partice sloupec `source` nemají — znamená živou cestu jako NULL.
            columns = ["ts_min", "close"]
            if "source" in pq.read_schema(path).names:
                columns.append("source")
            table = pq.read_table(path, columns=columns).to_pylist()
            for rec in table:
                ts = rec["ts_min"]
                source = rec.get("source") or "NULL"
                if ts in by_ts:
                    duplicates += 1
                by_ts[ts] = (float(rec["close"]), source)
                months[_month(trading_session_date(ts))][source] += 1
        sources = sorted({s for c in months.values() for s in c})
        body = [[m, *(months[m][s] or "" for s in sources)] for m in sorted(months)]
        out.append(f"#### {symbol} (duplicitní minuty napříč particemi: {duplicates})\n\n")
        out[-1] += _table(["měsíc", *sources], body)
        filled: dict[dt.date, Counter[str]] = defaultdict(Counter)
        prev: tuple[dt.datetime, float, str] | None = None
        for ts in sorted(by_ts):
            close, source = by_ts[ts]
            session = trading_session_date(ts)
            if source not in ("NULL", BAR_SOURCE_LIVE):
                filled[session][source] += 1
            if prev is not None and close > 0 and prev[1] > 0:
                bp = 1e4 * (close / prev[1] - 1.0)
                if abs(bp) >= JUMP_BP:
                    same = trading_session_date(prev[0]) == session
                    gap = int((ts - prev[0]).total_seconds() // 60)
                    roll = "ano" if _roll_window(session) else ""
                    row = [symbol, ts.isoformat(), gap, f"{bp:+.0f}", prev[2], source, roll]
                    (jumps if same else boundary).append(row)
            prev = (ts, close, source)
        if symbol in CORE_SYMBOLS:
            kinds = sorted({s for c in filled.values() for s in c})
            rows = [[symbol, day, *(filled[day][k] or "" for k in kinds)] for day in sorted(filled)]
            supplemented.append(_table(["symbol", "seance", *kinds], rows))
    header = ["symbol", "ts (UTC)", "mezera min", "bp", "zdroj před", "zdroj po", "týden rollu"]
    out.append(f"#### Skoky ≥ {JUMP_BP:.0f} bp mezi po sobě jdoucími bary téže seance\n\n")
    out[-1] += _table(header, jumps)
    out.append(
        f"#### Skoky ≥ {JUMP_BP:.0f} bp přes hranici seance (první bar proti poslednímu)\n\n"
    )
    out[-1] += _table(header, boundary)
    out.append("#### Seance s doplněnými bary (ne živá cesta)\n\n" + "\n\n".join(supplemented))
    return "\n\n".join(out)


def archive_depth(data: Path) -> str:
    body = []
    for symbol_dir in sorted(p for p in data.glob("snapshots/*") if p.is_dir()):
        symbol = symbol_dir.name
        for kind, pattern in (
            ("snapshots", f"snapshots/{symbol}/*/*.parquet"),
            ("levels", f"derived/{symbol}/*/levels/*.parquet"),
            ("bars", f"derived/{symbol}/bars/*.parquet"),
        ):
            days = sorted({d for p in data.glob(pattern) if (d := _day_of(p)) is not None})
            if days:
                body.append([symbol, kind, days[0], days[-1], len(days)])
    return _table(["symbol", "řada", "nejstarší partice", "nejnovější", "dní"], body)


def disk(data: Path) -> str:
    sizes: Counter[str] = Counter()
    unreadable = 0
    for root, _dirs, files in os.walk(data):
        top = Path(root).relative_to(data).parts[:1]
        key = top[0] if top else "(kořen)"
        for name in files:
            try:
                sizes[key] += (Path(root) / name).stat().st_size
            except OSError:
                unreadable += 1
    body = [[k, f"{v / 1e9:.2f}"] for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])]
    body.append(["celkem", f"{sum(sizes.values()) / 1e9:.2f}"])
    if unreadable:
        body.append([f"nečitelných souborů (nezapočteno): {unreadable}", ""])
    return _table(["adresář", "GB"], body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--reclass-at", type=dt.datetime.fromisoformat, required=True)
    args = parser.parse_args()
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    as_of = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    print(f"as_of {as_of}\n")
    engine = create_engine(args.db)
    with engine.connect() as conn:
        # Autobegin: první příkaz transakce ji přepne do read only; konec = rollback.
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        print("### Gamma režim seancí (em_respect)\n\n" + gamma_regime(conn) + "\n")
        print("### Zprávy podle zdroje × měsíce\n\n" + news_sources(conn) + "\n")
        print("### Sentiment\n\n" + sentiment(conn, args.reclass_at) + "\n")
        print("### Archiv OI (oi_eod) a velikost PG\n\n" + storage(conn) + "\n")
        conn.rollback()
    print("### Bary podle source × měsíce\n\n" + bars(args.data) + "\n")
    print("### Hloubka archivu partic\n\n" + archive_depth(args.data) + "\n")
    print("### Velikost data/\n\n" + disk(args.data) + "\n")


if __name__ == "__main__":
    main()
