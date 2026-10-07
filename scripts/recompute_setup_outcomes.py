"""Přepočet výsledků uzavřených setupů z 1min barů (#1320).

Živý SetupEngine do #1320 uzavíral setupy jen nad bary, které dorazily v dávce
minutového cyklu, a bez nich nad spotem v okamžiku cyklu; timeout bral cenu
minuty, kdy se zjistil. Výsledky v DB proto u části setupů neodpovídají cestě
ceny (NQ 1003 / ES 1004 3. 9.: cíl místo stopu po výpadku streamu, NQ 1373
27. 9.: spot stop o tick minul, timeouty 1106–1110 za cenu z 20:50 po restartu…).

Skript přehraje každý uzavřený setup TOUTÉŽ funkcí jako engine
(`compute.setups.walk_setup_path`, žádná kopie logiky) nad bary z partic
`derived/{sym}/bars` a porovná výsledek s DB. Cesta začíná za barem vstupu
(jeho close je entry): u nových řádků z kontextu (`entry_bar_ts`), u starších
se dohledá mezi bary N−2 … N+3 (opožděný cyklus nese i pozdější minuty, NQ 832
vznikl „ve 13:31“ nad barem 13:32). Setup nad spotem (vstup není close žádného
z nich) začíná za barem N−1 jako v enginu, pokud vstup leží v barech N−1..N.

Verdikty:
- **opravit** — jiný status nebo R (report ukáže i rozdíl USD na 1 kontrakt);
- **opravit čas** — výsledek sedí, liší se jen `closed_ts` (engine zásah
  zjistil později než bar, který úroveň zasáhl);
- **beze změny**;
- **vstup mimo bary** (výsledek se nepřepisuje): setup vznikl nad cenou,
  kterou bary v minutě vzniku nemají — zamrzlý spot při výpadku streamu
  (ES 1004 3. 9.: 7709 = close baru 14:17, trh už na 7711–7714), bary jiného
  kontraktu (#1232) nebo žádný bar. Návrh (od baru N−1 jako engine) je jen
  v reportu; `--exclude` takový setup vyřadí ze statistik (#1346). Kontroluje
  se i na kvartální expiraci (vstup na settle nezávisí);
- **neověřitelný** (nezapisuje se, návrh je jen v reportu): v cestě ceny chybí
  bary (díra nebo chybějící bary do settle; návrh je pak spočítaný přes díru),
  cesta obsahuje bary z jiného zdroje než IBKR (rekonstrukce z tasty
  `tasty_candle`; živý `ibkr` i historický `ibkr_hist` jsou týž zdroj), cena
  v cestě skočí na jinou hladinu (jiný kontrakt, #1232), bar vstupu je
  nejednoznačný a výsledek na něm závisí, nečitelná expirace, nebo expirace
  na kvartální datum, kde settle není jednoznačný (SOQ podle ADR-0039 bod 2,
  ale týdenní řetěz EW3/QN3 18. 9. se vypořádal odpoledne; #1366);
- **po settle** — setup vznikl po settle vlastní expirace (`born_after_settle`,
  #1324), přeskočí se.

Režimy:
- **výchozí je dry-run**: report jako markdown + CSV do `{data}/reports/`
  (nebo `--out`), do DB nic nepíše;
- `--apply` zapíše verdikty „opravit“ a „opravit čas“ po výslovném potvrzení
  (napsat `ano`). Neinteraktivně `--yes` jen spolu s `--approved <csv>` =
  CSV schváleného dry-runu: přepočet běží znovu a zapíše se jen tehdy, když
  se zapisované řádky se schváleným reportem shodují (mezitím doplněné bary
  by jinak zapsaly něco, co nikdo neviděl). Nic nemaže: přepíše status,
  `closed_ts`, `outcome_r` a MFE/MAE z téže cesty ceny, původní hodnoty uloží
  do `context.outcome_correction` (`SetupsRepository.correct_outcome`).
  Opakovaný běh je idempotentní;
- `--exclude` (#1346, rozhodnutí uživatele 6. 10. 2026) zapíše verdiktům
  „vstup mimo bary“ trvalou značku `context.excluded` = {reason
  `vstup_mimo_bary`, detail = důvod z reportu, ts} (`SetupsRepository.exclude`).
  Výsledek ani nic jiného se nemění, nic se nemaže; čtenáři historie (souhrn,
  Knihovna, brzdy, brána, kalibrace, sebekontrola, kouč) se ptají
  `counts_in_stats`. Potvrzení a `--approved` stejně jako u `--apply`
  (shoda řádků „vstup mimo bary“ se schváleným CSV). Řádek se značkou se
  nemění — opakovaný běh je idempotentní.

Spuštění z hostitele (partice v `data/`, PG publikované na 55432; URL se
nevypisuje). Produkce se nejdřív jen čte, zápis až po schválení reportu:
    uv run python scripts/recompute_setup_outcomes.py --data data \\
        [--symbols ES,NQ] [--ids 1003,1004] \\
        [--apply | --exclude [--yes --approved report.csv]]
URL: `--db`, jinak `GEXLENS_HOST_DATABASE_URL`, jinak `GEXLENS_DATABASE_URL`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import create_engine, select

from gexlens_engine.compute.paper import POINT_VALUES
from gexlens_engine.compute.settle import expiry_settle, is_quarterly_expiry
from gexlens_engine.compute.setups import (
    EXCLUDED_ENTRY_OFF_BARS,
    SETUP_MECHANICS_VERSION,
    Direction,
    Outcome,
    PathResult,
    PathState,
    born_after_settle,
    last_expected_minute,
    path_start,
    r_result,
    walk_setup_path,
)
from gexlens_engine.ibkr.underlying import BACKFILL_CONTRACT_TOLERANCE
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_RECONSTRUCTED,
    StoredBar,
    read_bars,
)
from gexlens_engine.storage.setups_store import SetupsRepository, entry_bar_ts, setups_table
from gexlens_engine.ticker import symbol_root

Verdict = Literal[
    "opravit", "opravit čas", "beze změny", "vstup mimo bary", "neověřitelný", "po settle"
]
VERDICTS: tuple[Verdict, ...] = (
    "opravit",
    "opravit čas",
    "beze změny",
    "vstup mimo bary",
    "neověřitelný",
    "po settle",
)
#: Verdikty, které `--apply` zapíše
WRITABLE: frozenset[Verdict] = frozenset({"opravit", "opravit čas"})
#: Verdikty, které `--exclude` označí značkou vyřazení ze statistik (#1346)
EXCLUDABLE: frozenset[Verdict] = frozenset({"vstup mimo bary"})
#: Shoda R při porovnání s DB (float z Postgresu vs. přepočet)
R_TOLERANCE = 1e-6
#: Shoda ceny: entry je close baru vstupu přesně (tatáž float hodnota)
PRICE_TOLERANCE = 1e-6
_MINUTE = dt.timedelta(minutes=1)
#: Kde hledat bar vstupu starších řádků (minuty proti minutě vzniku N), v pořadí
#: četnosti v5: N−1 (cyklus N nese bar N−1), N (opožděný cyklus, NQ po ES),
#: N−2 (zpožděný stream), N+1 … N+3 (cyklus u openu seance přetekl o minuty)
ENTRY_BAR_OFFSETS = (-1, 0, -2, 1, 2, 3)
#: Jak daleko před vznikem hledat bar, jehož close je vstup nad zamrzlým spotem
FROZEN_LOOKBACK = dt.timedelta(minutes=90)

# Bary čtené pro jeden setup: (symbol, od, do] → seřazené bary
BarLoader = Callable[[str, dt.datetime, dt.datetime], Sequence[StoredBar]]


@dataclass(frozen=True)
class SetupRow:
    """Uzavřený setup — jen sloupce, které přepočet čte."""

    id: int
    symbol: str
    expiry: str
    direction: str
    created_ts: dt.datetime
    entry: float
    target: float
    stop: float
    status: str
    closed_ts: dt.datetime | None
    outcome_r: float | None
    mfe: float | None = None
    mae: float | None = None
    #: Bar vstupu z kontextu (#1320); None = starší řádek nebo setup nad spotem
    entry_bar_ts: dt.datetime | None = None


@dataclass(frozen=True)
class Recomputed:
    """Výsledek přepočtu jednoho setupu proti DB."""

    row: SetupRow
    verdict: Verdict
    reason: str
    new_status: str | None = None
    new_r: float | None = None
    new_closed_ts: dt.datetime | None = None
    new_mfe: float | None = None
    new_mae: float | None = None
    sources: tuple[str, ...] = ()

    @property
    def usd_diff(self) -> float | None:
        """Rozdíl výsledku na 1 kontrakt v USD (nový − starý)."""
        point_value = POINT_VALUES.get(symbol_root(self.row.symbol))
        if self.new_r is None or point_value is None:
            return None
        risk = abs(self.row.entry - self.row.stop)
        return (self.new_r - (self.row.outcome_r or 0.0)) * risk * point_value

    @property
    def kind(self) -> str:
        """Třída změny pro report: co se na výsledku mění."""
        if self.new_status is None:
            return ""
        old, new = self.row.status, self.new_status
        if old == new:
            same_r = (
                self.row.outcome_r is not None
                and self.new_r is not None
                and abs(self.row.outcome_r - self.new_r) < R_TOLERANCE
            )
            if same_r:
                return "jen čas"
            return "timeout: cena v settle" if new == Outcome.TIMEOUT.value else "jiné R"
        return f"{_label(old)} → {_label(new)}"


def _label(status: str) -> str:
    return {"closed_target": "cíl", "closed_stop": "stop", "closed_timeout": "timeout"}.get(
        status, status
    )


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _hhmm(ts: dt.datetime | None) -> str:
    return "—" if ts is None else f"{_utc(ts):%d. %m. %H:%M}"


def _same_price(a: float, b: float) -> bool:
    return abs(a - b) < PRICE_TOLERANCE


def entry_starts(
    row: SetupRow, bars: Sequence[StoredBar], created: dt.datetime
) -> tuple[list[PathState], str]:
    """Kde začíná cesta ceny setupu: kandidáti na bar vstupu, nebo důvod, proč žádný.

    Bar vstupu z kontextu je jednoznačný. U starších řádků se hledá bar, jehož
    close je entry (`ENTRY_BAR_OFFSETS`); víc kandidátů rozhodne shoda výsledků
    ve volajícím. Bez shody vznikl setup nad spotem: jako engine začíná za
    barem N−1, pokud vstup leží v rozsahu barů N−1..N; jinak prázdný seznam
    a důvod pro verdikt „vstup mimo bary“.
    """
    if row.entry_bar_ts is not None:
        return [PathState(last_ts=_utc(row.entry_bar_ts), last_close=row.entry)], ""
    minute = created.replace(second=0, microsecond=0)
    by_ts = {bar.ts: bar for bar in bars}
    starts = [
        PathState(last_ts=ts, last_close=row.entry)
        for ts in (minute + offset * _MINUTE for offset in ENTRY_BAR_OFFSETS)
        if ts in by_ts and _same_price(by_ts[ts].close, row.entry)
    ]
    if starts:
        return starts, ""
    window = [by_ts[ts] for ts in (minute - _MINUTE, minute) if ts in by_ts]
    if not window:
        return [], f"v minutě vzniku {minute:%H:%M} ani před ní žádný bar — vstup nelze ověřit"
    low, high = min(bar.low for bar in window), max(bar.high for bar in window)
    if low <= row.entry <= high:
        return [path_start(created)], ""
    frozen = next(
        (
            bar
            for bar in reversed(bars)
            if minute - FROZEN_LOOKBACK <= bar.ts < minute and _same_price(bar.close, row.entry)
        ),
        None,
    )
    span = f"{window[0].ts:%H:%M}–{window[-1].ts:%H:%M} ({low:g}–{high:g})"
    if frozen is not None:
        age = int((minute - frozen.ts).total_seconds() // 60)
        return [], (
            f"vstup {row.entry:g} mimo bary {span}; = close baru {frozen.ts:%H:%M} "
            f"({age} min před vznikem) — zamrzlý spot"
        )
    return [], (
        f"vstup {row.entry:g} mimo bary {span} — cena mimo obchod (mid kotace) "
        "nebo jiný kontrakt (#1232)"
    )


def recompute(row: SetupRow, load_bars: BarLoader, now: dt.datetime) -> Recomputed:
    """Přehraje setup týmž `walk_setup_path` jako SetupEngine a porovná s DB.

    `now` = okamžik přepočtu: setup, jehož expirace ještě běží, se hodnotí jen
    po bary, které už existují (timeout ještě nenastal).
    """
    created = _utc(row.created_ts)
    if born_after_settle(row.expiry, created):
        return Recomputed(row, "po settle", "vznik po settle vlastní expirace (#1324)")
    settle = expiry_settle(row.expiry)
    if settle is None:
        return Recomputed(row, "neověřitelný", f"nečitelná expirace {row.expiry!r}")
    direction = Direction(row.direction)
    minute = created.replace(second=0, microsecond=0)
    if is_quarterly_expiry(settle.date()):
        # Výsledek rozhodne #1366, vstup na settle nezávisí: setup nad spotem
        # se vyřadí ze statistik i na kvartální den (#1346)
        entry_bars = load_bars(
            row.symbol, minute - FROZEN_LOOKBACK, minute + max(ENTRY_BAR_OFFSETS) * _MINUTE
        )
        _, entry_problem = entry_starts(row, entry_bars, created)
        if entry_problem:
            return Recomputed(row, "vstup mimo bary", entry_problem)
        return Recomputed(
            row,
            "neověřitelný",
            "kvartální datum expirace: settle SOQ (ADR-0039 bod 2) vs. odpolední "
            "týdenní řetěz (EW3/QN3) — rozhodne #1366",
        )
    # Od FROZEN_LOOKBACK před vznikem: bar vstupu i diagnostika zamrzlého spotu
    bars = load_bars(row.symbol, minute - FROZEN_LOOKBACK, settle)

    def walk(start: PathState) -> tuple[PathResult, str]:
        def step(force_until: dt.datetime | None) -> PathResult:
            return walk_setup_path(
                direction,
                row.entry,
                row.target,
                row.stop,
                settle,
                bars,
                start,
                now=min(settle, now),
                force_until=force_until,
            )

        path = step(None)
        if not path.blocked:
            return path, ""
        # Díra = neověřitelný; návrh přes díru jen pro rozhodnutí v reportu
        holes = ", ".join(f"{_hhmm(a)}–{b:%H:%M}" for a, b in path.gaps)
        return step(settle), holes

    starts, entry_problem = entry_starts(row, bars, created)
    ambiguous = ""
    if not starts:
        start = path_start(created)
        path, holes = walk(start)
    else:
        start = starts[0]
        path, holes = walk(start)
        outcomes = {
            (other.outcome, other.closed_ts, other.exit_price)
            for other, _ in (walk(candidate) for candidate in starts[1:])
        }
        if outcomes - {(path.outcome, path.closed_ts, path.exit_price)}:
            ambiguous = ", ".join(f"{s.last_ts:%H:%M}" for s in starts)

    if path.outcome is None or path.closed_ts is None:
        if entry_problem:
            return Recomputed(row, "vstup mimo bary", entry_problem)
        if holes:
            reason = f"chybí bary {holes}"
        elif now < settle:
            reason = "expirace ještě běží a bary úroveň nezasáhly"
        else:
            reason = "bez barů"
        return Recomputed(row, "neověřitelný", reason)
    outcome = path.outcome
    closed_ts = path.closed_ts
    # Bary, které rozhodly: od baru vstupu do zásahu úrovně / do konce seance před settle
    end = closed_ts if outcome is not Outcome.TIMEOUT else last_expected_minute(settle)
    used = [bar for bar in bars if end is not None and start.last_ts < bar.ts <= end]
    sources = tuple(sorted({bar.source or "ibkr" for bar in used}))
    new_r = (
        None
        if path.exit_price is None
        else r_result(direction, row.entry, row.stop, path.exit_price)
    )

    def result(verdict: Verdict, reason: str) -> Recomputed:
        # Návrh nese i nezapisovaný řádek (pro rozhodnutí), zapisuje se jen WRITABLE
        return Recomputed(
            row,
            verdict,
            reason,
            new_status=outcome.value,
            new_r=new_r,
            new_closed_ts=closed_ts,
            new_mfe=path.state.mfe,
            new_mae=path.state.mae,
            sources=sources,
        )

    if entry_problem:
        return result("vstup mimo bary", entry_problem)
    if ambiguous:
        return result(
            "neověřitelný",
            f"nejednoznačný bar vstupu (close {ambiguous} = entry) a výsledek na něm závisí",
        )
    if holes:
        return result("neověřitelný", f"chybí bary {holes} (návrh přes díru)")
    if new_r is None:
        return result("neověřitelný", "bez barů")
    if any(bar.source == BAR_SOURCE_RECONSTRUCTED for bar in used):
        return result(
            "neověřitelný",
            f"bary z jiného zdroje než IBKR ({', '.join(sources)}) — rekonstrukce z tasty",
        )
    jump = contract_jump(used)
    if jump is not None:
        return result("neověřitelný", jump)
    same_result = (
        row.status == outcome.value
        and row.outcome_r is not None
        and abs(row.outcome_r - new_r) < R_TOLERANCE
    )
    same_time = row.closed_ts is not None and _utc(row.closed_ts) == closed_ts
    if same_result and same_time:
        return result("beze změny", "")
    change = (
        f"{outcome.value} {_hhmm(closed_ts)} místo {row.status} {_hhmm(row.closed_ts)}"
        if not same_result
        else f"čas uzavření {_hhmm(closed_ts)} místo {_hhmm(row.closed_ts)}"
    )
    return result(
        "opravit" if not same_result else "opravit čas",
        f"#1320 přepočet z barů ({', '.join(sources)}): {change}",
    )


def contract_jump(used: Sequence[StoredBar]) -> str | None:
    """Skočí cena mezi sousedními minutami cesty na jinou hladinu — jiný kontrakt (#1232)?

    Partice 8.–18. 9. 2026 nesou vedle měřených barů doplněné `ibkr_hist`
    z dalšího kontraktu (+230–460 b). Práh = zápisová stráž backfillu
    (`BACKFILL_CONTRACT_TOLERANCE`, 0,3 %); pohyb mezi sousedními minutami je
    řádově 0,05 %. Vstup mimo hladinu barů hlídá už `entry_starts`.
    """
    for previous, bar in zip(used, used[1:], strict=False):
        if bar.ts - previous.ts != _MINUTE or previous.close <= 0:
            continue
        if abs(bar.open - previous.close) / previous.close > BACKFILL_CONTRACT_TOLERANCE:
            return (
                f"skok ceny {previous.close:g} → {bar.open:g} v {_hhmm(bar.ts)} "
                f"({previous.source or 'ibkr'} → {bar.source or 'ibkr'}) — jiný kontrakt? (#1232)"
            )
    return None


# ── Report ────────────────────────────────────────────────────────────────

CSV_COLUMNS = (
    "id",
    "symbol",
    "expiry",
    "verdict",
    "kind",
    "old_status",
    "new_status",
    "old_r",
    "new_r",
    "old_closed_ts",
    "new_closed_ts",
    "usd_diff_1c",
    "old_mfe",
    "new_mfe",
    "old_mae",
    "new_mae",
    "sources",
    "reason",
)


def _fmt_r(value: float | None) -> str:
    return "" if value is None else f"{value:+.3f}"


def _fmt_pts(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _iso(value: dt.datetime | None) -> str:
    return "" if value is None else _utc(value).isoformat()


def report_csv(results: Sequence[Recomputed]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for item in results:
        usd = item.usd_diff
        writer.writerow(
            [
                item.row.id,
                item.row.symbol,
                item.row.expiry,
                item.verdict,
                item.kind,
                item.row.status,
                item.new_status or "",
                _fmt_r(item.row.outcome_r),
                _fmt_r(item.new_r),
                _iso(item.row.closed_ts),
                _iso(item.new_closed_ts),
                "" if usd is None else f"{usd:.2f}",
                _fmt_pts(item.row.mfe),
                _fmt_pts(item.new_mfe),
                _fmt_pts(item.row.mae),
                _fmt_pts(item.new_mae),
                ",".join(item.sources),
                item.reason,
            ]
        )
    return buffer.getvalue()


def _impact_lines(items: Sequence[Recomputed]) -> list[str]:
    """Σ ΔR a Σ Δ USD na 1 kontrakt po symbolech."""
    lines = ["| symbol | počet | Σ ΔR | Σ Δ USD |", "|---|---:|---:|---:|"]
    for symbol in sorted({item.row.symbol for item in items}):
        rows = [item for item in items if item.row.symbol == symbol]
        delta_r = sum((item.new_r or 0.0) - (item.row.outcome_r or 0.0) for item in rows)
        delta_usd = sum(item.usd_diff or 0.0 for item in rows)
        lines.append(f"| {symbol} | {len(rows)} | {delta_r:+.2f} | {delta_usd:+,.0f} |")
    return lines


def report_markdown(results: Sequence[Recomputed], generated: dt.datetime, *, apply: bool) -> str:
    """Souhrn po verdiktech a symbolech + tabulky oprav, vstupů mimo bary a neověřitelných."""
    mode = (
        "před zápisem `--apply` (zapisují se jen verdikty „opravit“ a „opravit čas“)"
        if apply
        else "dry-run — do DB nic nezapsáno"
    )
    lines = [
        "# Přepočet výsledků setupů z barů (#1320)",
        "",
        f"Vygenerováno {generated:%Y-%m-%d %H:%M} UTC, {mode}.",
        "",
        "| verdikt | počet |",
        "|---|---:|",
    ]
    for verdict in VERDICTS:
        count = sum(1 for item in results if item.verdict == verdict)
        lines.append(f"| {verdict} | {count} |")
    fixes = [item for item in results if item.verdict == "opravit"]
    lines += ["", "## Dopad oprav na 1 kontrakt", "", *_impact_lines(fixes)]
    kinds = sorted({item.kind for item in results if item.verdict in WRITABLE})
    lines += ["", "## Zapisované změny podle třídy", "", "| třída | počet |", "|---|---:|"]
    for kind in kinds:
        count = sum(1 for item in results if item.verdict in WRITABLE and item.kind == kind)
        lines.append(f"| {kind} | {count} |")
    header = (
        "| id | symbol | třída | R staré → nové | closed_ts staré → nové | Δ USD 1 k. "
        "| MAE staré → nové | zdroje |"
    )
    sections = (
        ("Opravy výsledku", fixes),
        (
            "Opravy jen času uzavření (výsledek beze změny)",
            [item for item in results if item.verdict == "opravit čas"],
        ),
    )
    for title, items in sections:
        lines += ["", f"## {title}", "", header, "|---|---|---|---|---|---:|---|---|"]
        for item in items:
            usd = item.usd_diff
            lines.append(
                f"| {item.row.id} | {item.row.symbol} | {item.kind} | "
                f"{_fmt_r(item.row.outcome_r)} → {_fmt_r(item.new_r)} | "
                f"{_hhmm(item.row.closed_ts)} → {_hhmm(item.new_closed_ts)} | "
                f"{'' if usd is None else f'{usd:+,.0f}'} | "
                f"{_fmt_pts(item.row.mae)} → {_fmt_pts(item.new_mae)} | "
                f"{', '.join(item.sources)} |"
            )
    for verdict, title in (
        (
            "vstup mimo bary",
            "Vstup mimo bary (výsledek se nepřepisuje, `--exclude` vyřadí ze statistik)",
        ),
        ("neověřitelný", "Neověřitelné (nezapisují se, návrh jen pro rozhodnutí)"),
    ):
        items = [item for item in results if item.verdict == verdict]
        lines += [
            "",
            f"## {title}",
            "",
            "| id | symbol | status | návrh | důvod |",
            "|---|---|---|---|---|",
        ]
        for item in items:
            proposal = (
                f"{item.new_status} {_fmt_r(item.new_r)} {_hhmm(item.new_closed_ts)}"
                if item.new_status is not None
                else "—"
            )
            lines.append(
                f"| {item.row.id} | {item.row.symbol} | {item.row.status} "
                f"{_fmt_r(item.row.outcome_r)} | {proposal} | {item.reason} |"
            )
        if verdict == "vstup mimo bary" and items:
            proposals = [item for item in items if item.new_r is not None]
            lines += ["", "Dopad, kdyby se zapsal návrh:", "", *_impact_lines(proposals)]
    return "\n".join(lines) + "\n"


# ── DB ────────────────────────────────────────────────────────────────────


def load_rows(
    engine_url: str,
    *,
    symbols: Iterable[str] | None,
    ids: Iterable[int] | None,
    mechanics_version: int,
) -> list[SetupRow]:
    """Uzavřené setupy (status ≠ active) vybrané verze mechaniky — jen SELECT."""
    stmt = select(
        setups_table.c.id,
        setups_table.c.symbol,
        setups_table.c.expiry,
        setups_table.c.direction,
        setups_table.c.created_ts,
        setups_table.c.entry,
        setups_table.c.target,
        setups_table.c.stop,
        setups_table.c.status,
        setups_table.c.closed_ts,
        setups_table.c.outcome_r,
        setups_table.c.mfe,
        setups_table.c.mae,
        setups_table.c.context,
    ).where(
        setups_table.c.status != "active",
        setups_table.c.mechanics_version == mechanics_version,
    )
    if symbols:
        stmt = stmt.where(setups_table.c.symbol.in_(list(symbols)))
    if ids:
        stmt = stmt.where(setups_table.c.id.in_(list(ids)))
    stmt = stmt.order_by(setups_table.c.id)
    rows: list[SetupRow] = []
    with create_engine(engine_url).connect() as conn:
        for row in conn.execute(stmt):
            values = dict(row._mapping)
            context = values.pop("context")
            rows.append(SetupRow(**values, entry_bar_ts=entry_bar_ts(context)))
    return rows


def approved_mismatch(
    results: Sequence[Recomputed],
    approved_csv: Path,
    verdicts: frozenset[Verdict] = WRITABLE,
) -> list[str]:
    """Rozdíly zapisovaných řádků (`verdicts`) proti schválenému dry-runu (stejné sloupce CSV).

    Prázdný seznam = `--apply` / `--exclude` zapíše přesně to, co uživatel
    v reportu viděl.
    """

    def writable(text: str) -> dict[str, dict[str, str]]:
        return {
            row["id"]: row
            for row in csv.DictReader(io.StringIO(text))
            if row["verdict"] in verdicts
        }

    current = writable(report_csv(results))
    approved = writable(approved_csv.read_text(encoding="utf-8"))
    problems = [
        f"Setup {key}: chybí ve schváleném reportu" for key in current.keys() - approved.keys()
    ]
    problems += [
        f"Setup {key}: schválený, teď se nezapisuje" for key in approved.keys() - current.keys()
    ]
    for key in sorted(current.keys() & approved.keys(), key=int):
        changed = [
            f"{column} {approved[key][column]!r} → {current[key][column]!r}"
            for column in CSV_COLUMNS
            if approved[key].get(column) != current[key][column]
        ]
        if changed:
            problems.append(f"Setup {key}: {', '.join(changed)}")
    return problems


def apply_corrections(
    repository: SetupsRepository, results: Sequence[Recomputed], corrected_ts: dt.datetime
) -> int:
    """Zapíše verdikty „opravit“ a „opravit čas“; nic nemaže, původní hodnoty do kontextu."""
    written = 0
    for item in results:
        if item.verdict not in WRITABLE:
            continue
        if (
            item.new_status is None
            or item.new_r is None
            or item.new_closed_ts is None
            or item.new_mfe is None
            or item.new_mae is None
        ):
            raise ValueError(f"Setup {item.row.id}: verdikt {item.verdict} bez návrhu")
        if repository.correct_outcome(
            item.row.id,
            status=item.new_status,
            closed_ts=item.new_closed_ts,
            outcome_r=item.new_r,
            mfe=item.new_mfe,
            mae=item.new_mae,
            reason=item.reason,
            corrected_ts=corrected_ts,
        ):
            written += 1
        else:
            print(f"Setup {item.row.id}: neopraven — mezitím smazán nebo aktivní", file=sys.stderr)
    return written


def apply_exclusions(
    repository: SetupsRepository, results: Sequence[Recomputed], excluded_ts: dt.datetime
) -> int:
    """Označí verdikty „vstup mimo bary“ značkou `context.excluded` (#1346); nic nemaže."""
    written = 0
    for item in results:
        if item.verdict not in EXCLUDABLE:
            continue
        if repository.exclude(
            item.row.id,
            reason=EXCLUDED_ENTRY_OFF_BARS,
            detail=item.reason,
            excluded_ts=excluded_ts,
        ):
            written += 1
        else:
            print(
                f"Setup {item.row.id}: neoznačen — značku už má, je aktivní nebo smazán",
                file=sys.stderr,
            )
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument(
        "--data",
        default=os.environ.get("GEXLENS_DATA_DIR", "data"),
        help="datový kořen (derived/…/bars, reports/)",
    )
    parser.add_argument("--symbols", default="", help="např. ES,NQ (výchozí všechny)")
    parser.add_argument("--ids", default="", help="jen tato id (např. 1003,1004)")
    parser.add_argument("--mechanics-version", type=int, default=SETUP_MECHANICS_VERSION)
    parser.add_argument("--out", default="", help="adresář reportu (výchozí {data}/reports)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="zapsat opravy (po potvrzení)")
    mode.add_argument(
        "--exclude",
        action="store_true",
        help="vyřadit „vstup mimo bary“ ze statistik značkou context.excluded (#1346)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="s --apply / --exclude bez interaktivní otázky"
    )
    parser.add_argument(
        "--approved",
        default="",
        help="CSV schváleného dry-runu: zápis jen při shodě zapisovaných řádků",
    )
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    if args.yes and not args.approved:
        parser.error("--yes jen s --approved (CSV schváleného dry-runu)")

    data = Path(args.data)
    derived = data / "derived"
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    ids = [int(s) for s in args.ids.split(",") if s.strip()] or None
    rows = load_rows(args.db, symbols=symbols, ids=ids, mechanics_version=args.mechanics_version)

    def load_bars(symbol: str, since: dt.datetime, until: dt.datetime) -> list[StoredBar]:
        return read_bars(derived, symbol, since, until)

    generated = dt.datetime.now(dt.UTC)
    results = [recompute(row, load_bars, generated) for row in rows]
    out_dir = Path(args.out) if args.out else data / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"recompute-setup-outcomes-{generated:%Y%m%d-%H%M}"
    stem.with_suffix(".md").write_text(
        report_markdown(results, generated, apply=args.apply), encoding="utf-8"
    )
    stem.with_suffix(".csv").write_text(report_csv(results), encoding="utf-8")
    fixes = sum(1 for item in results if item.verdict in WRITABLE)
    off_bars = sum(1 for item in results if item.verdict in EXCLUDABLE)
    print(
        f"Setupů {len(results)}, k opravě {fixes}, vstup mimo bary {off_bars}; "
        f"report {stem}.md / .csv"
    )

    if args.exclude:
        return _exclude(args, results, off_bars, generated)
    if not args.apply:
        return 0
    if fixes == 0:
        print("Nic k opravě.")
        return 0
    if args.approved:
        problems = approved_mismatch(results, Path(args.approved))
        if problems:
            print("Přepočet se od schváleného reportu liší — nezapsáno:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
    if not args.yes:
        # Jen host/DB, přihlašovací údaje z URL se nevypisují
        answer = input(f"Zapsat {fixes} oprav do DB {args.db.split('@')[-1]}? Napiš 'ano': ")
        if answer.strip().lower() != "ano":
            print("Nezapsáno.")
            return 1
    written = apply_corrections(SetupsRepository(create_engine(args.db)), results, generated)
    print(f"Zapsáno {written} oprav (původní hodnoty v context.outcome_correction).")
    return 0


def _exclude(
    args: argparse.Namespace, results: Sequence[Recomputed], count: int, generated: dt.datetime
) -> int:
    """`--exclude`: značka vyřazení po kontrole schváleného CSV a potvrzení (#1346)."""
    if count == 0:
        print("Nic k vyřazení.")
        return 0
    if args.approved:
        problems = approved_mismatch(results, Path(args.approved), EXCLUDABLE)
        if problems:
            print("Přepočet se od schváleného reportu liší — nezapsáno:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
    if not args.yes:
        answer = input(
            f"Vyřadit {count} setupů ze statistik v DB {args.db.split('@')[-1]}? Napiš 'ano': "
        )
        if answer.strip().lower() != "ano":
            print("Nezapsáno.")
            return 1
    written = apply_exclusions(SetupsRepository(create_engine(args.db)), results, generated)
    print(f"Označeno {written} setupů (context.excluded, výsledky beze změny).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
