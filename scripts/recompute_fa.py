"""Přepočet FA validace a kalibrace α nad finálními OI archivy (#1314).

Do #1314 běžela FA validace i kalibrace α hned po prvním OI archivu dne
v 00:00 UTC, tedy nad OI před publikací CME (07:00 CT). ΔOI vyšlo ≈ 0
(`fa_validation.doi_abs_sum` 0 u ES/NQ 22.–24. 9.), medián α kolem 0 a denní
dedup bod zamkl — pozdější obnova archivu po publikaci ho už nepřepočítala.
Symbol bez kalibrace navíc po bodu s mediánem ≤ 0 dostal `fa_alpha` s α 0
(NQ: FA vrstva fakticky vypnutá).

OI archiv dne se po publikaci přepisuje (#463), takže DB dnes nese finální
čísla a body jde spočítat znovu. Skript přehraje TYTÉŽ funkce jako engine
(`collect_fa_validation`, `collect_alpha_calibration`, žádná kopie logiky)
den po dni nad čistou paměťovou DB a porovná výsledek s produkcí:

- den archivu, jehož snímek (nebo snímek předchozího obchodního dne) vznikl
  před publikačním oknem, se nepřehrává — **neověřitelný** (obnova po
  publikaci neproběhla, finální čísla v DB nejsou);
- α se počítá od nuly v pořadí dnů (EMA); dny bez netflow partice (retence)
  bod nemají, report ukáže starou i novou α a počet dnů.

Režimy:
- **výchozí je dry-run**: report jako markdown do `{data}/reports/` (nebo
  `--out`), do DB nic nepíše;
- `--apply` po výslovném potvrzení (napsat `ano`, nebo `--yes`) přepíše body
  FA validace (upsert) a historii i stav α. Nic se nemaže: řádky, které
  přehrání nevytvořilo (neověřitelný den), zůstávají a report je vypíše.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.facalibration import AlphaCalibrationPoint
from gexlens_engine.compute.favalidation import FaValidationPoint
from gexlens_engine.compute.settle import is_trading_session
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import (
    FaAlphaRepository,
    collect_alpha_calibration,
    fa_alpha_history_table,
)
from gexlens_engine.storage.fa_validation import (
    FaValidationRecord,
    FaValidationRepository,
    collect_fa_validation,
    fa_validation_table,
)
from gexlens_engine.storage.oi_archive import OIEodRepository


@dataclass
class SymbolReplay:
    """Výsledek přehrání jednoho symbolu."""

    symbol: str
    replayed: list[dt.date] = field(default_factory=list)
    unverifiable: list[tuple[dt.date, str]] = field(default_factory=list)
    #: (expiry, day) → (stará |ΔOI|, nová |ΔOI|, stará open-ratio, nová open-ratio)
    validation: dict[tuple[str, dt.date], tuple[float | None, float, float | None, float]] = field(
        default_factory=dict
    )
    #: day → (starý medián, nový medián, nová α po bodu)
    alpha_points: dict[dt.date, tuple[float | None, float, float]] = field(default_factory=dict)
    old_alpha: float | None = None
    new_alpha: float | None = None
    new_days: int = 0
    memory: Engine | None = None


def published_days(
    oi_repo: OIEodRepository, settings: Settings, symbol: str
) -> tuple[list[dt.date], dict[dt.date, str]]:
    """Obchodní dny archivu a důvod, proč se který nedá ověřit."""
    days = sorted(day for day in oi_repo.days(symbol) if is_trading_session(day))
    problems: dict[dt.date, str] = {}
    for day in days:
        captured = oi_repo.captured_at(symbol, day)
        if captured is None:
            problems[day] = "snímek bez času pořízení (před #463)"
        elif captured < settings.oi_publication_utc(day):
            problems[day] = f"snímek z {captured:%H:%M} UTC, před publikací"
    return days, problems


def replay_symbol(
    symbol: str, settings: Settings, oi_repo: OIEodRepository, prod: Engine
) -> SymbolReplay:
    """Přehraje FA validaci a kalibraci α symbolu den po dni do paměťové DB."""
    result = SymbolReplay(symbol)
    memory = create_engine("sqlite://")
    result.memory = memory
    fa_memory = FaValidationRepository(memory)
    fa_memory.ensure_schema()
    alpha_memory = FaAlphaRepository(memory)
    alpha_memory.ensure_schema()

    days, problems = published_days(oi_repo, settings, symbol)
    for index, day in enumerate(days):
        previous = days[index - 1] if index else None
        reason = problems.get(day) or (problems.get(previous) if previous else None)
        if reason is not None:
            result.unverifiable.append((day, reason))
            continue
        collect_fa_validation(symbol, settings.snapshots_dir, oi_repo, fa_memory, day)
        collect_alpha_calibration(symbol, settings.derived_dir, oi_repo, alpha_memory, day)
        result.replayed.append(day)

    with prod.connect() as conn:
        old_validation = {
            (row.expiry, row.day): (row.doi_abs_sum, row.open_ratio)
            for row in conn.execute(
                select(fa_validation_table).where(fa_validation_table.c.symbol == symbol)
            )
        }
        old_history = {
            row.day: row.ratio_median
            for row in conn.execute(
                select(fa_alpha_history_table).where(fa_alpha_history_table.c.symbol == symbol)
            )
        }
    with memory.connect() as conn:
        for row in conn.execute(select(fa_validation_table)):
            old = old_validation.get((row.expiry, row.day))
            result.validation[(row.expiry, row.day)] = (
                old[0] if old else None,
                row.doi_abs_sum,
                old[1] if old else None,
                row.open_ratio,
            )
        for row in conn.execute(
            select(fa_alpha_history_table).order_by(fa_alpha_history_table.c.day)
        ):
            result.alpha_points[row.day] = (
                old_history.get(row.day),
                row.ratio_median,
                row.alpha_after,
            )
    old_state = FaAlphaRepository(prod).get(symbol)
    new_state = alpha_memory.get(symbol)
    result.old_alpha = old_state.alpha if old_state else None
    result.new_alpha = new_state.alpha if new_state else None
    result.new_days = new_state.days if new_state else 0
    return result


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def report_markdown(results: Sequence[SymbolReplay], generated: dt.datetime) -> str:
    lines = [f"# Přepočet FA validace a α (#1314) — {generated:%Y-%m-%d %H:%M} UTC", ""]
    for item in results:
        note = "" if item.new_alpha is not None else " — bez kalibrace platí konfigurace"
        lines += [
            f"## {item.symbol}",
            "",
            f"- α: {_fmt(item.old_alpha)} → **{_fmt(item.new_alpha)}** ({item.new_days} dnů){note}",
            f"- přehráno dnů: {len(item.replayed)}, neověřitelných: {len(item.unverifiable)}",
            "",
        ]
        if item.unverifiable:
            lines += ["### Neověřitelné dny (nepřehrávají se)", ""]
            lines += [f"- {day}: {reason}" for day, reason in item.unverifiable]
            lines.append("")
        lines += [
            "### FA validace",
            "",
            "| expirace | den | |ΔOI| staré | |ΔOI| nové | open-ratio staré | nové |",
            "|---|---|---|---|---|---|",
        ]
        for (expiry, day), (old_doi, new_doi, old_ratio, new_ratio) in sorted(
            item.validation.items(), key=lambda entry: (entry[0][1], entry[0][0])
        ):
            lines.append(
                f"| {expiry} | {day} | {_fmt(old_doi, 0)} | {new_doi:.0f} | "
                f"{_fmt(old_ratio)} | {new_ratio:.3f} |"
            )
        lines += [
            "",
            "### Kalibrace α",
            "",
            "| den | medián starý | nový | α po bodu |",
            "|---|---|---|---|",
        ]
        for day, (old_median, new_median, alpha_after) in sorted(item.alpha_points.items()):
            lines.append(f"| {day} | {_fmt(old_median)} | {new_median:.3f} | {alpha_after:.3f} |")
        lines.append("")
    return "\n".join(lines)


def apply(results: Sequence[SymbolReplay], prod: Engine) -> int:
    """Zapíše přehrané body (upsert); nic nemaže. Vrací počet zapsaných řádků."""
    written = 0
    fa_prod = FaValidationRepository(prod)
    alpha_prod = FaAlphaRepository(prod)
    for item in results:
        assert item.memory is not None
        with item.memory.connect() as conn:
            validation_rows = list(conn.execute(select(fa_validation_table)))
            history_rows = list(
                conn.execute(select(fa_alpha_history_table).order_by(fa_alpha_history_table.c.day))
            )
        for row in validation_rows:
            fa_prod.upsert(
                FaValidationRecord(
                    symbol=row.symbol,
                    expiry=row.expiry,
                    day=row.day,
                    next_day=row.next_day,
                    point=FaValidationPoint(
                        contracts=row.contracts,
                        volume_sum=row.volume_sum,
                        doi_abs_sum=row.doi_abs_sum,
                        doi_net_sum=row.doi_net_sum,
                        open_ratio=row.open_ratio,
                        spearman=row.spearman,
                        silent_share=row.silent_share,
                    ),
                )
            )
            written += 1
        # Historie α v pořadí dnů; stav symbolu jen s posledním bodem a jen
        # když přehrání nějakou α spočítalo (jinak platí konfigurace, #1314)
        for index, row in enumerate(history_rows):
            last = index == len(history_rows) - 1
            alpha_prod.record(
                item.symbol,
                row.day,
                row.expiry,
                AlphaCalibrationPoint(
                    samples=row.samples,
                    ratio_median=row.ratio_median,
                    ratio_buy=row.ratio_buy,
                    ratio_sell=row.ratio_sell,
                ),
                row.alpha_after,
                item.new_days,
                update_state=False,
            )
            written += 1
            if last and item.new_alpha is not None:
                alpha_prod.set_state(item.symbol, item.new_alpha, item.new_days)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", default=os.environ.get("GEXLENS_DATA_DIR", "data"))
    parser.add_argument("--symbols", default="ES,NQ", help="např. ES,NQ")
    parser.add_argument("--out", default="", help="adresář reportu (výchozí {data}/reports)")
    parser.add_argument("--apply", action="store_true", help="zapsat přepočet (po potvrzení)")
    parser.add_argument("--yes", action="store_true", help="s --apply bez interaktivní otázky")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")

    settings = Settings(data_dir=Path(args.data))
    prod = create_engine(args.db)
    oi_repo = OIEodRepository(prod)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = [replay_symbol(symbol, settings, oi_repo, prod) for symbol in symbols]

    generated = dt.datetime.now(dt.UTC)
    out_dir = Path(args.out) if args.out else Path(args.data) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"recompute-fa-{generated:%Y%m%d-%H%M}.md"
    path.write_text(report_markdown(results, generated), encoding="utf-8")
    for item in results:
        print(
            f"{item.symbol}: α {_fmt(item.old_alpha)} → {_fmt(item.new_alpha)} "
            f"({item.new_days} dnů), FA bodů {len(item.validation)}, "
            f"neověřitelných dnů {len(item.unverifiable)}"
        )
    print(f"Report {path}")
    if not args.apply:
        return 0
    if not args.yes:
        # Jen host/DB, přihlašovací údaje z URL se nevypisují
        answer = input(f"Zapsat přepočet do DB {args.db.split('@')[-1]}? Napiš 'ano': ")
        if answer.strip().lower() != "ano":
            print("Nezapsáno.")
            return 1
    written = apply(results, prod)
    print(f"Zapsáno {written} řádků.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
