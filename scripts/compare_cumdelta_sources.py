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

Pokrytí trade větví (podíl objemu z tisků, fallback v RTH) se od #1071 čte
z partice `flow`; seance před #1071 ho nemají („—").

Shoda s cenou futures (verdikt #1018): korelace přírůstků obou řad s pohybem
ceny futures a s CVD futures (strana agresora od burzy) v oknech 1/5/15 min,
per seance a v souhrnu medián + počet seancí s kladnou korelací. Řada, která
jde soustavně proti ceně i proti CVD, čte stranu agresora obráceně.

Víkendy a svátky (`settle.is_trading_session`) se nevypisují — engine píše
ploché partice i při zavřeném trhu.

Hybrid režimu `dxfeed` (`--hybrid`, #1459): řada, jak ji režim `dxfeed` píše
dnes — tisky se stranou (stín) + midpoint tok kontrakt-minut bez jediného
tisku (`printvol.printed` 0 nebo NULL), složený z přírůstků `netflow` × delta
ze `snapshots` × multiplikátor. Metoda se ověřuje rekonstrukcí celé živé
midpoint řady proti uložené `flow_delta` (sloupec „rekonstrukce"; seance, kde
nesedí, do souhrnu hybridu nejdou). Tabulky shody s cenou dostanou sloupce
hybrid ~ cena, hybrid ~ CVD a samotný fallback ~ cena. Čte i `snapshots/`
(~0,9 mil. řádků za seanci ES) — pomalejší, proto jen na přepínač.

Seance, které do souhrnu NEPATŘÍ (v tabulce jsou, do souhrnu je vrátí
`--include-unusable`):
- 4. 9. 2026 — #1013 nasazeno v 09:03 CEST, seance neúplná;
- 8. 9. 2026 — PC vypnuté, market data přetažená na mobil, seance neúplná;
- automaticky každá seance s < 1 000 společnými minutami.
Víkendy a svátky (např. Labor Day 7. 9.) se nevypisují vůbec — viz níže.
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
    uv run python scripts/compare_cumdelta_sources.py --hybrid --from 2026-09-21 --to 2026-10-07
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import io
import json
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.storage.cumdelta_compare import (  # noqa: E402
    ALIGNMENT_WINDOWS,
    HybridCheck,
    PriceAlignment,
    SessionComparison,
    Summary,
    align_session,
    available_sessions,
    check_hybrid_session,
    compare_session,
    summarize,
)
from gexlens_engine.ticker import contract_multiplier  # noqa: E402

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
        "| Seance | Sym | Min. spol. (jen dx / jen live) | max \\|Δ\\| | max \\|Δ\\| % rozsahu | "
        "tvar. odch. | rozsah dx/live | r hladiny | r přírůstky | r přír. RTH | "
        "r přír. mimo RTH | max \\|Δ\\| RTH / mimo | opačné zn. | close dx / live | zn. close | "
        "tisky (bez strany / bez kontextu) | tisky z klasif. objemu | tisky z celého objemu | "
        "strukt. objem | fallback RTH (objem) | zahozeno bez Δ | "
        "řetěz dx/live | r přír. nejl. lag | poznámka |"
    )
    sep = "|" + "|".join(["---"] * 24) + "|"
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
                    _pct(r.live_printed_share_total),
                    _pct(r.live_structured_share),
                    _pct(r.live_fallback_share_rth),
                    "—" if r.live_dropped_no_delta is None else str(r.live_dropped_no_delta),
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
        "| Sym | Seancí v souhrnu | zn. close shoda | med. max \\|Δ\\| % rozsahu | "
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


#: Sloupce shody s cenou: (popisek, pole PriceAlignment)
_ALIGNMENT_FIELDS = (
    ("dx ~ cena", "dx_vs_price"),
    ("midpoint ~ cena", "live_vs_price"),
    ("CVD fut. ~ cena", "cvd_vs_price"),
    ("dx ~ CVD", "dx_vs_cvd"),
    ("midpoint ~ CVD", "live_vs_cvd"),
)
#: Navíc s `--hybrid` (#1459)
_HYBRID_ALIGNMENT_FIELDS = (
    ("hybrid ~ cena", "hybrid_vs_price"),
    ("hybrid ~ CVD", "hybrid_vs_cvd"),
    ("jen fallback ~ cena", "fallback_vs_price"),
)

Alignments = dict[tuple[str, dt.date], tuple[PriceAlignment, ...]]
Hybrids = dict[tuple[str, dt.date], HybridCheck]


def _alignment_fields(hybrid: bool) -> tuple[tuple[str, str], ...]:
    return _ALIGNMENT_FIELDS + (_HYBRID_ALIGNMENT_FIELDS if hybrid else ())


def alignment_table(
    results: Sequence[SessionComparison], alignments: Alignments, *, hybrid: bool = False
) -> str:
    """Per seance: korelace přírůstků s cenou a CVD futures pro každé okno."""
    fields = _alignment_fields(hybrid)
    labels = " | ".join(label for label, _ in fields)
    lines = [
        f"| Seance | Sym | okno | {labels} |",
        "|" + "|".join(["---"] * (3 + len(fields))) + "|",
    ]
    for r in results:
        for a in alignments.get((r.symbol, r.session), ()):
            cells = [_fmt(getattr(a, field), 3) for _, field in fields]
            row = [r.session.isoformat(), r.symbol, f"{a.window} min", *cells]
            lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def alignment_summary(
    usable: Sequence[SessionComparison],
    alignments: Alignments,
    symbols: Sequence[str],
    *,
    hybrid: bool = False,
) -> str:
    """Medián korelace přes použitelné seance a počet seancí s kladnou korelací."""
    fields = _alignment_fields(hybrid)
    labels = " | ".join(label for label, _ in fields)
    lines = [
        f"| Sym | okno | {labels} |",
        "|" + "|".join(["---"] * (2 + len(fields))) + "|",
    ]
    for symbol in symbols:
        rows = [alignments.get((r.symbol, r.session), ()) for r in usable if r.symbol == symbol]
        for i, window in enumerate(ALIGNMENT_WINDOWS):
            cells = []
            for _, field in fields:
                values = [getattr(row[i], field) for row in rows if len(row) > i]
                clean = [v for v in values if v is not None]
                if not clean:
                    cells.append("—")
                    continue
                positive = sum(1 for v in clean if v > 0)
                cells.append(f"{_fmt(statistics.median(clean), 3)} (+ {positive}/{len(clean)})")
            lines.append("| " + " | ".join([symbol, f"{window} min", *cells]) + " |")
    return "\n".join(lines)


def hybrid_table(results: Sequence[SessionComparison], hybrids: Hybrids) -> str:
    """Per seance: kontrola rekonstrukce a podíl midpointu v hybridu (#1459)."""
    header = (
        "| Seance | Sym | rekonstrukce r / chyba | fallback z objemu | fallback z \\|toku\\| | "
        "r přír. hybrid ~ dx | r přír. hybrid ~ midpoint | rozsah hybrid/dx | "
        "close hybrid / dx | zn. close | bez delty | poznámka |"
    )
    lines = [header, "|" + "|".join(["---"] * 12) + "|"]
    for r in results:
        h = hybrids.get((r.symbol, r.session))
        if h is None:
            continue
        recon_error = "—" if h.recon_rel_error is None else f"{h.recon_rel_error:.1e}"
        lines.append(
            "| "
            + " | ".join(
                [
                    h.session.isoformat(),
                    h.symbol,
                    f"{_fmt(h.recon_corr, 4)} / {recon_error}",
                    _pct(h.fallback_volume_share),
                    _pct(h.fallback_flow_share),
                    _fmt(h.hybrid_vs_dx, 3),
                    _fmt(h.hybrid_vs_live, 3),
                    _fmt(h.range_ratio_hybrid_dx, 2),
                    f"{_fmt(h.close_hybrid, 0)} / {_fmt(h.close_dx, 0)}",
                    _fmt_bool(h.sign_agree_close),
                    str(h.missing_delta),
                    f"MIMO SOUHRN: {h.unusable_reason}" if h.unusable_reason else "",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def hybrid_summary(
    usable: Sequence[SessionComparison], hybrids: Hybrids, symbols: Sequence[str]
) -> str:
    """Mediány hybridu přes seance použitelné v hlavním srovnání i v rekonstrukci."""
    lines = [
        "| Sym | seancí | max chyba rekonstrukce | med. fallback z objemu | "
        "med. fallback z \\|toku\\| | med. r hybrid ~ dx | med. r hybrid ~ midpoint | "
        "med. rozsah hybrid/dx | zn. close hybrid = dx |",
        "|" + "|".join(["---"] * 9) + "|",
    ]
    for symbol in symbols:
        rows = [
            h
            for r in usable
            if r.symbol == symbol
            and (h := hybrids.get((r.symbol, r.session))) is not None
            and h.unusable_reason is None
        ]
        if not rows:
            lines.append(f"| {symbol} | 0 |" + " — |" * 7)
            continue
        errors = [h.recon_rel_error for h in rows if h.recon_rel_error is not None]
        agree = sum(1 for h in rows if h.sign_agree_close)

        def med(values: list[float | None]) -> float | None:
            clean = [v for v in values if v is not None]
            return statistics.median(clean) if clean else None

        lines.append(
            "| "
            + " | ".join(
                [
                    symbol,
                    str(len(rows)),
                    f"{max(errors):.1e}" if errors else "—",
                    _pct(med([h.fallback_volume_share for h in rows])),
                    _pct(med([h.fallback_flow_share for h in rows])),
                    _fmt(med([h.hybrid_vs_dx for h in rows]), 3),
                    _fmt(med([h.hybrid_vs_live for h in rows]), 3),
                    _fmt(med([h.range_ratio_hybrid_dx for h in rows]), 2),
                    f"{agree}/{len(rows)}",
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
    snapshots_dir: Path | None = None,
) -> tuple[list[SessionComparison], list[Summary], list[SessionComparison], Alignments, Hybrids]:
    """(všechny seance, souhrny, použitelné seance, shoda s cenou, hybrid per seance).

    `snapshots_dir` zapne hybrid režimu `dxfeed` (#1459); bez něj je slovník prázdný.
    Shoda s cenou bez použitelného hybridu (rekonstrukce nesedí) sloupce hybridu nemá."""
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
    alignments: Alignments = {}
    hybrids: Hybrids = {}
    for r in results:
        parts = None
        if snapshots_dir is not None:
            multiplier = contract_multiplier(r.symbol)
            if multiplier is None:
                raise ValueError(f"{r.symbol}: neznámý multiplikátor — hybrid nejde složit")
            parts, check = check_hybrid_session(
                derived_dir, snapshots_dir, r.symbol, r.session, multiplier
            )
            if check is not None:
                hybrids[(r.symbol, r.session)] = check
                if check.unusable_reason is not None:
                    parts = None
        alignments[(r.symbol, r.session)] = align_session(
            derived_dir, r.symbol, r.session, hybrid=parts
        )
    return results, summaries, usable, alignments, hybrids


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
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="i hybrid režimu dxfeed: tisky + midpoint fallback kontrakt-minut bez tisku (#1459)",
    )
    parser.add_argument(
        "--snapshots",
        type=Path,
        default=None,
        help="adresář snapshots/ pro --hybrid (výchozí sourozenec adresáře derived/)",
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

    snapshots_dir: Path | None = None
    if args.hybrid:
        snapshots_dir = args.snapshots or derived_dir.parent / "snapshots"
        if not snapshots_dir.exists():
            print(f"Adresář {snapshots_dir} neexistuje (--hybrid)", file=sys.stderr)
            return 2

    results, summaries, usable, alignments, hybrids = run(
        derived_dir,
        args.symbols,
        date_from=args.date_from,
        date_to=args.date_to,
        include_unusable=args.include_unusable,
        rechain_series=args.rechain,
        snapshots_dir=snapshots_dir,
    )
    if not results:
        print(f"V {derived_dir} není žádná stínová partice cumdelta_dx", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "derived_dir": str(derived_dir),
            "sessions": [dataclasses.asdict(r) for r in results],
            "summary": [dataclasses.asdict(s) for s in summaries],
            "price_alignment": [
                {
                    "symbol": symbol,
                    "session": session,
                    "windows": [dataclasses.asdict(a) for a in rows],
                }
                for (symbol, session), rows in alignments.items()
            ],
            "hybrid": [dataclasses.asdict(h) for h in hybrids.values()],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
        return 0

    mode = "kumulativy znovu z přírůstků (--rechain)" if args.rechain else "uložené kumulativy"
    print(f"# CumΔ dxFeed (stín) vs. midpoint (živá) — {derived_dir} — {mode}\n")
    print("## Per seance\n")
    print(session_table(results))
    print("\n## Souhrn (jen použitelné seance, mediány)\n")
    print(summary_table(summaries))
    print("\n## Shoda s cenou futures — souhrn (medián r, + seancí s r > 0)\n")
    print(alignment_summary(usable, alignments, args.symbols, hybrid=args.hybrid))
    print("\n## Shoda s cenou futures — per seance\n")
    print(alignment_table(results, alignments, hybrid=args.hybrid))
    if args.hybrid:
        print("\n## Hybrid režimu dxfeed — souhrn (#1459)\n")
        print(hybrid_summary(usable, hybrids, args.symbols))
        print("\n## Hybrid režimu dxfeed — per seance\n")
        print(hybrid_table(results, hybrids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
