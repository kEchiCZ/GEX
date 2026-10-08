"""Kolik setupů replaye vzniká v minutách sekundárního řetězu (#1464, nález #1081).

`backtest_setups.build_minutes` skládá minuty expirace ze všech jejích `levels`,
jenže ty píše i sekundární runtime (kadence 3 min) dřív, než se expirace stane
aktivní; živý `SetupEngine` běží jen nad aktivním řetězem. Skript přehraje
expirace baseline parametry (`SetupParams()`, bez Max Pain — bez PG) a spočítá
setupy vzniklé před settle předchozí expirace v archivu (= roll od #1331).

Jen čte partice `data/derived`. Řez `--to` drží měření opakovatelné (jen
uzavřené expirace):
    uv run python scripts/measure_replay_secondary_1464.py
        [--symbols ES,NQ] [--last 25] [--to 20261007]
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_setups as bt  # noqa: E402 — sousední skript, ne balík

from gexlens_engine.compute.settle import history_expiry_settle  # noqa: E402
from gexlens_engine.compute.setups import SetupParams  # noqa: E402


class _NoOiRepo:
    """Max Pain bez PG: `max_pain_for` výjimku převede na None."""

    def values_for(self, *_args: object) -> list[object]:
        raise RuntimeError("měření běží bez PG")


def measure(symbol: str, last: int, to: str) -> dict[str, float]:
    expiries = sorted(
        os.path.basename(p)
        for p in glob.glob(f"{bt.DATA}/{symbol}/*")
        if os.path.basename(p).isdigit() and os.path.basename(p) <= to
    )
    stats = {"expiries": 0, "setups": 0, "secondary": 0, "r_all": 0.0, "r_secondary": 0.0}
    for index in range(max(1, len(expiries) - last), len(expiries)):
        expiry = expiries[index]
        rolled_in = history_expiry_settle(expiries[index - 1], symbol)[0]
        minutes = bt.build_minutes(symbol, expiry, _NoOiRepo())
        if rolled_in is None or len(minutes) < 60:
            continue
        stats["expiries"] += 1
        for row in bt.replay(minutes, SetupParams(), expiry, symbol):
            r = row["r"] or 0.0
            stats["setups"] += 1
            stats["r_all"] += r
            if row["created"] < rolled_in:
                stats["secondary"] += 1
                stats["r_secondary"] += r
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--last", type=int, default=25, help="počet posledních expirací do řezu")
    parser.add_argument("--to", default="20261007", help="poslední expirace (YYYYMMDD)")
    args = parser.parse_args()

    print(f"Řez: {args.last} expirací do {args.to}, hranice = settle předchozí expirace")
    print("| symbol | expirací | setupů | před rollem | ΣR vše | ΣR před rollem |")
    print("|---|---|---|---|---|---|")
    for symbol in args.symbols.split(","):
        s = measure(symbol, args.last, args.to)
        share = s["secondary"] / s["setups"] if s["setups"] else 0.0
        print(
            f"| {symbol} | {s['expiries']:.0f} | {s['setups']:.0f} | "
            f"{s['secondary']:.0f} ({share:.0%}) | {s['r_all']:+.1f} | {s['r_secondary']:+.1f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
