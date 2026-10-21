"""Přehrání v5 × v6 nad stejnými seancemi — dopad přepnutí CumΔ na tisky (#1590, ADR-0057 bod 7).

Před bumpem v6 (#1403) ukáže, jak se změní množina setupů a ΣR, když replay
čte CumΔ z tisků (stín `cumdelta_dx` od 21. 9. 2026) místo midpointu. Obě
mechaniky jdou týmž `backtest_setups.build_minutes` + `replay` se stejnými
parametry; liší se jen zdroj CumΔ a mezera `cum_gap_before` (hranice seance
pod v6). Šablony bez CumΔ (T2 mimo kontra-režim, T3, T7) se mohou změnit jen
nepřímo — anti-spam, cooldown a blokace směru po sérii stopů.

Setup se páruje klíčem (šablona, směr, minuta vzniku). Report: per symbol
a šablona počty jen v5 / jen v6 / v obou, ΣR obou mechanik a podíl minut
s mezerou CumΔ pod v6. Dvě sady parametrů: produkční (platná verze store,
bez DB defaulty kódu) a všechny šablony zapnuté (i vypnutá T5). Nic nezapisuje.

    uv run python scripts/compare_mechanics_v5_v6.py --db "postgresql+psycopg://…@127.0.0.1:55432/gexlens"
        --data "D:/…/GEX/data" [--from 2026-09-21] [--to 2026-10-15] [--out report.md]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import glob
import os
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_setups as bt  # noqa: E402 — sousední skript, ne balík

from gexlens_engine.compute.setups import SetupParams  # noqa: E402
from gexlens_engine.storage.oi_archive import OIEodRepository  # noqa: E402
from gexlens_engine.storage.setup_params_store import SetupParamsRepository  # noqa: E402

Key = tuple[str, str, dt.datetime]


@dataclasses.dataclass
class Tally:
    """Součty jedné šablony (nebo celku) pro jednu sadu parametrů."""

    v5: int = 0
    v6: int = 0
    both: int = 0
    r_v5: float = 0.0
    r_v6: float = 0.0

    def add(self, rows5: dict[Key, dict], rows6: dict[Key, dict]) -> None:
        self.v5 += len(rows5)
        self.v6 += len(rows6)
        self.both += len(rows5.keys() & rows6.keys())
        self.r_v5 += sum(row["r"] for row in rows5.values() if row["r"] is not None)
        self.r_v6 += sum(row["r"] for row in rows6.values() if row["r"] is not None)


def keyed(rows: list[dict]) -> dict[Key, dict]:
    return {(row["template"], row["direction"], row["created"]): row for row in rows}


def compare(
    symbols: list[str],
    configs: dict[str, SetupParams],
    repo: OIEodRepository | None,
    start: dt.date,
    end: dt.date,
) -> tuple[dict[str, dict[str, dict[str, Tally]]], dict[str, tuple[int, int, int]]]:
    """→ (config → symbol → šablona → Tally, symbol → (seancí, minut, minut s mezerou v6))."""
    result: dict[str, dict[str, dict[str, Tally]]] = {
        name: {symbol: defaultdict(Tally) for symbol in symbols} for name in configs
    }
    coverage: dict[str, tuple[int, int, int]] = {}
    for symbol in symbols:
        sessions = minutes_total = gap_minutes = 0
        expiries = sorted(
            os.path.basename(p)
            for p in glob.glob(f"{bt.DATA}/{symbol}/*")
            if os.path.basename(p).isdigit()
        )
        for expiry in expiries:
            day = dt.date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
            if not start <= day <= end:
                continue
            minutes5 = bt.build_minutes(symbol, expiry, repo, prints=False)
            minutes6 = bt.build_minutes(symbol, expiry, repo, prints=True)
            if len(minutes5) < 60 or len(minutes6) < 60:
                continue
            sessions += 1
            minutes_total += len(minutes6)
            gap_minutes += sum(1 for m in minutes6 if m.cum_delta is None)
            for name, params in configs.items():
                rows5 = keyed(bt.replay(minutes5, params, expiry, symbol))
                rows6 = keyed(bt.replay(minutes6, params, expiry, symbol))
                per_template = result[name][symbol]
                for template in {key[0] for key in rows5.keys() | rows6.keys()}:
                    per_template[template].add(
                        {k: v for k, v in rows5.items() if k[0] == template},
                        {k: v for k, v in rows6.items() if k[0] == template},
                    )
        coverage[symbol] = (sessions, minutes_total, gap_minutes)
    return result, coverage


def render(
    result: dict[str, dict[str, dict[str, Tally]]],
    coverage: dict[str, tuple[int, int, int]],
    start: dt.date,
    end: dt.date,
    baseline_label: str,
) -> str:
    lines = [
        f"## Přehrání v5 × v6 — seance {start.isoformat()} až {end.isoformat()}",
        "",
        "v5 = CumΔ midpoint (dnešní živá řada), v6 = CumΔ z tisků (stín `cumdelta_dx`, "
        "ADR-0057 bod 7). Setup = (šablona, směr, minuta vzniku); ΣR jen uzavřených.",
        "",
        "| symbol | seancí | minut | mezera CumΔ v6 |",
        "|---|---|---|---|",
    ]
    for symbol, (sessions, minutes, gaps) in coverage.items():
        share = f"{gaps / minutes:.1%}" if minutes else "—"
        lines.append(f"| {symbol} | {sessions} | {minutes} | {gaps} ({share}) |")
    for name, per_symbol in result.items():
        lines += [
            "",
            f"### {name}"
            + (f" — baseline {baseline_label}" if name.startswith("produkční") else ""),
            "",
            "| symbol | šablona | v5 | v6 | v obou | jen v5 | jen v6 | ΣR v5 | ΣR v6 | Δ ΣR |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for symbol, per_template in per_symbol.items():
            total = Tally()
            for template, tally in sorted(per_template.items()):
                lines.append(_row(symbol, template, tally))
                total.v5 += tally.v5
                total.v6 += tally.v6
                total.both += tally.both
                total.r_v5 += tally.r_v5
                total.r_v6 += tally.r_v6
            lines.append(_row(symbol, "**celkem**", total))
    return "\n".join(lines) + "\n"


def _row(symbol: str, template: str, t: Tally) -> str:
    return (
        f"| {symbol} | {template} | {t.v5} | {t.v6} | {t.both} | {t.v5 - t.both} | "
        f"{t.v6 - t.both} | {t.r_v5:+.2f} | {t.r_v6:+.2f} | {t.r_v6 - t.r_v5:+.2f} |"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--db", default=os.environ.get("GEXLENS_DATABASE_URL", ""))
    parser.add_argument("--data", default=bt.ROOT, help="kořen dat (derived/, snapshots/)")
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=bt.SHADOW_FROM)
    parser.add_argument(
        "--to", dest="end", type=dt.date.fromisoformat, default=dt.date(2026, 10, 15)
    )
    parser.add_argument("--out", help="soubor pro markdown (default stdout)")
    args = parser.parse_args(argv)
    if args.start < bt.SHADOW_FROM:
        parser.error(
            f"--from {args.start} je před stínem tisků ({bt.SHADOW_FROM}) — v6 by byla mezera"
        )
    bt.set_data_root(args.data)
    engine = create_engine(args.db) if args.db else None
    repo = OIEodRepository(engine) if engine is not None else None
    stored = SetupParamsRepository(engine).latest() if engine is not None else None
    baseline = stored.params if stored is not None else SetupParams()
    baseline_label = (
        f"store v{stored.version} (mechanika v{stored.mechanics_version})"
        if stored is not None
        else "defaulty kódu"
    )
    configs = {
        "produkční parametry": baseline,
        "všechny šablony zapnuté": dataclasses.replace(baseline, disabled_templates=frozenset()),
    }
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    result, coverage = compare(symbols, configs, repo, args.start, args.end)
    text = render(result, coverage, args.start, args.end, baseline_label)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"zapsáno: {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
