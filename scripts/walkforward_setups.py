"""Walk-forward kandidátních parametrů setupů — noční report a NÁVRH (#794 fáze 3).

Autonomie stupeň 1 (rozhodnutí uživatele v #794): skript **nic nezapisuje**.
Přehraje archiv produkčním detektorem (`backtest_setups.build_minutes` +
`replay`, parita ověřená ve fázi 1) pro každou sadu parametrů z předem
deklarovaného prostoru (`configs/walkforward_grid.json`), z denních ΣR udělá
walk-forward (`compute/walkforward.py`, protokol ADR-0034) a vypíše report.
Když kandidát splní všechny zábrany, report obsahuje i tělo pro
`POST /setups/params` — odeslat ho může jen člověk.

Baseline = platná verze parameter store (tabulka `setup_params`), bez DB
defaulty kódu. Bloky: per symbol a portfolio (ES+NQ sečtené per seance,
ADR-0030). Seance = expirace řetězu (0DTE), tj. jeden den archivu.

Použití (z hostitele je compose PG na portu 55432, ne na URL z .env — viz
`scripts/walkforward-nightly.ps1`, který URL složí bez výpisu hesla):
    uv run python scripts/walkforward_setups.py --db "postgresql+psycopg://gexlens:…@127.0.0.1:55432/gexlens"
        [--symbols ES,NQ] [--grid configs/walkforward_grid.json]
        [--in-sample 20] [--out-sample 5] [--metric sharpe|sum_r]
        [--out data/reports/walkforward-2026-09-09.md] [--json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_setups as bt  # noqa: E402 — sousední skript, ne balík

from gexlens_engine.compute.setups import (  # noqa: E402
    SETUP_MECHANICS_VERSION,
    SetupParams,
    params_from_dict,
    params_to_dict,
)
from gexlens_engine.compute.walkforward import (  # noqa: E402
    WalkForwardParams,
    WalkForwardResult,
    render_markdown,
    walk_forward,
)
from gexlens_engine.storage.oi_archive import OIEodRepository  # noqa: E402
from gexlens_engine.storage.setup_params_store import SetupParamsRepository  # noqa: E402

DEFAULT_GRID = Path(__file__).resolve().parent.parent / "configs" / "walkforward_grid.json"
BASELINE_NAME = "baseline"


def load_grid(path: Path, baseline: SetupParams) -> dict[str, SetupParams]:
    """Deklarovaný prostor kandidátů: jméno → SetupParams (baseline + override klíčů).

    Kandidát je vždy baseline s přepsanými klíči — jinak by změna platné verze
    tiše posunula i všechny kandidáty a report by srovnával jablka s hruškami.
    """
    spec = json.loads(path.read_text(encoding="utf-8"))
    base_dict = params_to_dict(baseline)
    candidates: dict[str, SetupParams] = {BASELINE_NAME: baseline}
    for entry in spec["candidates"]:
        name = str(entry["name"])
        if name in candidates:
            raise ValueError(f"Duplicitní kandidát {name!r} v {path}")
        overrides = dict(entry["params"])
        candidates[name] = params_from_dict({**base_dict, **overrides})
    return candidates


def session_date(expiry: str) -> dt.date:
    return dt.date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))


def daily_series(
    symbol: str, candidates: dict[str, SetupParams], repo: OIEodRepository
) -> dict[str, dict[dt.date, float]]:
    """Denní ΣR per kandidát z replaye všech expirací symbolu v archivu."""
    expiries = sorted(
        os.path.basename(p)
        for p in glob.glob(f"{bt.DATA}/{symbol}/*")
        if os.path.basename(p).isdigit()
    )
    series: dict[str, dict[dt.date, float]] = {name: {} for name in candidates}
    for expiry in expiries:
        minutes = bt.build_minutes(symbol, expiry, repo)
        if len(minutes) < 60:
            continue
        day = session_date(expiry)
        for name, params in candidates.items():
            rows = bt.replay(minutes, params)
            series[name][day] = float(sum(r["r"] for r in rows if r["r"] is not None))
    return series


def portfolio(
    per_symbol: dict[str, dict[str, dict[dt.date, float]]],
) -> dict[str, dict[dt.date, float]]:
    """ES+NQ sečtené per seance (ADR-0030: denní ΣR přes watchlist)."""
    combined: dict[str, dict[dt.date, float]] = defaultdict(lambda: defaultdict(float))
    for series in per_symbol.values():
        for name, values in series.items():
            for day, value in values.items():
                combined[name][day] += value
    return {name: dict(values) for name, values in combined.items()}


def proposal_payload(
    result: WalkForwardResult, candidates: dict[str, SetupParams], block: str
) -> dict[str, Any] | None:
    if result.proposal is None:
        return None
    return {
        "params": params_to_dict(candidates[result.proposal]),
        "note": (
            f"walk-forward {dt.date.today().isoformat()} ({block}): {result.proposal} — "
            f"{result.verdict}"
        ),
        "created_by": "ui",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--db", default=os.environ.get("GEXLENS_DATABASE_URL", ""))
    parser.add_argument("--data", default=bt.ROOT, help="kořen dat (derived/, snapshots/)")
    parser.add_argument("--grid", default=str(DEFAULT_GRID))
    parser.add_argument("--in-sample", type=int, default=WalkForwardParams.in_sample_days)
    parser.add_argument("--out-sample", type=int, default=WalkForwardParams.out_sample_days)
    parser.add_argument("--metric", choices=("sharpe", "sum_r"), default=WalkForwardParams.metric)
    parser.add_argument("--out", help="soubor pro markdown report (default stdout)")
    parser.add_argument("--json", action="store_true", help="místo markdownu JSON (folds, řady)")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db nebo GEXLENS_DATABASE_URL (Max Pain z OI archivu + baseline ze store)")
    bt.set_data_root(args.data)
    engine = create_engine(args.db)
    oi_repo = OIEodRepository(engine)

    # Baseline = platná verze store (fáze 2A); bez řádku defaulty kódu
    store = SetupParamsRepository(engine)
    store.ensure_schema()
    stored = store.latest()
    baseline = stored.params if stored is not None else SetupParams()
    baseline_label = f"store v{stored.version}" if stored is not None else "defaulty kódu"
    candidates = load_grid(Path(args.grid), baseline)
    wf_params = WalkForwardParams(
        in_sample_days=args.in_sample, out_sample_days=args.out_sample, metric=args.metric
    )

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    per_symbol = {symbol: daily_series(symbol, candidates, oi_repo) for symbol in symbols}
    blocks: dict[str, dict[str, dict[dt.date, float]]] = dict(per_symbol)
    if len(symbols) > 1:
        blocks["portfolio " + "+".join(symbols)] = portfolio(per_symbol)
    results = {
        block: walk_forward(series, baseline=BASELINE_NAME, params=wf_params)
        for block, series in blocks.items()
    }

    title = (
        f"Walk-forward parametrů setupů — {dt.date.today().isoformat()} · "
        f"mechanika v{SETUP_MECHANICS_VERSION} · baseline {baseline_label} · "
        f"IS {wf_params.in_sample_days} / OOS {wf_params.out_sample_days} seancí · "
        f"metrika {wf_params.metric} · kandidátů {len(candidates) - 1}"
    )
    if args.json:
        payload: dict[str, Any] = {
            "title": title,
            "baseline": baseline_label,
            "candidates": {name: params_to_dict(p) for name, p in candidates.items()},
            "blocks": {
                block: {
                    **{
                        k: v
                        for k, v in asdict(result).items()
                        if k not in ("oos", "oos_baseline", "folds")
                    },
                    "folds": [asdict(f) for f in result.folds],
                    "oos": {d.isoformat(): v for d, v in sorted(result.oos.items())},
                    "oos_baseline": {
                        d.isoformat(): v for d, v in sorted(result.oos_baseline.items())
                    },
                    "proposal_payload": proposal_payload(result, candidates, block),
                }
                for block, result in results.items()
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        text = render_markdown(results, title=title)
        proposals = {
            block: proposal_payload(result, candidates, block) for block, result in results.items()
        }
        text += "\n## Návrhy (NIC NEZAPSÁNO — autonomie stupeň 1)\n\n"
        any_proposal = False
        for block, body in proposals.items():
            if body is None:
                continue
            any_proposal = True
            text += (
                f"### {block}\n\n`POST /setups/params` po schválení člověkem:\n\n```json\n"
                + json.dumps(body, ensure_ascii=False, indent=2)
                + "\n```\n\n"
            )
        if not any_proposal:
            text += "Žádný kandidát nesplnil zábrany ADR-0034 — parametry se nemění.\n"

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"zapsáno: {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
