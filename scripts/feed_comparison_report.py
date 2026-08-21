"""Vyhodnocení shadow sběru (#613): odchylky a stáří per pole.

Z `feed_comparison` spočítá per pole: n, medián a p95 |odchylky|, podíl
vzorků s chybějící stranou, medián a p95 rozdílu stáří. Čísla jsou vstup
pro prahy hystereze fáze 2 (#614) — bez nich by se prahy střílely od boku.

**Agreguje se v databázi, ne v Pythonu.** Původní verze načítala všechny řádky
přes `fetchall()` a držela z nich další kopie v listech. Do 1. dne shadow sběru
to bylo neškodné; 17. 8. při 8,7 milionu řádcích to vyčerpalo paměť WSL VM
(6 GB) a zaseklo celý Docker daemon. Percentily umí Postgres sám a vrátí
jednotky řádků místo milionů.

Spuštění:  python scripts/feed_comparison_report.py [--days 7]
           [--symbol ES] [--sessions 2026-08-14,2026-08-17,...]
Prostředí: GEXLENS_DATABASE_URL (stejné jako engine).

Rozšíření pro vyhodnocení M7 fáze 2: `--symbol` (ES a NQ mají jinak velké
odchylky v bodech — smíchané by prahy nadhodnotily pro ES a podhodnotily
pro NQ) a `--sessions` (jen čisté seance; 18. 8. má v datech 8h výpadek TWS
a špinavé dny by p95 nafoukly). Seance = UTC datum řádku.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.config import load_settings  # noqa: E402

#: Pod tímhle počtem vzorků je p95 nesmyslné a raději se neukáže vůbec, než
#: aby se podle něj ladily prahy.
MIN_SAMPLES_FOR_P95 = 20

# `symbol` v tabulce je celý kontrakt („NQ 20260821 29810P") — pro rozpad per
# podklad se bere kořen před první mezerou
QUERY_TEMPLATE = """
    select {select_symbol} field,
           count(*)                                                          as n,
           count(*) filter (where value_ibkr is null)::float / count(*)      as missing_ibkr,
           count(*) filter (where value_tasty is null)::float / count(*)     as missing_tasty,
           count(delta)                                                      as n_delta,
           percentile_cont(0.5)  within group (order by abs(delta))          as med_delta,
           percentile_cont(0.95) within group (order by abs(delta))          as p95_delta,
           count(*) filter (where age_ibkr_ms is not null
                              and age_tasty_ms is not null)                  as n_age,
           percentile_cont(0.5)  within group (
               order by (age_tasty_ms - age_ibkr_ms))                        as med_age,
           percentile_cont(0.95) within group (
               order by (age_tasty_ms - age_ibkr_ms))                        as p95_age
    from feed_comparison
    where ts >= :since {where_symbol} {where_sessions}
    group by {group_symbol} field
    order by {group_symbol} field
"""


def fmt(value: float | None, spec: str = ".5f") -> str:
    return format(value, spec) if value is not None else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Report shadow porovnání feedů (#613)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--symbol", help="Jen jeden podklad (ES/NQ); default vše s rozpadem")
    parser.add_argument(
        "--sessions",
        help="Čárkami oddělená UTC data čistých seancí — ostatní dny se ignorují",
    )
    args = parser.parse_args()

    sessions = [day.strip() for day in args.sessions.split(",")] if args.sessions else None
    query = text(
        QUERY_TEMPLATE.format(
            select_symbol="split_part(symbol, ' ', 1) as sym," if args.symbol is None else "",
            group_symbol="split_part(symbol, ' ', 1)," if args.symbol is None else "",
            where_symbol="and split_part(symbol, ' ', 1) = :symbol" if args.symbol else "",
            where_sessions="and ts::date = any((:sessions)::date[])" if sessions else "",
        )
    )
    params: dict[str, object] = {"since": dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)}
    if args.symbol:
        params["symbol"] = args.symbol
    if sessions:
        params["sessions"] = sessions

    settings = load_settings()
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        rows = conn.execute(query, params).fetchall()

    total = sum(int(row.n) for row in rows)
    scope = args.symbol or "vsechny symboly"
    days_note = f", seance: {args.sessions}" if sessions else ""
    print(f"feed_comparison za poslednich {args.days} dni ({scope}{days_note}): {total} radku")
    print(
        "sym  pole      n        med|d|      p95|d|    chybi_ib  chybi_ty  "
        "stari_med_ms  stari_p95_ms"
    )
    for row in rows:
        # p95 se skrývá zvlášť podle počtu vzorků daného sloupce — polí s
        # měřeným stářím je míň než polí s hodnotou (OI má věk vypnutý, #664)
        p95_delta = row.p95_delta if int(row.n_delta) >= MIN_SAMPLES_FOR_P95 else None
        p95_age = row.p95_age if int(row.n_age) >= MIN_SAMPLES_FOR_P95 else None
        symbol = args.symbol or str(row.sym)
        print(
            f"{symbol:<4} {str(row.field):<9} {int(row.n):<8} "
            f"{fmt(row.med_delta):<11} {fmt(p95_delta):<9} "
            f"{float(row.missing_ibkr):<9.1%} {float(row.missing_tasty):<9.1%} "
            f"{fmt(row.med_age, '.0f'):<13} {fmt(p95_age, '.0f')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
