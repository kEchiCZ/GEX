"""Doplnění minutové fáze reakcí, když bary přibyly až po výpočtu (#1494).

`ReactionJob` měří minutovou fázi eventu v běžné frontě jen jednou: vybírá
eventy **bez jakéhokoli řádku** reakcí (ochrana #655). Díra vznikala dvěma
cestami:

* **souběh front:** denní fronta (eventy starší 16 dní) zapsala řádek eventu,
  který minutová fronta ještě neměřila, a event z ní vypadl (backfill zpráv
  17. 8., 27 968 párů doplněno 10. 10.);
* **výpadek symbolu:** symbol bez barů v T+60 min (výpadek feedu, restart
  enginu) dostal jen druhý symbol. Díru v barech pak doplní `ibkr_hist`.

Od #1494 (A + D) obě cesty v provozu zavírá job sám jednou metodou
`ReactionJob.complete_minute`: denní fáze doměří minutovou zprávám, které
zapisuje (D), a jednou za hodinu se doměří eventy posledních 3 dní (A). Skript
je tatáž metoda pro historii — potřeba je jen tam, kde denní fáze už proběhla
dřív, než bary existovaly (bary dodané později než 16 dní po zprávě, nebo
události z doby před #1494).

* Kandidát je pár (event, symbol), kde event už nějaký řádek reakcí má
  a symbol nemá minutovou fázi (`reaction_job.minute_gaps`).
* Měří se všechny symboly, ale zapisují se jen ty bez minutové fáze.
  Existující měření se v tomto režimu nepřepisuje (to dělá jen `--truncated-deferred`
  níž) a nic se nemaže. `market_closed` se opraví
  ze všech změřených symbolů jako v jobu (#339). Výjimka: deferred minutová
  fáze zapíše i `closure_open_ts`, který mohla nastavit denní fáze. Na týchž
  barech je to tatáž hodnota.
* Baseline objemu (`vol_z`) se bere k obchodnímu dni eventu (point-in-time).
* Pár bez barů zůstane bez minutové fáze a report ho započte (zavřený trh
  nebo díra v archivu).
* Idempotentní: druhý běh najde jen páry, které bary pořád nemají.

Jeden event trvá ~0,15 s, takže ~15 tis. kandidátů znamená ~35 min na průchod;
dry-run měří stejně jako `--apply`.

**Useknuté odložené reakce (#1574, `--truncated-deferred`):** do opravy #1574 se
odložená reakce měřila podle času zprávy, ne podle otevření po uzavírce, takže okna
často usekl poslední bar v době měření (16. 10.: 23 060 z 52 580 odložených
minutových reakcí). Kandidát je pár (event, symbol) s `deferred_min` a
`computed_at_min` < `closure_open_ts` + nejdelší okno. Přeměří ho táž
`complete_minute` z archivu barů (věčný) a minutovou fázi páru přepíše celým oknem.
Opakovaný běh už pár nenajde (nový `computed_at_min` je po uzavření okna). Pak
noční `ModelStatsJob` přepočítá buckety.

Režimy: výchozí dry-run jen čtením (PG `default_transaction_read_only`), `--apply`
zapíše. Před `--apply` na produkci záloha `pwsh scripts/backup-postgres.ps1`.
Spuštění z hostitele (URL se nikdy nevypisuje):
    uv run python scripts/backfill_minute_reactions.py [--apply] [--data-dir data]
        [--truncated-deferred]
URL z `--db`, `GEXLENS_NEWS_DATABASE_URL`, `GEXLENS_HOST_DATABASE_URL`, nebo
`GEXLENS_PG_PASSWORD`. Výstup je Markdown na stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.storage.sentiment import news_events, news_reactions  # noqa: E402
from gexlens_news.bars import CachedBars  # noqa: E402
from gexlens_news.reaction_job import MinuteGap, ReactionJob, minute_gaps  # noqa: E402
from gexlens_news.reactions import DEFAULT_WINDOWS  # noqa: E402

SYMBOLS = ("ES", "NQ")
DEFAULT_BATCH = 500


@dataclass
class Report:
    """Páry (měsíc, symbol): chybí / doplněno / bez barů."""

    missing: Counter[tuple[str, str]] = field(default_factory=Counter)
    filled: Counter[tuple[str, str]] = field(default_factory=Counter)
    windows: int = 0
    #: Režim `--truncated-deferred` (#1574): popisky „useknuté / přeměřeno“
    truncated: bool = False
    #: Odložené řádky bez `closure_open_ts` — useknutí u nich nejde poznat (#1574)
    unknown_closure: int = 0

    def render(self, *, apply: bool) -> str:
        if self.truncated:
            verb = "přeměřeno" if apply else "přeměřitelné"
            candidates = "useknutá odložená okna"
        else:
            verb = "doplněno" if apply else "doplnitelné"
            candidates = "bez minutové fáze"
        lines = [
            f"| měsíc | symbol | {candidates} | {verb} | stále bez barů |",
            "|---|---|---|---|---|",
        ]
        for month, symbol in sorted(self.missing):
            missing = self.missing[(month, symbol)]
            filled = self.filled[(month, symbol)]
            lines.append(f"| {month} | {symbol} | {missing} | {filled} | {missing - filled} |")
        total, done = sum(self.missing.values()), sum(self.filled.values())
        lines += [
            "",
            f"Celkem párů {total}, {verb} {done}, stále bez barů {total - done}; "
            f"oken {'zapsáno' if apply else 'k zápisu'} {self.windows}.",
        ]
        if self.truncated:
            lines.append(
                f"Odložených řádků bez `closure_open_ts` (useknutí nejde poznat): "
                f"{self.unknown_closure}."
            )
        return "\n".join(lines)


def candidates(
    engine: Engine,
    now: dt.datetime,
    *,
    symbols: Sequence[str] = SYMBOLS,
    batch: int = DEFAULT_BATCH,
) -> Iterator[MinuteGap]:
    """Eventy s aspoň jedním řádkem reakcí, kterým chybí minutová fáze symbolu.

    Tentýž výběr jako hodinové doměření jobu (`reaction_job.minute_gaps`), jen
    bez omezení na poslední 3 dny.
    """
    ready_before = now - dt.timedelta(minutes=max(DEFAULT_WINDOWS))
    return minute_gaps(engine, symbols=symbols, ready_before=ready_before, batch=batch)


def truncated_deferred(
    engine: Engine,
    *,
    symbols: Sequence[str] = SYMBOLS,
    max_window: int = max(DEFAULT_WINDOWS),
    report: Report | None = None,
) -> Iterator[MinuteGap]:
    """Odložené minutové reakce změřené před uzavřením okna (#1574), podle času eventu.

    Filtr času v Pythonu: součet sloupce a intervalu není dialektově neutrální
    (SQLite v testech) a odložených řádků je jen desítky tisíc.
    """
    span = dt.timedelta(minutes=max_window)
    stmt = (
        select(
            news_reactions.c.event_id,
            news_reactions.c.symbol,
            news_reactions.c.computed_at_min,
            news_reactions.c.closure_open_ts,
            news_events.c.ts_event,
            news_events.c.category,
        )
        .join(news_events, news_events.c.id == news_reactions.c.event_id)
        .where(
            news_reactions.c.deferred_min.is_(True),
            news_reactions.c.computed_at_min.is_not(None),
            news_reactions.c.symbol.in_(list(symbols)),
        )
        .order_by(news_events.c.ts_event, news_reactions.c.event_id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    events: dict[int, tuple[dt.datetime, str | None, set[str]]] = {}
    for row in rows:
        if row.closure_open_ts is None:
            if report is not None:
                report.unknown_closure += 1
            continue
        if _utc(row.computed_at_min) < _utc(row.closure_open_ts) + span:
            entry = events.setdefault(int(row.event_id), (_utc(row.ts_event), row.category, set()))
            entry[2].add(str(row.symbol))
    for event_id, (ts_event, category, truncated) in events.items():
        yield MinuteGap(event_id, ts_event, category, frozenset(truncated))


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def run(
    engine: Engine,
    data_dir: Path,
    *,
    apply: bool,
    now: dt.datetime,
    symbols: Sequence[str] = SYMBOLS,
    batch: int = DEFAULT_BATCH,
    truncated: bool = False,
) -> Report:
    """Doplní (nebo v dry-runu jen spočítá) minutovou fázi kandidátů.

    `truncated` = přeměření useknutých odložených reakcí (#1574) místo doplnění.
    """
    job = ReactionJob(engine, CachedBars(data_dir), symbols=symbols)
    report = Report(truncated=truncated)
    gaps = (
        truncated_deferred(engine, symbols=symbols, report=report)
        if truncated
        else candidates(engine, now, symbols=symbols, batch=batch)
    )
    for fill in job.complete_minute(gaps, now, write=apply):
        month = fill.gap.ts_event.strftime("%Y-%m")
        for symbol in fill.gap.missing:
            report.missing[(month, symbol)] += 1
        for symbol in fill.filled:
            report.filled[(month, symbol)] += 1
        report.windows += fill.windows
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", help="SQLAlchemy URL; jinak z prostředí (nevypisuje se)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data", help="kořen archivu")
    parser.add_argument("--apply", action="store_true", help="zapsat (jinak dry-run jen čtením)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument(
        "--truncated-deferred",
        action="store_true",
        help="přeměřit useknuté odložené reakce (#1574) místo doplnění chybějících",
    )
    args = parser.parse_args(argv)
    if not (args.data_dir / "derived").is_dir():
        raise SystemExit(f"Chybí archiv barů: {args.data_dir / 'derived'}")
    engine = make_engine(database_url(args.db), read_only=not args.apply)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    report = run(
        engine,
        args.data_dir,
        apply=args.apply,
        now=now,
        batch=args.batch,
        truncated=args.truncated_deferred,
    )
    mode = "zápis" if args.apply else "dry-run"
    print(f"`as_of` = {now.isoformat()}, režim {mode}\n\n{report.render(apply=args.apply)}")


if __name__ == "__main__":
    main()
