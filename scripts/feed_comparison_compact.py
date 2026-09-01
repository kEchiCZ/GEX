"""Zhuštění `feed_comparison` na denní souhrny (#965, varianta B).

Shadow sběr (#613) skončil 22. 8. a nechal po sobě 25,7 M řádků / 2,5 GB,
což je 79 % celé databáze. Data nejsou k zahození — stojí na nich prahy
fallbacku (#614), rozšíření pokrytí (#616) i prahy detektoru (#517 A) — ale
držet surové řádky kvůli hypotetické budoucí otázce je nepoměr k tomu, že se
zálohují při každé záloze PG.

**Co se zachová a co ne.** Percentily NEJSOU skládatelné: z denních mediánů
se celkový medián dopočítat nedá. Proto se ukládají DVĚ úrovně:

* denní řádky (`session_date` vyplněné) — pro budoucí hrubší analýzu,
* celkové řádky (`session_date IS NULL`) — spočítané ze SUROVÝCH dat, takže
  přesně ta čísla, která citují #614, #616 a #517, zůstanou ověřitelná
  i po smazání.

Co se ztrácí: dotaz na libovolný nový řez (jiné okno, jiná podmnožina seancí).
To je vědomá cena varianty B.

Postup je záměrně dvoufázový — `--build` nic nemaže:

    python scripts/feed_comparison_compact.py --build    # postaví souhrny
    python scripts/feed_comparison_compact.py --verify   # souhrn vs. surová data
    python scripts/feed_comparison_compact.py --drop     # teprve teď maže

`--drop` odmítne mazat, pokud `--verify` neprojde.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.config import load_settings  # noqa: E402

SCHEMA = """
create table if not exists feed_comparison_daily (
    session_date  date,
    symbol_root   varchar(16) not null,
    field         varchar(16) not null,
    n             bigint      not null,
    missing_ibkr  double precision,
    missing_tasty double precision,
    n_delta       bigint,
    med_delta     double precision,
    p95_delta     double precision,
    n_age         bigint,
    med_age       double precision,
    p95_age       double precision
)
"""

#: Denní i celkové řádky jedním dotazem. `grouping sets` dá obojí v jednom
#: průchodu nad 25 M řádky místo dvou — a hlavně ze stejného snímku dat.
AGGREGATE = """
insert into feed_comparison_daily (
    session_date, symbol_root, field, n, missing_ibkr, missing_tasty,
    n_delta, med_delta, p95_delta, n_age, med_age, p95_age
)
select ts::date                                                          as session_date,
       split_part(symbol, ' ', 1)                                        as symbol_root,
       field,
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
group by grouping sets ((ts::date, split_part(symbol, ' ', 1), field),
                        (split_part(symbol, ' ', 1), field))
"""

#: Kontrolní dotaz nad SUROVÝMI daty — musí dát totéž co celkové řádky souhrnu.
RAW_TOTALS = """
select split_part(symbol, ' ', 1)                                        as symbol_root,
       field,
       count(*)                                                          as n,
       percentile_cont(0.5)  within group (order by abs(delta))          as med_delta,
       percentile_cont(0.95) within group (order by abs(delta))          as p95_delta
from feed_comparison
group by 1, 2 order by 1, 2
"""

SUMMARY_TOTALS = """
select symbol_root, field, n, med_delta, p95_delta
from feed_comparison_daily
where session_date is null
order by symbol_root, field
"""


def build(conn) -> None:
    conn.execute(text(SCHEMA))
    conn.execute(text("delete from feed_comparison_daily"))
    conn.execute(text(AGGREGATE))
    dennich = conn.execute(
        text("select count(*) from feed_comparison_daily where session_date is not null")
    ).scalar()
    celkovych = conn.execute(
        text("select count(*) from feed_comparison_daily where session_date is null")
    ).scalar()
    print(f"Souhrn postaven: {dennich} denních řádků, {celkovych} celkových.")


def verify(conn) -> bool:
    """Souhrn musí dát tytéž hodnoty jako surová data. Jinak se nemaže."""
    raw = {(r.symbol_root, r.field): r for r in conn.execute(text(RAW_TOTALS))}
    summary = {(r.symbol_root, r.field): r for r in conn.execute(text(SUMMARY_TOTALS))}
    if not raw:
        print("Surová data jsou prázdná — není co ověřovat.")
        return False
    if raw.keys() != summary.keys():
        print(f"NESOUHLAS: klíče se liší (surová {len(raw)}, souhrn {len(summary)})")
        return False

    ok = True
    print(f"{'sym':<4} {'pole':<6} {'n':>9}  {'med|d|':>12}  {'p95|d|':>12}   stav")
    for key in sorted(raw):
        r, s = raw[key], summary[key]
        shoda = (
            int(r.n) == int(s.n)
            and _same(r.med_delta, s.med_delta)
            and _same(r.p95_delta, s.p95_delta)
        )
        ok = ok and shoda
        print(
            f"{key[0]:<4} {key[1]:<6} {int(r.n):>9}  {_f(r.med_delta):>12}  "
            f"{_f(r.p95_delta):>12}   {'OK' if shoda else 'NESOUHLAS'}"
        )
    print("\nVERDIKT:", "souhrn odpovídá surovým datům" if ok else "NESOUHLAS — nemazat!")
    return ok


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-9 * max(1.0, abs(a))


def _f(value: float | None) -> str:
    return format(value, ".5f") if value is not None else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Zhuštění feed_comparison (#965)")
    parser.add_argument("--build", action="store_true", help="postaví denní i celkové souhrny")
    parser.add_argument("--verify", action="store_true", help="porovná souhrn se surovými daty")
    parser.add_argument(
        "--drop", action="store_true", help="smaže surové řádky (jen po úspěšném ověření)"
    )
    args = parser.parse_args()
    if not (args.build or args.verify or args.drop):
        parser.error("zvol --build, --verify nebo --drop")

    engine = create_engine(load_settings().database_url)
    with engine.begin() as conn:
        if args.build:
            build(conn)
        if args.verify or args.drop:
            ok = verify(conn)
            if args.drop and not ok:
                print("\nMazání ZRUŠENO — souhrn neodpovídá surovým datům.")
                return 1
        if args.drop:
            pred = conn.execute(text("select count(*) from feed_comparison")).scalar()
            conn.execute(text("truncate table feed_comparison"))
            print(f"\nSmazáno {pred} surových řádků; souhrny zůstávají.")
            print("Místo se vrátí až po VACUUM FULL / autovacuum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
