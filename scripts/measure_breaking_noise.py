"""Šum karty breaking news — potvrzené × nepotvrzené zprávy (ADR-0059 bod 4, #1491).

Vlastník 9. 10. 2026 rozhodl, že významná zpráva tier 3 jde na kartu hned se
štítkem „článek, zatím nepotvrzeno“ a štítek zmizí s první viditelnou kopií
tier 1–2. Skript měří, kolik takových zpráv karta ukáže a jak často
je tier 1–2 později potvrdí. Výběr a potvrzení počítá tatáž funkce jako karta
(`gexlens_news.breaking`) nad efektivním tierem v čase *t*
(`compute/news_tier.effective_tier`).

* **Vstup na kartu** je první okamžik, kdy je zpráva breaking: příjem
  prvního doručení (`ts_ingested`), nebo příjem kopie (`fetched_at`), která
  efektivní tier posunula do 1–3.
* **Potvrzení** je první okamžik od vstupu s efektivním tierem 1–2.
  **Náskok karty** = potvrzení − vstup u zpráv, které vstoupily nepotvrzené.
* Kopie (`news_event_sources`) se zaznamenávají až od nasazení E-6.24b
  (2026-10-09 12:01:22 UTC, #1489). Starší dny jsou proto bez potvrzení a počet
  nepotvrzených je v nich jen horní mez.
* Den = obchodní den seance Globexu, do které vstup patří
  (`compute/settle.trading_session_date`: po 17:00 CT už běží další den, nedělní
  otevření patří pondělí); sobota a neděle před otevřením jsou mimo seance.
  Průměr na obchodní den počítá jen seance uvnitř okna
  (`compute/settle.is_trading_session`) bez obou krajních, které bývají neúplné.
* Importance a kategorie jsou dnešní (reklasifikace se promítne i zpětně,
  stejně jako na kartě).

Spojení je jen pro čtení (`default_transaction_read_only`, transakce končí
rollbackem). Spuštění z hostitele (URL se nikdy nevypisuje):
    uv run python scripts/measure_breaking_noise.py [--days 30] [--as-of 2026-10-10T21:00:00+00:00]
URL z `--db`, `GEXLENS_NEWS_DATABASE_URL`, `GEXLENS_HOST_DATABASE_URL`, nebo
`GEXLENS_PG_PASSWORD`. Výstup je Markdown na stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.news_significance import (  # noqa: E402
    SIGNIFICANT_MIN_IMPORTANCE,
    is_key,
)
from gexlens_engine.compute.news_tier import SourceCopy, effective_tier  # noqa: E402
from gexlens_engine.compute.settle import (  # noqa: E402
    is_trading_session,
    trading_session_date,
)
from gexlens_engine.storage.sentiment import news_event_sources, news_events  # noqa: E402
from gexlens_news.breaking import card_group, is_breaking, is_confirmed  # noqa: E402

#: Nasazení E-6.24b — od té chvíle se kopie z jiných zdrojů zaznamenávají
COPIES_FROM = dt.datetime(2026, 10, 9, 12, 1, 22, tzinfo=dt.UTC)
DEFAULT_DAYS = 30
#: Zpoždění příjmu za publikací, nad kterým zpráva není čerstvá (varianta C)
STALE_MIN = 60
WEEKDAYS = ("po", "út", "st", "čt", "pá", "so", "ne")


@dataclass(frozen=True)
class Event:
    """První doručení zprávy (řádek `news_events`)."""

    id: int
    ts_event: dt.datetime
    ts_ingested: dt.datetime
    source: str
    kind: str
    category: str | None
    importance: int | None
    content_tier: int | None


@dataclass(frozen=True)
class Copy:
    """Kopie z jiného zdroje (řádek `news_event_sources`)."""

    source: str
    copy: SourceCopy


@dataclass(frozen=True)
class Timeline:
    """Kdy zpráva vstoupila na kartu a kdy ji potvrdilo doručení tier 1–2."""

    event: Event
    entered: dt.datetime
    confirmed: dt.datetime | None
    confirmed_by: str | None

    @property
    def confirmed_on_entry(self) -> bool:
        return self.confirmed == self.entered

    @property
    def lead(self) -> dt.timedelta | None:
        """Náskok karty proti čekání na potvrzení; None = potvrzená při vstupu nebo vůbec."""
        if self.confirmed is None or self.confirmed_on_entry:
            return None
        return self.confirmed - self.entered


def timeline(event: Event, copies: Sequence[Copy], *, until: dt.datetime) -> Timeline | None:
    """Vstup na kartu a potvrzení do `until`; None = zpráva na kartu nevstoupila.

    Před příjmem prvního doručení zpráva neexistuje; kopie přijatá dřív (souběh
    zapisovatelů) je viditelná od příjmu prvního doručení.
    """
    arrival = event.ts_ingested
    moments = sorted(
        {arrival, *(c.copy.fetched_at for c in copies if arrival < c.copy.fetched_at < until)}
    )
    plain = [c.copy for c in copies]
    entered: dt.datetime | None = None
    for moment in moments:
        tier = effective_tier(event.content_tier, plain, at=moment)
        if entered is None:
            if not is_breaking(tier, event.kind, event.importance, event.category):
                continue
            entered = moment
        if is_confirmed(tier):
            return Timeline(event, entered, moment, _confirmer(event, copies, moment))
    return Timeline(event, entered, None, None) if entered is not None else None


def _confirmer(event: Event, copies: Sequence[Copy], at: dt.datetime) -> str:
    """Zdroj, který zprávu potvrdil: první doručení, nebo nejdřívější viditelná kopie tier 1–2."""
    if is_confirmed(event.content_tier):
        return event.source
    visible = [c for c in copies if c.copy.fetched_at <= at and is_confirmed(c.copy.content_tier)]
    return min(visible, key=lambda c: c.copy.fetched_at).source


@dataclass
class DayStats:
    total: int = 0
    confirmed_on_entry: int = 0
    unconfirmed: int = 0
    later_confirmed: int = 0
    unconfirmed_key: int = 0


def daily(timelines: Sequence[Timeline]) -> dict[dt.date, DayStats]:
    days: dict[dt.date, DayStats] = defaultdict(DayStats)
    for item in timelines:
        stats = days[trading_session_date(item.entered)]
        stats.total += 1
        if item.confirmed_on_entry:
            stats.confirmed_on_entry += 1
            continue
        stats.unconfirmed += 1
        if item.confirmed is not None:
            stats.later_confirmed += 1
        event = item.event
        if is_key(event.kind, event.importance, event.category):
            stats.unconfirmed_key += 1
    return dict(days)


@dataclass
class SourceStats:
    unconfirmed: int = 0
    later_confirmed: int = 0
    lags_min: list[float] = field(default_factory=list)


def by_source(timelines: Sequence[Timeline]) -> dict[str, SourceStats]:
    """Zdroje nepotvrzených zpráv a jejich zpoždění `ts_ingested − ts_event`."""
    sources: dict[str, SourceStats] = defaultdict(SourceStats)
    for item in timelines:
        if item.confirmed_on_entry:
            continue
        stats = sources[item.event.source]
        stats.unconfirmed += 1
        if item.confirmed is not None:
            stats.later_confirmed += 1
        lag = item.event.ts_ingested - item.event.ts_event
        stats.lags_min.append(lag.total_seconds() / 60)
    return dict(sources)


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def load(engine: Engine, start: dt.datetime, end: dt.datetime) -> list[Timeline]:
    """Kandidáti z PG: předfiltr importance ≥ 2 je nadmnožina, pravidlo běží v Pythonu."""
    candidate = (news_events.c.ts_ingested >= start) & (news_events.c.ts_ingested < end)
    candidate &= news_events.c.importance >= SIGNIFICANT_MIN_IMPORTANCE
    events_stmt = select(
        news_events.c.id,
        news_events.c.ts_event,
        news_events.c.ts_ingested,
        news_events.c.source,
        news_events.c.kind,
        news_events.c.category,
        news_events.c.importance,
        news_events.c.content_tier,
    ).where(candidate)
    copies_stmt = select(news_event_sources).where(
        news_event_sources.c.event_id.in_(select(news_events.c.id).where(candidate))
    )
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
        event_rows = conn.execute(events_stmt).fetchall()
        copy_rows = conn.execute(copies_stmt).fetchall()
        conn.rollback()
    copies: dict[int, list[Copy]] = defaultdict(list)
    for row in copy_rows:
        copies[int(row.event_id)].append(
            Copy(
                row.source,
                SourceCopy(row.content_tier, _utc(row.published_at), _utc(row.fetched_at)),
            )
        )
    result: list[Timeline] = []
    for row in event_rows:
        event = Event(
            id=int(row.id),
            ts_event=_utc(row.ts_event),
            ts_ingested=_utc(row.ts_ingested),
            source=row.source,
            kind=row.kind,
            category=row.category,
            importance=row.importance,
            content_tier=row.content_tier,
        )
        item = timeline(event, copies[event.id], until=end)
        if item is not None:
            result.append(item)
    return result


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f} %" if whole else "—"


def _median(values: Sequence[float]) -> str:
    return f"{statistics.median(values):.1f}" if values else "—"


def render(timelines: Sequence[Timeline], *, as_of: dt.datetime, days: int) -> str:
    lines = [
        f"`as_of` = {as_of.isoformat()}, okno {days} dní; kopie se zaznamenávají od "
        f"{COPIES_FROM.isoformat()} (dřívější dny: nepotvrzené = horní mez).",
        "",
        "### Denně (obchodní den seance Globexu, ve které zpráva vstoupila na kartu)",
        "",
        "| den | breaking | potvrzené při vstupu | nepotvrzené | z nich později potvrzené "
        "| nepotvrzené `is_key` |",
        "|---|---|---|---|---|---|",
    ]
    for day, stats in sorted(daily(timelines).items()):
        lines.append(
            f"| {day.isoformat()} {WEEKDAYS[day.weekday()]} | {stats.total} "
            f"| {stats.confirmed_on_entry} "
            f"| {stats.unconfirmed} ({_pct(stats.unconfirmed, stats.total)}) "
            f"| {stats.later_confirmed} | {stats.unconfirmed_key} |"
        )

    copy_era = [item for item in timelines if item.entered >= COPIES_FROM]
    unconfirmed = [item for item in copy_era if not item.confirmed_on_entry]
    later = [item for item in unconfirmed if item.confirmed is not None]
    leads = [item.lead.total_seconds() / 60 for item in later if item.lead is not None]
    confirmers = Counter(item.confirmed_by for item in later)
    lines += [
        "",
        f"### Od záznamu kopií ({COPIES_FROM.isoformat()})",
        "",
        f"- breaking: {len(copy_era)}, nepotvrzené při vstupu: {len(unconfirmed)} "
        f"({_pct(len(unconfirmed), len(copy_era))})",
        f"- nepotvrzené, které tier 1–2 později potvrdil: {len(later)} "
        f"({_pct(len(later), len(unconfirmed))})",
        f"- medián náskoku karty: {_median(leads)} min (n = {len(leads)})",
        f"- potvrdil: {', '.join(f'{s} {n}' for s, n in confirmers.most_common()) or '—'}",
        "",
        "### Zdroje nepotvrzených zpráv (celé okno)",
        "",
        "| zdroj | nepotvrzené | později potvrzené "
        f"| medián zpoždění `ts_ingested − ts_event` (min) | nad {STALE_MIN} min |",
        "|---|---|---|---|---|",
    ]
    for source, stats in sorted(by_source(timelines).items(), key=lambda kv: -kv[1].unconfirmed):
        stale = sum(lag > STALE_MIN for lag in stats.lags_min)
        lines.append(
            f"| {source} | {stats.unconfirmed} | {stats.later_confirmed} "
            f"| {_median(stats.lags_min)} | {stale} ({_pct(stale, stats.unconfirmed)}) |"
        )
    groups = Counter(
        card_group(item.event.category, item.event.source)
        for item in timelines
        if not item.confirmed_on_entry
    )
    lines += [
        "",
        "### Skupiny nepotvrzených zpráv (celé okno)",
        "",
        ", ".join(f"{group} {count}" for group, count in groups.most_common()) or "—",
        "",
        "### Varianty zpřísnění — nepotvrzené při vstupu (celé okno)",
        "",
        "| varianta | celkem | průměr na celou obchodní seanci | mimo seance (víkend, svátky) |",
        "|---|---|---|---|",
    ]
    sessions = full_sessions(as_of - dt.timedelta(days=days), as_of)
    for name, total, in_sessions, off_session in variant_counts(timelines, sessions):
        average = f"{in_sessions / len(sessions):.1f}" if sessions else "—"
        lines.append(f"| {name} | {total} | {average} | {off_session} |")
    lines += ["", f"Celých obchodních seancí v okně: {len(sessions)}."]
    return "\n".join(lines)


def full_sessions(start: dt.datetime, end: dt.datetime) -> set[dt.date]:
    """Obchodní seance uvnitř `[start, end)` bez obou krajních (bývají neúplné)."""
    first, last = trading_session_date(start), trading_session_date(end)
    inner = (first + dt.timedelta(days=n) for n in range(1, (last - first).days))
    return {day for day in inner if is_trading_session(day)}


def is_stale(event: Event) -> bool:
    return event.ts_ingested - event.ts_event > dt.timedelta(minutes=STALE_MIN)


def variant_counts(
    timelines: Sequence[Timeline], sessions: set[dt.date]
) -> list[tuple[str, int, int, int]]:
    """Nepotvrzené při vstupu podle varianty.

    Vrací (název, celkem, v seancích `sessions`, mimo obchodní seance); vstupy
    v krajních seancích okna jsou jen v celkovém počtu.
    """
    unconfirmed = [item for item in timelines if not item.confirmed_on_entry]

    def key(item: Timeline) -> bool:
        event = item.event
        return is_key(event.kind, event.importance, event.category)

    def fresh(item: Timeline) -> bool:
        return not is_stale(item.event)

    variants: list[tuple[str, Callable[[Timeline], bool]]] = [
        ("A — všechny (rozhodnutí 9. 10.)", lambda item: True),
        ("B — jen `is_key`", key),
        (f"C — jen čerstvé (zpoždění ≤ {STALE_MIN} min)", fresh),
        ("B + C", lambda item: key(item) and fresh(item)),
    ]
    rows = []
    for name, keep in variants:
        days = [trading_session_date(item.entered) for item in unconfirmed if keep(item)]
        in_sessions = sum(day in sessions for day in days)
        off_session = sum(not is_trading_session(day) for day in days)
        rows.append((name, len(days), in_sessions, off_session))
    return rows


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", help="SQLAlchemy URL; jinak z prostředí (nevypisuje se)")
    parser.add_argument("--as-of", type=dt.datetime.fromisoformat, help="konec okna, ISO s TZ")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="délka okna ve dnech")
    args = parser.parse_args(argv)
    as_of: dt.datetime = args.as_of or dt.datetime.now(dt.UTC).replace(microsecond=0)
    if as_of.tzinfo is None:
        raise SystemExit("--as-of musí nést časové pásmo (např. +00:00)")
    engine = make_engine(database_url(args.db), read_only=True)
    start = as_of - dt.timedelta(days=args.days)
    print(render(load(engine, start, as_of), as_of=as_of, days=args.days))


if __name__ == "__main__":
    main()
