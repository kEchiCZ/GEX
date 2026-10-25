"""Kolik setupů replaye vzniká mimo okno aktivního řetězu (#1464, nález #1081).

Do #1464 skládal `backtest_setups.build_minutes` minuty expirace ze všech jejích
`levels`, jenže ty píše i sekundární runtime (kadence 3 min) dřív, než se
expirace stane aktivní; živý `SetupEngine` běží jen nad aktivním řetězem.
Skript přehraje expirace baseline parametry (`SetupParams()`, bez Max Pain —
bez PG) a spočítá:
- setupy mimo okno: vzniklé před settle předchozí expirace v archivu (= roll
  od #1331) nebo od settle vlastní expirace — po opravě musí být 0;
- setupy večera před #1331: do nasazení #1331 (7. 10. 2026) roloval živý řetěz
  až s novým dnem v UTC, takže v okně po settle předchozí expirace psal E ještě
  sekundár. Konec večera = první minuta aktivní kadence (`bt.live_start`).
  Replay tyto minuty od #1464 vynechává — sloupec ověřuje, že je to 0
  (před vynecháním 22. 10.: ES 5 setupů, ΣR −5,0; NQ 10, ΣR −2,0).

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
    stats = {
        "expiries": 0,
        "setups": 0,
        "outside": 0,
        "r_all": 0.0,
        "r_outside": 0.0,
        "skipped_minutes": 0,
        "evenings": 0,
        "evening_minutes": 0,
        "evening": 0,
        "r_evening": 0.0,
    }
    for index in range(max(1, len(expiries) - last), len(expiries)):
        expiry = expiries[index]
        rolled_in = history_expiry_settle(expiries[index - 1], symbol)[0]
        settle = bt.expiry_end(symbol, expiry)
        skip = bt.WindowSkip()
        minutes = bt.build_minutes(symbol, expiry, _NoOiRepo(), skip=skip)
        if rolled_in is None or settle is None or len(minutes) < 60:
            continue
        stats["expiries"] += 1
        stats["skipped_minutes"] += skip.minutes
        start = bt.active_window(symbol, expiry)[0]
        levels = bt.load_series(f"{bt.DATA}/{symbol}/{expiry}/levels/*.parquet")
        evening_end = None if levels is None else bt.live_start(levels["ts_min"], start)
        if evening_end is not None and evening_end > start:
            stats["evenings"] += 1
            stats["evening_minutes"] += sum(1 for m in minutes if m.ts < evening_end)
        for row in bt.replay(minutes, SetupParams(), expiry, symbol):
            r = row["r"] or 0.0
            stats["setups"] += 1
            stats["r_all"] += r
            if row["created"] < rolled_in or row["created"] >= settle:
                stats["outside"] += 1
                stats["r_outside"] += r
            if evening_end is not None and row["created"] < evening_end:
                stats["evening"] += 1
                stats["r_evening"] += r
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--last", type=int, default=25, help="počet posledních expirací do řezu")
    parser.add_argument("--to", default="20261007", help="poslední expirace (YYYYMMDD)")
    args = parser.parse_args()

    print(f"Řez: {args.last} expirací do {args.to}, okno = (settle předchozí expirace, settle E]")
    print(
        "| symbol | expirací | setupů | mimo okno | ΣR vše | ΣR mimo okno | minut mimo okno "
        "| večerů před #1331 | minut večera | setupů večera | ΣR večera |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for symbol in args.symbols.split(","):
        s = measure(symbol, args.last, args.to)
        share = s["evening"] / s["setups"] if s["setups"] else 0.0
        print(
            f"| {symbol} | {s['expiries']:.0f} | {s['setups']:.0f} | {s['outside']:.0f} | "
            f"{s['r_all']:+.1f} | {s['r_outside']:+.1f} | {s['skipped_minutes']:.0f} | "
            f"{s['evenings']:.0f} | {s['evening_minutes']:.0f} | "
            f"{s['evening']:.0f} ({share:.0%}) | {s['r_evening']:+.1f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
