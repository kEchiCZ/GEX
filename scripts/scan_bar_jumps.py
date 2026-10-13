"""Sken skoků ≥ 60 bp mezi po sobě jdoucími minutovými bary ES a NQ (E-0.2, #1390).

Čte partice `data/derived/{symbol}/bars/*.parquet` (jen čtení) a hlásí každý
skok close proti předchozímu baru uvnitř seance i přes její hranici. Řádek
reportu: obchodní den × symbol × čas × bp × mezera × zdroj před → po ×
pravděpodobná příčina. Příčiny v pořadí přednosti:

1. **špatný kontrakt** — skok vrácený opačným skokem podobné velikosti v téže
   seanci, přičemž trh před blokem i v něm stojí (blok barů jiného kontraktu:
   8.–18. 9. 2026 Z6 o +83 až +107 bp mezi U6, #1232, #1299),
2. **díra v datech** — mezi bary chybí víc než 20 min otevřeného trhu (okno
   zprávy); kdy pohyb nastal, nejde říct (díra 14.–16. 7. 2026, #1300),
3. **roll** — kladný skok přes hranici seance v týdnu rollu (`_roll_window`):
   pipeline přepíná kontrakt novým discovery (nová seance, restart enginu) a další
   kontrakt je v contangu výš; záporný skok v týdnu rollu je pohyb trhu. Přepnutí
   při restartu uvnitř seance se jako roll nepozná (vyjde jako skutečný pohyb),
4. **zpráva** — významná zpráva (`compute.news_significance`) v okně
   −20/+2 min kolem skoku; po mezeře delší než 20 min od posledního baru před ní
   (víkend, pauza — pohyb mohl nastat kdykoli v mezeře),
5. jinak **skutečný pohyb bez zprávy**.

Mezera mezi bary jen přes zavřený trh (denní pauza, víkend, svátek —
`marketclock.is_market_closed`) je „zavřeno", ne díra; víkendové UTC klíče
partic se tak jako díra nehlásí. Obchodní den je `settle.trading_session_date`,
den bez US seance (`settle.is_trading_session`) nese poznámku. Práh skoku
`JUMP_BP` a týden rollu sdílí s měřením tvrdých dat (#1392) — jedna definice.

Zprávy se čtou v transakci `SET TRANSACTION READ ONLY`. Spuštění z hostitele
(PG publikované na 55432; URL se nikdy nevypisuje):
    uv run python scripts/scan_bar_jumps.py --data data --db "<URL>"
s URL `postgresql+psycopg://gexlens:<heslo>@127.0.0.1:55432/gexlens` (heslo =
`GEXLENS_PG_PASSWORD`; bez `--db` se bere `GEXLENS_HOST_DATABASE_URL`).
Na Windows s `PYTHONUTF8=1` (výstup obsahuje ≤ a české znaky). Výstup je
Markdown na stdout. Skript převezme E-6.15 (#1406).
"""

import argparse
import datetime as dt
import os
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from measure_hard_data_1381 import CORE_SYMBOLS, JUMP_BP, _roll_window, _table
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.news_significance import significance_tier
from gexlens_engine.compute.settle import is_trading_session, trading_session_date
from gexlens_engine.storage.parquet_store import bar_source_rank
from gexlens_engine.storage.sentiment import news_events

#: Opačný skok vrací blok jiného kontraktu, když se zbytek |a + b| vejde do
#: tohoto podílu většího z obou skoků — zbytek je jen pohyb trhu v minutách hran
#: bloku. Záloha kontaminace 8.–18. 9. 2026 (data/backup/bars-1232): 52 párů,
#: zbytek ≤ 8 %. Přeměření: soubory `{SYM}-{den}.parquet` zálohy zkopírovat do
#: `<dir>/derived/{SYM}/bars/{den}.parquet` a spustit s `--data <dir>`
#: (odůvodnění prahů v PR #1390).
REVERT_TOLERANCE = 0.10
#: …a trh kolem bloku stojí: medián |minutového pohybu| před blokem i uvnitř
#: něj nejvýš tolik bp. Kontaminace září 2026 ≤ 3,5 bp; skutečné vrácené pohyby
#: (cla 2.–9. 4. 2025) mají zbytek i pod 10 %, ale medián 7–137 bp.
CALM_BP = 5.0
#: Kolik barů (minut obchodu) před vstupem do bloku se měří klid trhu
CALM_LOOKBACK = 30
MINUTE = dt.timedelta(minutes=1)
#: Okno hledání zprávy kolem skoku (před, po); díra delší než NEWS_BEFORE = čas pohybu neurčitelný
NEWS_BEFORE = dt.timedelta(minutes=20)
NEWS_AFTER = dt.timedelta(minutes=2)

CAUSE_WRONG_CONTRACT = "špatný kontrakt"
CAUSE_HOLE = "díra v datech"
CAUSE_ROLL = "roll"
CAUSE_NEWS = "zpráva"
CAUSE_REAL = "skutečný pohyb bez zprávy"
CAUSES = (CAUSE_WRONG_CONTRACT, CAUSE_HOLE, CAUSE_ROLL, CAUSE_NEWS, CAUSE_REAL)


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    close: float
    source: str


@dataclass
class Jump:
    symbol: str
    #: Pozice `bar` v seznamu barů symbolu (klid trhu kolem bloku)
    index: int
    prev: Bar
    bar: Bar
    bp: float
    session: dt.date
    boundary: bool
    cause: str = ""
    partner: dt.datetime | None = None
    news: str = ""

    @property
    def gap_minutes(self) -> int:
        return (self.bar.ts - self.prev.ts) // MINUTE

    @property
    def missing_open_minutes(self) -> int:
        """Minuty otevřeného trhu bez baru uvnitř mezery; 0 = jen zavřený trh."""
        return sum(
            not is_market_closed(self.prev.ts + k * MINUTE) for k in range(1, self.gap_minutes)
        )


def load_bars(bars_dir: Path) -> list[Bar]:
    """Bary symbolu ze všech partic, seřazené; při střetu minuty vyhrává původ podle storage."""
    by_ts: dict[dt.datetime, Bar] = {}
    for path in sorted(bars_dir.glob("*.parquet")):
        # Starší partice sloupec `source` nemají — NULL znamená živou cestu
        columns = ["ts_min", "close"]
        if "source" in pq.read_schema(path).names:
            columns.append("source")
        for rec in pq.read_table(path, columns=columns).to_pylist():
            source = rec.get("source")
            old = by_ts.get(rec["ts_min"])
            if old is None or bar_source_rank(source) >= bar_source_rank(old.source):
                by_ts[rec["ts_min"]] = Bar(rec["ts_min"], float(rec["close"]), source or "NULL")
    return [by_ts[ts] for ts in sorted(by_ts)]


def _move_bp(bars: Sequence[Bar], index: int) -> float | None:
    prev, bar = bars[index - 1], bars[index]
    if prev.close <= 0 or bar.close <= 0:
        return None
    return 1e4 * (bar.close / prev.close - 1.0)


def find_jumps(symbol: str, bars: Sequence[Bar], threshold_bp: float = JUMP_BP) -> list[Jump]:
    jumps = []
    for index in range(1, len(bars)):
        bp = _move_bp(bars, index)
        if bp is not None and abs(bp) >= threshold_bp:
            prev, bar = bars[index - 1], bars[index]
            session = trading_session_date(bar.ts)
            boundary = trading_session_date(prev.ts) != session
            jumps.append(Jump(symbol, index, prev, bar, bp, session, boundary))
    return jumps


def _median_move(bars: Sequence[Bar], start: int, stop: int) -> float:
    """Medián |minutového pohybu| v bp barů start..stop−1; prázdný úsek = 0."""
    moves = [abs(m) for k in range(max(start, 1), stop) if (m := _move_bp(bars, k)) is not None]
    return statistics.median(moves) if moves else 0.0


def is_calm_block(bars: Sequence[Bar], entry: Jump, exit_: Jump, calm_bp: float = CALM_BP) -> bool:
    """Trh `CALM_LOOKBACK` min před vstupem do bloku i uvnitř bloku stojí."""
    before = _median_move(bars, entry.index - CALM_LOOKBACK, entry.index)
    inside = _median_move(bars, entry.index + 1, exit_.index)
    return max(before, inside) <= calm_bp


def mark_wrong_contract(
    jumps: Sequence[Jump],
    bars: Sequence[Bar],
    tolerance: float = REVERT_TOLERANCE,
    calm_bp: float = CALM_BP,
) -> None:
    """Spáruje skoky jednoho symbolu do bloků jiného kontraktu.

    Skok se páruje s nejbližším dřívějším nespárovaným opačným skokem téže
    seance. Blok jiného kontraktu = vstup o +s a výstup o −s v klidném trhu;
    zbytek |a + b| je jen pohyb trhu v minutách hran. Skutečný pohyb se vrací
    v rozkolísaném trhu (cla 4/2025), proto i podmínka klidu (`is_calm_block`).
    """
    open_: list[Jump] = []
    for jump in sorted(jumps, key=lambda j: j.bar.ts):
        partner = next(
            (
                o
                for o in reversed(open_)
                if o.session == jump.session
                and o.bp * jump.bp < 0
                and abs(o.bp + jump.bp) <= tolerance * max(abs(o.bp), abs(jump.bp))
                and is_calm_block(bars, o, jump, calm_bp)
            ),
            None,
        )
        if partner is None:
            open_.append(jump)
            continue
        open_.remove(partner)
        partner.cause = jump.cause = CAUSE_WRONG_CONTRACT
        partner.partner, jump.partner = jump.bar.ts, partner.bar.ts


def news_window(jump: Jump) -> tuple[dt.datetime, dt.datetime]:
    start = min(jump.bar.ts - NEWS_BEFORE, jump.prev.ts)
    return start, jump.bar.ts + NEWS_AFTER


def significant_news(conn: Connection, start: dt.datetime, end: dt.datetime) -> str:
    """Nejvýznamnější zpráva okna (stupeň, pak blízkost konci okna); prázdné = žádná."""
    rows = conn.execute(
        select(
            news_events.c.ts_event,
            news_events.c.kind,
            news_events.c.importance,
            news_events.c.category,
            news_events.c.title,
        ).where(news_events.c.ts_event >= start, news_events.c.ts_event <= end)
    ).fetchall()
    ranked = []
    for row in rows:
        tier = significance_tier(row.kind, row.importance, row.category)
        if tier is not None:
            ts = row.ts_event if row.ts_event.tzinfo else row.ts_event.replace(tzinfo=dt.UTC)
            ranked.append((tier, end - ts, ts, str(row.title)))
    if not ranked:
        return ""
    tier, _, ts, title = min(ranked)
    clean = " ".join(title.replace("|", "/").split())
    # Po víkendu nebo pauze může být zpráva z jiného dne než skok
    when = f"{ts:%H:%M}" if ts.date() == (end - NEWS_AFTER).date() else f"{ts:%Y-%m-%d %H:%M}"
    return f"{when} st. {tier}: {clean[:90]}"


def classify(jumps: Iterable[Jump], conn: Connection) -> None:
    """Doplní příčinu skokům, které nejsou špatný kontrakt; zprávu hledá u všech."""
    for jump in jumps:
        jump.news = significant_news(conn, *news_window(jump))
        if jump.cause:
            continue
        if jump.missing_open_minutes > NEWS_BEFORE / MINUTE:
            jump.cause = CAUSE_HOLE
        elif jump.boundary and jump.bp > 0 and _roll_window(jump.session):
            jump.cause = CAUSE_ROLL
        elif jump.news:
            jump.cause = CAUSE_NEWS
        else:
            jump.cause = CAUSE_REAL


def _gap(jump: Jump) -> str:
    if jump.gap_minutes <= 1:
        return f"{jump.gap_minutes}"
    missing = jump.missing_open_minutes
    return f"{jump.gap_minutes} ({f'díra {missing} min' if missing else 'zavřeno'})"


def report(jumps: Sequence[Jump], threshold_bp: float) -> str:
    header = [
        "obchodní den",
        "symbol",
        "čas (UTC)",
        "bp",
        "mezera min",
        "zdroj před → po",
        "pravděpodobná příčina",
        "zpráva (−20/+2 min, po mezeře celá mezera)",
    ]
    rows: list[list[object]] = []
    for j in sorted(jumps, key=lambda j: (j.session, j.symbol, j.bar.ts)):
        day = f"{j.session}" + ("" if is_trading_session(j.session) else " (bez US seance)")
        cause = j.cause
        if j.partner is not None:
            cause += f" (pár {j.partner:%H:%M})"
        elif j.boundary:
            cause += " · hranice seance"
        rows.append(
            [
                day,
                j.symbol,
                f"{j.bar.ts:%Y-%m-%d %H:%M}",
                f"{j.bp:+.0f}",
                _gap(j),
                f"{j.prev.source} → {j.bar.source}",
                cause,
                j.news,
            ]
        )
    counts: Counter[tuple[str, str]] = Counter((j.cause, j.symbol) for j in jumps)
    symbols = sorted({j.symbol for j in jumps})
    summary = [[c, *(counts[(c, s)] for s in symbols)] for c in CAUSES]
    return (
        f"### Skoky ≥ {threshold_bp:.0f} bp mezi po sobě jdoucími minutovými bary\n\n"
        + _table(header, rows)
        + "\n\n### Souhrn podle příčiny\n\n"
        + _table(["pravděpodobná příčina", *symbols], summary)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--symbols", default=",".join(CORE_SYMBOLS))
    parser.add_argument("--threshold-bp", type=float, default=JUMP_BP)
    parser.add_argument("--revert-tolerance", type=float, default=REVERT_TOLERANCE)
    parser.add_argument("--calm-bp", type=float, default=CALM_BP)
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    print(f"as_of {dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()}\n")
    jumps: list[Jump] = []
    for symbol in args.symbols.split(","):
        bars = load_bars(args.data / "derived" / symbol / "bars")
        if not bars:
            raise SystemExit(f"{symbol}: žádné bary v {args.data}/derived/{symbol}/bars")
        first, last = bars[0].ts, bars[-1].ts
        print(f"- {symbol}: {len(bars)} minut, {first:%Y-%m-%d %H:%M} až {last:%Y-%m-%d %H:%M} UTC")
        found = find_jumps(symbol, bars, args.threshold_bp)
        mark_wrong_contract(found, bars, args.revert_tolerance, args.calm_bp)
        jumps += found
    with create_engine(args.db).connect() as conn:
        # Autobegin: první příkaz transakce ji přepne do read only; konec = rollback.
        # SQLite (testy) příkaz nezná.
        if conn.dialect.name == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
        classify(jumps, conn)
        conn.rollback()
    print(
        f"\nBlok jiného kontraktu: zbytek ≤ {args.revert_tolerance:.0%} většího skoku, "
        f"medián |minutového pohybu| před blokem i v něm ≤ {args.calm_bp:.1f} bp\n"
    )
    print(report(jumps, args.threshold_bp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
