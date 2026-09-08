"""Měření #1018: stínové CumΔ z dxFeed tisků vs. živá midpoint řada (#615 krok 3/6).

Podklad pro rozhodnutí o přepnutí `GEXLENS_CUMDELTA_SOURCE=dxfeed`. Nad
uloženými řadami v `derived/` spočítá per obchodní seanci (Globex, #512:
22:00 UTC D−1 → 22:00 UTC D v létě) a symbol, jak se liší:

- stín `derived/{sym}/cumdelta_dx/{session}.parquet` — CumΔ ze všech tisků
  `TimeAndSale` se stranou od burzy (`cum_ring + cum_hot`; od #1013 je
  `cum_hot` nula a `cum_ring` celý řetěz),
- živá `derived/{sym}/flow/{utc_day}.parquet` — `cum_delta` z midpoint testu
  (partice po UTC dnech → seance se skládá z D−1 + D).

Metriky (podrobný výklad v `gexlens_engine.storage.cumdelta_compare`):
max |odchylka| v jednotkách CumΔ a v % rozsahu živé řady za den, Pearson
kumulativních řad, Pearson minutových PŘÍRŮSTKŮ (přísnější — tvar dne může
sedět i při jiném čtení jednotlivých minut), shoda znaménka na close seance,
podíl minut s opačným znaménkem, rozklad RTH (US 9:30–16:00 NY, DST-korektně)
vs. mimo RTH, poměr rozsahů obou řad a tvarová odchylka po přeškálování
(tisková řada nese jen outright agresi, midpoint klasifikuje veškerý objem —
měřítko se liší, korelace na něm nezávisí), počet přerušení řetězu kumulativu
(restart stínu bez navázání začíná od nuly; `--rechain` oba kumulativy postaví
znovu ze součtu uložených přírůstků), korelace přírůstků při posunu živé
řady o −3…+3 minut (nízké r při k=0 a vysoké při k=±1 = posun značek minut
mezi flush smyčkou stínu a cyklem enginu, ne jiný tok). Ze stínové partice se
přidávají součty `trades`, `unknown_side`, `dropped_no_context`, `volume`.

Co skript NEČTE: `printed_share`, `structured_volume`, `fallback_volume`,
`dropped_no_delta` z `/status.cumdelta_coverage` — ty se do parquetu
neukládají, existují jen v živém `/status` (per den od startu enginu) a pro
komentář v #615 se musí opsat odtud.

Seance, které do souhrnu NEPATŘÍ (v tabulce jsou, do souhrnu je vrátí
`--include-unusable`):
- 4. 9. 2026 — #1013 nasazeno v 09:03 CEST, seance neúplná;
- 7. 9. 2026 — Labor Day, zkrácená seance;
- 8. 9. 2026 — PC vypnuté, market data přetažená na mobil, seance neúplná;
- automaticky každá seance s < 1 000 společnými minutami.
Seance před 4. 9. mají stín dělený na zóny (ATM±15 prstenec + hot ±1) — jsou
označeny „zóny" a srovnávají ATM±15, ne celý řetěz.

Skript je nástroj k měření, ne verdikt: rozhodnutí je uživatele po ≥ 5
čistých seancích (11. 9. 2026 nebo později podle jejich počtu). Read-only,
do `data/` nic nezapisuje.

Spuštění (z kořene repa, čte `data/derived`; produkce mapuje ./data):
    uv run python scripts/compare_cumdelta_sources.py
    uv run python scripts/compare_cumdelta_sources.py --symbols ES NQ --from 2026-09-09
    uv run python scripts/compare_cumdelta_sources.py --rechain
    uv run python scripts/compare_cumdelta_sources.py --derived data-dev/derived --json
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.storage.cumdelta_compare import (  # noqa: E402
    SessionComparison,
    Summary,
    available_sessions,
    compare_session,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}{suffix}"


def _fmt_bool(value: bool | None) -> str:
    if value is None:
        return "—"
    return "ano" if value else "NE"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100.0 * value:.1f} %"


def _fmt_best_lag(r: SessionComparison) -> str:
    if r.best_lag is None:
        return "—"
    corr = dict(r.corr_increments_by_lag).get(r.best_lag)
    return f"{_fmt(corr, 3)} (k={r.best_lag:+d})"


def session_table(results: Sequence[SessionComparison]) -> str:
    header = (
        "| Seance | Sym | Min. spol. (jen dx / jen live) | max |Δ| | max |Δ| % rozsahu | "
        "tvar. odch. | rozsah dx/live | r hladiny | r přírůstky | r přír. RTH | "
        "r přír. mimo RTH | max |Δ| RTH / mimo | opačné zn. | close dx / live | zn. close | "
        "tisky (bez strany / bez kontextu) | pokrytí tisky | fallback RTH | "
        "řetěz dx/live | r přír. nejl. lag | poznámka |"
    )
    sep = "|" + "|".join(["---"] * 21) + "|"
    lines = [header, sep]
    for r in results:
        note_parts = list(r.notes)
        if r.unusable_reason and r.unusable_reason not in note_parts:
            note_parts.insert(0, f"MIMO SOUHRN: {r.unusable_reason}")
        lines.append(
            "| "
            + " | ".join(
                [
                    r.session.isoformat(),
                    r.symbol,
                    f"{r.minutes_common} ({r.minutes_dx_only} / {r.minutes_live_only})",
                    _fmt(r.total.max_abs_dev, 0),
                    _fmt(r.max_abs_dev_pct, 1, " %"),
                    _fmt(r.total.max_abs_dev_norm, 1, " %"),
                    _fmt(r.total.range_ratio, 3),
                    _fmt(r.total.corr_levels, 3),
                    _fmt(r.total.corr_increments, 3),
                    _fmt(r.rth.corr_increments, 3),
                    _fmt(r.off_rth.corr_increments, 3),
                    f"{_fmt(r.rth.max_abs_dev, 0)} / {_fmt(r.off_rth.max_abs_dev, 0)}",
                    _pct(r.total.sign_disagree_share),
                    f"{_fmt(r.close_dx, 0)} / {_fmt(r.close_live, 0)}",
                    _fmt_bool(r.sign_agree_close),
                    f"{r.dx_trades} ({r.dx_unknown_side} / {r.dx_dropped_no_context})",
                    # Pokrytí z živé partice (#1071); „—" = partice před #1071
                    _pct(r.live_printed_share),
                    _pct(r.live_fallback_share_rth),
                    f"{r.dx_chain_breaks} / {r.live_chain_breaks}",
                    _fmt_best_lag(r),
                    "; ".join(note_parts) if note_parts else "",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def summary_table(summaries: Sequence[Summary]) -> str:
    header = (
        "| Sym | Seancí v souhrnu | zn. close shoda | med. max |Δ| % rozsahu | "
        "med. tvar. odch. | med. rozsah dx/live | med. r hladiny | med. r přírůstky | "
        "med. r přír. RTH | med. r přír. mimo RTH | med. opačné zn. | "
        "med. r přír. nejl. lag | nejl. lagy |"
    )
    sep = "|" + "|".join(["---"] * 13) + "|"
    lines = [header, sep]
    for s in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    s.symbol,
                    str(s.sessions),
                    f"{s.sign_agree_close}/{s.sessions}",
                    _fmt(s.median_max_abs_dev_pct, 1, " %"),
                    _fmt(s.median_max_abs_dev_norm, 1, " %"),
                    _fmt(s.median_range_ratio, 3),
                    _fmt(s.median_corr_levels, 3),
                    _fmt(s.median_corr_increments, 3),
                    _fmt(s.median_corr_increments_rth, 3),
                    _fmt(s.median_corr_increments_off_rth, 3),
                    _pct(s.median_sign_disagree_share),
                    _fmt(s.median_corr_increments_best_lag, 3),
                    ", ".join("—" if k is None else f"{k:+d}" for k in s.best_lags) or "—",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _json_default(value: object) -> object:
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(f"Neserializovatelný typ {type(value).__name__}")


def run(
    derived_dir: Path,
    symbols: Sequence[str],
    *,
    date_from: dt.date | None,
    date_to: dt.date | None,
    include_unusable: bool,
    rechain_series: bool = False,
) -> tuple[list[SessionComparison], list[Summary]]:
    results: list[SessionComparison] = []
    for symbol in symbols:
        for session in available_sessions(derived_dir, symbol):
            if date_from is not None and session < date_from:
                continue
            if date_to is not None and session > date_to:
                continue
            results.append(
                compare_session(derived_dir, symbol, session, rechain_series=rechain_series)
            )
    usable = [r for r in results if include_unusable or r.unusable_reason is None]
    summaries = [summarize(usable, symbol) for symbol in symbols]
    return results, summaries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--symbols", nargs="+", default=["ES", "NQ"], help="výchozí ES NQ")
    parser.add_argument(
        "--derived",
        type=Path,
        default=REPO_ROOT / "data" / "derived",
        help="adresář derived/ (výchozí data/derived v kořeni repa)",
    )
    parser.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, default=None)
    parser.add_argument(
        "--include-unusable",
        action="store_true",
        help="započítat do souhrnu i seance označené jako neúplné/nepoužitelné",
    )
    parser.add_argument(
        "--rechain",
        action="store_true",
        help="kumulativy znovu ze součtu uložených přírůstků (bez restartů stínu bez navázání)",
    )
    parser.add_argument("--json", action="store_true", help="výstup jako JSON místo markdownu")
    args = parser.parse_args(argv)

    # Windows konzole má cp1250 a na „Δ" v hlavičce spadne — výstup je vždy UTF-8
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    derived_dir: Path = args.derived
    if not derived_dir.exists():
        print(f"Adresář {derived_dir} neexistuje", file=sys.stderr)
        return 2

    results, summaries = run(
        derived_dir,
        args.symbols,
        date_from=args.date_from,
        date_to=args.date_to,
        include_unusable=args.include_unusable,
        rechain_series=args.rechain,
    )
    if not results:
        print(f"V {derived_dir} není žádná stínová partice cumdelta_dx", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "derived_dir": str(derived_dir),
            "sessions": [dataclasses.asdict(r) for r in results],
            "summary": [dataclasses.asdict(s) for s in summaries],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
        return 0

    mode = "kumulativy znovu z přírůstků (--rechain)" if args.rechain else "uložené kumulativy"
    print(f"# CumΔ dxFeed (stín) vs. midpoint (živá) — {derived_dir} — {mode}\n")
    print("## Per seance\n")
    print(session_table(results))
    print("\n## Souhrn (jen použitelné seance, mediány)\n")
    print(summary_table(summaries))
    print(
        "\nPokrytí `printed_share` / `structured_volume` / `fallback_volume` / "
        "`dropped_no_delta` se do parquetu neukládá — opsat z živého `/status.cumdelta_coverage`."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
