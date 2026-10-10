"""Doplnění minutové fáze reakcí, když bary přibyly až po výpočtu (#1494).

`ReactionJob` měří event jednou: vybírá jen eventy **bez jakéhokoli řádku**
reakcí (ochrana #655). Když v době výpočtu minutové bary ještě nebyly (backfill
zpráv před backfillem barů), zapíše se jen denní fáze. Minutová fáze
(`computed_at_min` NULL) se pak už nikdy nepřepočítá, i když bary později
přibudou. Tenhle skript ji doplní touž cestou jako job
(`ReactionJob.measure_minute` a `write_minute`).

* Kandidát je pár (event, symbol), kde event už nějaký řádek reakcí má
  a symbol nemá minutovou fázi.
* Měří se všechny symboly, ale zapisují se jen ty bez minutové fáze.
  Existující měření se nepřepisuje a nic se nemaže. `market_closed` se opraví
  ze všech změřených symbolů jako v jobu (#339). Výjimka: deferred minutová
  fáze zapíše i `closure_open_ts`, který mohla nastavit denní fáze. Na týchž
  barech je to tatáž hodnota.
* Baseline objemu (`vol_z`) se bere k obchodnímu dni eventu (point-in-time),
  stejně jako u jobu, který měří hned po zprávě.
* Pár bez barů zůstane bez minutové fáze a report ho započte (zavřený trh
  nebo díra v archivu).
* Idempotentní: druhý běh najde jen páry, které bary pořád nemají.

Díra vzniká dvěma cestami:

* backfill zpráv proběhne před backfillem barů;
* v běžném provozu symbol, který v T+60 min nemá bary (výpadek feedu, restart
  enginu), dostane jen denní fázi. `run` zapíše jen změřené symboly a event
  z pending dotazu vypadne. Díru v barech pak doplní automatický backfill
  `ibkr_hist`, reakci ale ne.

Od #1494 (A + D) obě cesty v provozu zavírá sám job: denní fáze doměří minutovou
zprávám, které zapisuje, a jednou za hodinu se doměří eventy posledních 3 dní
(`ReactionJob.complete_minute`). Skript je tatáž metoda pro historii, typicky
po backfillu barů starších než 3 dny. Jeden event trvá ~0,15 s, takže
~15 tis. kandidátů znamená ~35 min na průchod; dry-run měří stejně jako `--apply`.

Režimy: výchozí dry-run jen čtením (PG `default_transaction_read_only`), `--apply`
zapíše. Před `--apply` na produkci záloha `pwsh scripts/backup-postgres.ps1`.
Spuštění z hostitele (URL se nikdy nevypisuje):
    uv run python scripts/backfill_minute_reactions.py [--apply] [--data-dir data]
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
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_news.bars import BarsRepository  # noqa: E402
from gexlens_news.reaction_job import MinuteGap, ReactionJob, minute_gaps  # noqa: E402
from gexlens_news.reactions import DEFAULT_WINDOWS, Bar  # noqa: E402

SYMBOLS = ("ES", "NQ")
DEFAULT_BATCH = 500
#: Partic v paměti: baseline čte ~57 partic na symbol (`recent_sessions`: 2 × 20
#: seancí + 14 dní + kraje) a okno eventu ~13; dva symboly ≈ 140, s rezervou 160.
#: Méně než 140 čte partice dokola (měřeno 64 → 5,5× víc čtení).
CACHE_DAYS = 160


class CachedBars(BarsRepository):
    """Partice barů s omezenou cache; eventy podle času čtou tytéž dny dokola."""

    def __init__(self, data_dir: Path, capacity: int = CACHE_DAYS) -> None:
        super().__init__(data_dir)
        self._capacity = capacity
        self._cache: dict[tuple[str, dt.date], list[Bar]] = {}

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        key = (symbol, day)
        cached = self._cache.get(key)
        if cached is None:
            cached = super().load_day(symbol, day)
            if len(self._cache) >= self._capacity:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = cached
        return cached


@dataclass
class Report:
    """Páry (měsíc, symbol): chybí / doplněno / bez barů."""

    missing: Counter[tuple[str, str]] = field(default_factory=Counter)
    filled: Counter[tuple[str, str]] = field(default_factory=Counter)
    windows: int = 0

    def render(self, *, apply: bool) -> str:
        verb = "doplněno" if apply else "doplnitelné"
        lines = [
            f"| měsíc | symbol | bez minutové fáze | {verb} | stále bez barů |",
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


def run(
    engine: Engine,
    data_dir: Path,
    *,
    apply: bool,
    now: dt.datetime,
    symbols: Sequence[str] = SYMBOLS,
    batch: int = DEFAULT_BATCH,
) -> Report:
    """Doplní (nebo v dry-runu jen spočítá) minutovou fázi kandidátů."""
    job = ReactionJob(engine, CachedBars(data_dir), symbols=symbols)
    report = Report()
    gaps = candidates(engine, now, symbols=symbols, batch=batch)
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
    args = parser.parse_args(argv)
    if not (args.data_dir / "derived").is_dir():
        raise SystemExit(f"Chybí archiv barů: {args.data_dir / 'derived'}")
    engine = make_engine(database_url(args.db), read_only=not args.apply)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    report = run(engine, args.data_dir, apply=args.apply, now=now, batch=args.batch)
    mode = "zápis" if args.apply else "dry-run"
    print(f"`as_of` = {now.isoformat()}, režim {mode}\n\n{report.render(apply=args.apply)}")


if __name__ == "__main__":
    main()
