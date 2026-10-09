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
  ze všech změřených symbolů jako v jobu (#339).
* Baseline objemu (`vol_z`) se bere k obchodnímu dni eventu (point-in-time),
  stejně jako u jobu, který měří hned po zprávě.
* Pár bez barů zůstane bez minutové fáze a report ho započte (zavřený trh
  nebo díra v archivu).
* Idempotentní: druhý běh najde jen páry, které bary pořád nemají.

Do jobu se doplňování nedává, protože pár bez barů by se vybíral každý cyklus
(past #655). Skript se pustí ručně po každém backfillu barů.

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
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.settle import trading_session_date  # noqa: E402
from gexlens_engine.storage.sentiment import news_events, news_reactions  # noqa: E402
from gexlens_news.bars import BarsRepository  # noqa: E402
from gexlens_news.reaction_job import ReactionJob  # noqa: E402
from gexlens_news.reactions import DEFAULT_WINDOWS, Bar, VolumeBaseline  # noqa: E402

SYMBOLS = ("ES", "NQ")
DEFAULT_BATCH = 500
#: Partic v paměti — eventy jdou podle času, okno eventu čte ~12 dní na symbol
CACHE_DAYS = 64


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


@dataclass(frozen=True)
class Candidate:
    event_id: int
    ts_event: dt.datetime
    category: str | None
    missing: frozenset[str]


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


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def candidates(
    engine: Engine,
    now: dt.datetime,
    *,
    symbols: Sequence[str] = SYMBOLS,
    batch: int = DEFAULT_BATCH,
) -> Iterator[Candidate]:
    """Eventy s aspoň jedním řádkem reakcí, kterým chybí minutová fáze symbolu.

    Event bez řádku vůbec patří běžnému jobu (`_pending_events`), ne sem.
    Jen eventy s uzavřeným nejdelším minutovým oknem; podle `ts_event`.
    """
    incomplete = (
        select(news_reactions.c.event_id)
        .where(news_reactions.c.symbol.in_(symbols))
        .group_by(news_reactions.c.event_id)
        .having(func.count(news_reactions.c.computed_at_min) < len(symbols))
        .subquery()
    )
    ready_before = now - dt.timedelta(minutes=max(DEFAULT_WINDOWS))
    stmt = (
        select(news_events.c.id, news_events.c.ts_event, news_events.c.category)
        .join(incomplete, incomplete.c.event_id == news_events.c.id)
        .where(news_events.c.ts_event <= ready_before)
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    with engine.connect() as conn:
        events = conn.execute(stmt).fetchall()
    for start in range(0, len(events), batch):
        chunk = events[start : start + batch]
        ids = [int(row.id) for row in chunk]
        with engine.connect() as conn:
            complete = conn.execute(
                select(news_reactions.c.event_id, news_reactions.c.symbol).where(
                    news_reactions.c.event_id.in_(ids),
                    news_reactions.c.computed_at_min.is_not(None),
                )
            ).fetchall()
        done: dict[int, set[str]] = {}
        for row in complete:
            done.setdefault(int(row.event_id), set()).add(row.symbol)
        for row in chunk:
            missing = frozenset(set(symbols) - done.get(int(row.id), set()))
            if missing:
                yield Candidate(int(row.id), _utc(row.ts_event), row.category, missing)


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
    baselines: dict[dt.date, dict[str, dict[dt.time, VolumeBaseline] | None]] = {}
    report = Report()
    for candidate in candidates(engine, now, symbols=symbols, batch=batch):
        month = candidate.ts_event.strftime("%Y-%m")
        for symbol in candidate.missing:
            report.missing[(month, symbol)] += 1
        # Baseline k obchodnímu dni eventu, ne k dnešku (point-in-time)
        day = trading_session_date(candidate.ts_event)
        if day not in baselines:
            baselines[day] = {symbol: job.baseline_for(symbol, day) for symbol in symbols}
        measurement = job.measure_minute(
            candidate.ts_event, candidate.category, baselines[day], now
        )
        fillable = [row for row in measurement.rows if row.symbol in candidate.missing]
        if not fillable:
            continue
        for row in fillable:
            report.filled[(month, row.symbol)] += 1
        if apply:
            report.windows += job.write_minute(
                candidate.event_id, measurement, only_symbols=candidate.missing
            )
        else:
            report.windows += sum(row.windows for row in fillable)
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
