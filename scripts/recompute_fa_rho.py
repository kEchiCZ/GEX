"""Zpětný dopočet ρ FA odhadu z archivu OI a snímků (#1665, ADR-0061 bod 5).

Pod mechanikou v6 je FA odhad `OI + ρ·objem strany za seanci` a ρ kalibruje ranní
job (`storage/fa_calibration.collect_alpha_calibration`, větev v6). Stav jiného zdroje
(α z midpointu) se po bumpu bere jako nekalibrovaný, takže bez dopočtu by FA jela
až po `FA_MIN_DAYS` dnech. Skript přehraje TUTÉŽ funkci jako engine pod v6
(`cumdelta_prints=True`, žádná kopie logiky) den po dni nad paměťovou DB od `--from`:

- bod seance D: řetěz, který byl celou seanci sekundárem (#1580), medián ΔOI/V přes
  strany s V ≥ 25, ΔOI jen ze stran změřených po publikaci (#1605), V = maximum
  kumulativu ze snímků do řezu 16:00 CT; EMA a sevření jako engine;
- den bez bodu s důvodem z logu kalibrace (mezera snímků, konec před řezem, bez
  měření po publikaci, malý vzorek, medián ≤ 0);
- pokrytí seance pro každý den (poslední snímek, nejdelší mezera, strany bez objemu
  u řezu) a medián, který by den dal bez pravidla mezery — podklad prahů pro vlastníka;
- srovnání průměru denních mediánů s rozborem #1612 (NQ 0,339, ES 0,255).

Režimy:
- **výchozí je dry-run**: report jako markdown do `--out` (mimo `data/`), PostgreSQL
  jen v read-only transakci;
- `--apply` po výslovném potvrzení (napsat `ano`, nebo `--yes`) zapíše body do
  `fa_alpha_history` (upsert podle symbolu a dne — bod jiného zdroje téhož dne
  přepíše, report je vypíše) a stav `fa_alpha` se `source = 'volume'`. Opakovaný
  běh zapíše totéž. Spouští se v pauze s bumpem v6, po záloze PG a po dotazu
  vlastníka.

Spuštění (z kořene repa; URL DB jen z proměnné prostředí, nikdy ne výpisem):
    python -I scripts/recompute_fa_rho.py --data <data> --from 2026-09-25 --out <adresář>
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "engine" / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.facalibration import (  # noqa: E402
    MIN_VOLUME,
    SNAPSHOT_END_TOLERANCE_MIN,
    SNAPSHOT_GAP_MAX_MIN,
    AlphaCalibrationPoint,
    calibrate_rho,
)
from gexlens_engine.compute.settle import is_trading_session  # noqa: E402
from gexlens_engine.config import Settings  # noqa: E402
from gexlens_engine.storage.fa_calibration import (  # noqa: E402
    SOURCE_VOLUME,
    FaAlphaRepository,
    calibrated_chain,
    collect_alpha_calibration,
    fa_alpha_history_table,
    next_session,
    snapshot_session_volumes,
)
from gexlens_engine.storage.fa_validation import measured_oi_pair  # noqa: E402
from gexlens_engine.storage.oi_archive import OIEodRepository  # noqa: E402

#: Průměr denních mediánů ΔOI/objem z rozboru #1612 (bez 9. 10., issuecomment-6373807743)
REFERENCE_1612 = {"NQ": 0.339, "ES": 0.255}
CALIBRATION_LOGGER = "gexlens_engine.storage.fa_calibration"


class _Collect(logging.Handler):
    """Zprávy logu kalibrace během jednoho přehrání (důvod dne bez bodu)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@dataclass
class SessionRow:
    """Jedna seance D v reportu."""

    session: dt.date
    expiry: str | None
    last: dt.datetime | None = None
    longest_gap: int = 0
    unknown: int = 0
    #: strany s V ≥ 25 a ΔOI změřeným po publikaci
    samples: int = 0
    #: medián, který by den dal bez pravidla mezery (jen pokrytí konce a strany)
    median_without_gap: float | None = None
    problem: str | None = None
    #: z přehrání (tatáž funkce jako engine): bod, ρ po bodu, dnů
    median: float | None = None
    rho_after: float | None = None
    log: list[str] = field(default_factory=list)


@dataclass
class SymbolRho:
    """Výsledek přehrání jednoho symbolu."""

    symbol: str
    sessions: list[SessionRow] = field(default_factory=list)
    rho: float | None = None
    days: int = 0
    #: body v produkci, které `--apply` přepíše: den → (zdroj, medián)
    replaced: dict[dt.date, tuple[str | None, float]] = field(default_factory=dict)
    memory: Engine | None = None


def diagnose(
    symbol: str, settings: Settings, oi_repo: OIEodRepository, session: dt.date
) -> SessionRow:
    """Pokrytí seance a medián bez pravidla mezery — tytéž funkce jako job, bez zápisu."""
    base = settings.snapshots_dir / symbol
    chain = calibrated_chain([path for path in base.iterdir() if path.is_dir()], session)
    if chain is None:
        return SessionRow(session, None, problem="bez řetězu po seanci")
    row = SessionRow(session, chain.name)
    coverage = snapshot_session_volumes(chain, session)
    row.last, row.longest_gap, row.unknown = (
        coverage.last,
        coverage.longest_gap,
        len(coverage.unknown),
    )
    row.problem = coverage.problem()
    oi_before, oi_after, _ = measured_oi_pair(
        oi_repo, symbol, chain.name, next_session(session), session, settings.oi_publication_utc
    )
    doi = {key: oi_after[key] - oi_before[key] for key in oi_after.keys() & oi_before.keys()}
    row.samples = sum(1 for key, v in coverage.volumes.items() if v >= MIN_VOLUME and key in doi)
    without_gap = coverage.problem(gap_max=sys.maxsize)
    if without_gap is None:
        point = calibrate_rho(coverage.volumes, doi)
        row.median_without_gap = point.ratio_median if point is not None else None
    return row


def replay_symbol(
    symbol: str,
    settings: Settings,
    oi_repo: OIEodRepository,
    prod: Engine,
    first: dt.date,
    last: dt.date | None,
) -> SymbolRho:
    """Přehraje kalibraci ρ den po dni do paměťové DB a připraví řádky reportu."""
    result = SymbolRho(symbol)
    memory = create_engine("sqlite://")
    result.memory = memory
    alpha_memory = FaAlphaRepository(memory)
    alpha_memory.ensure_schema()
    days = sorted(day for day in oi_repo.days(symbol) if is_trading_session(day))
    sessions = [day for day in days if day >= first and (last is None or day <= last)]
    # Archiv dne po seanci: přehrání kalibruje seance [first, last], nic za `--to`
    end = next_session(last) if last is not None else None
    archive_days = [day for day in days if day > first and (end is None or day <= end)]
    logger = logging.getLogger(CALIBRATION_LOGGER)
    rows = {session: diagnose(symbol, settings, oi_repo, session) for session in sessions}
    for today in archive_days:
        handler = _Collect()
        logger.addHandler(handler)
        try:
            collect_alpha_calibration(
                symbol,
                settings.derived_dir,
                oi_repo,
                alpha_memory,
                today,
                publication_utc=settings.oi_publication_utc,
                snapshots_dir=settings.snapshots_dir,
                cumdelta_prints=True,
            )
        finally:
            logger.removeHandler(handler)
        for message in handler.messages:
            for session, row in rows.items():
                if f" {session.isoformat()}" in message:
                    row.log.append(message)
    with memory.connect() as conn:
        history = list(
            conn.execute(select(fa_alpha_history_table).order_by(fa_alpha_history_table.c.day))
        )
    for point in history:
        row = rows.get(point.day)
        if row is None:  # seance bez archivu v `days` (nemá být)
            continue
        row.median, row.rho_after = point.ratio_median, point.alpha_after
    state = alpha_memory.get(symbol)
    result.rho = state.alpha if state else None
    result.days = state.days if state else 0
    with prod.connect() as conn:
        produced = {point.day for point in history}
        for old in conn.execute(
            select(fa_alpha_history_table).where(fa_alpha_history_table.c.symbol == symbol)
        ):
            if old.day in produced:
                result.replaced[old.day] = (old.source, old.ratio_median)
    result.sessions = [rows[session] for session in sessions]
    return result


def _f(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def report_markdown(results: Sequence[SymbolRho], generated: dt.datetime) -> str:
    lines = [
        f"# Zpětný dopočet ρ FA (#1665, ADR-0061) — {generated:%Y-%m-%d %H:%M} UTC",
        "",
        f"Prahy: V ≥ {MIN_VOLUME:.0f}, mezera snímků ≤ {SNAPSHOT_GAP_MAX_MIN} min, konec "
        f"snímků a objem strany nejvýš {SNAPSHOT_END_TOLERANCE_MIN} min před koncem obchodu.",
        "",
    ]
    for item in results:
        medians = [row.median for row in item.sessions if row.median is not None]
        mean = statistics.fmean(medians) if medians else None
        cv = statistics.stdev(medians) / mean if mean and len(medians) > 1 and mean > 0 else None
        without_gap = [
            row.median_without_gap for row in item.sessions if row.median_without_gap is not None
        ]
        reference = REFERENCE_1612.get(item.symbol)
        lines += [
            f"## {item.symbol}",
            "",
            f"- ρ po dopočtu: **{_f(item.rho)}** ({item.days} dnů)",
            f"- bodů: {len(medians)} z {len(item.sessions)} seancí; průměr denních mediánů "
            f"{_f(mean)} (variační koeficient {_f(cv, 2)}), rozbor #1612 {_f(reference)}",
            f"- bez pravidla mezery by bod dalo {len(without_gap)} seancí, průměr "
            f"{_f(statistics.fmean(without_gap) if without_gap else None)}",
            "",
            "| seance | řetěz | poslední snímek | nejdelší mezera | stran | bez objemu u řezu "
            "| medián | ρ po bodu | medián bez pravidla mezery | důvod bez bodu |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in item.sessions:
            reason = row.problem or ""
            if row.median is None and not reason:
                reason = "; ".join(row.log[-1:])  # poslední zpráva kalibrace dne
            lines.append(
                f"| {row.session:%d. %m.} | {row.expiry or '—'} | "
                f"{'—' if row.last is None else f'{row.last:%H:%M}'} | {row.longest_gap} | "
                f"{row.samples} | {row.unknown} | {_f(row.median)} | {_f(row.rho_after)} | "
                f"{_f(row.median_without_gap)} | {reason.replace('|', '/')} |"
            )
        lines += ["", "### Co `--apply` přepíše v `fa_alpha_history` (klíč symbol, den)", ""]
        if not item.replaced:
            lines.append("- nic, dny bez bodu v produkci")
        lines += [
            f"- {day}: bod zdroje {source or 'midpoint (NULL)'} s mediánem {median:.3f}"
            for day, (source, median) in sorted(item.replaced.items())
        ]
        lines.append("")
    lines += [
        "## Co udělá `--apply`",
        "",
        "- `fa_alpha_history`: upsert dopočtených bodů v pořadí dnů se `source = 'volume'`.",
        "- `fa_alpha`: ρ a počet dnů z dopočtu se `source = 'volume'`, jen když dopočet ρ "
        "spočítal; jinak zůstává stávající stav.",
        "- Dny bez bodu se nezapisují; nic se nemaže.",
        "",
    ]
    return "\n".join(lines)


def apply(results: Sequence[SymbolRho], prod: Engine) -> int:
    """Zapíše dopočtené body a stav (upsert, idempotentní); vrací počet zapsaných řádků."""
    written = 0
    alpha_prod = FaAlphaRepository(prod)
    for item in results:
        assert item.memory is not None
        with item.memory.connect() as conn:
            history = list(
                conn.execute(select(fa_alpha_history_table).order_by(fa_alpha_history_table.c.day))
            )
        for row in history:
            alpha_prod.record(
                item.symbol,
                row.day,
                row.expiry,
                AlphaCalibrationPoint(
                    samples=row.samples,
                    ratio_median=row.ratio_median,
                    ratio_buy=None,
                    ratio_sell=None,
                ),
                row.alpha_after,
                item.days,
                source=SOURCE_VOLUME,
                update_state=False,
            )
            written += 1
        if item.rho is not None:
            alpha_prod.set_state(item.symbol, item.rho, item.days, source=SOURCE_VOLUME)
            written += 1
    return written


def database_engine(url: str, *, read_only: bool) -> Engine:
    """Engine DB; dry-run na PostgreSQL v read-only transakcích."""
    if read_only and url.startswith("postgresql"):
        return create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    return create_engine(url)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", default=os.environ.get("GEXLENS_DATA_DIR", "data"))
    parser.add_argument("--symbols", default="NQ,ES")
    parser.add_argument(
        "--from", dest="first", type=dt.date.fromisoformat, default=dt.date(2026, 9, 25)
    )
    parser.add_argument("--to", dest="last", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--out", required=True, help="adresář reportu (mimo data/)")
    parser.add_argument("--apply", action="store_true", help="zapsat body a stav (po potvrzení)")
    parser.add_argument("--yes", action="store_true", help="s --apply bez interaktivní otázky")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    # Přesměrovaný výstup má na Windows kódování cp1250 (vzor recompute_fa)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger(CALIBRATION_LOGGER).setLevel(logging.INFO)
    settings = Settings(data_dir=Path(args.data))
    out_dir = Path(args.out).resolve()
    if out_dir.is_relative_to(settings.data_dir.resolve()):
        parser.error("report nesmí do data/")
    prod = database_engine(args.db, read_only=not args.apply)
    oi_repo = OIEodRepository(prod)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = [
        replay_symbol(symbol, settings, oi_repo, prod, args.first, args.last) for symbol in symbols
    ]
    generated = dt.datetime.now(dt.UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"recompute-fa-rho-{generated:%Y%m%d-%H%M}.md"
    path.write_text(report_markdown(results, generated), encoding="utf-8")
    for item in results:
        print(
            f"{item.symbol}: ρ {_f(item.rho)} ({item.days} dnů), "
            f"bodů {sum(1 for row in item.sessions if row.median is not None)} "
            f"z {len(item.sessions)} seancí, přepíše {len(item.replaced)} bodů"
        )
    print(f"Report {path}")
    if not args.apply:
        return 0
    if not args.yes:
        # Jen host/DB, přihlašovací údaje z URL se nevypisují
        answer = input(f"Zapsat ρ do DB {args.db.split('@')[-1]}? Napiš 'ano': ")
        if answer.strip().lower() != "ano":
            print("Nezapsáno.")
            return 1
    written = apply(results, prod)
    print(f"Zapsáno {written} řádků.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
