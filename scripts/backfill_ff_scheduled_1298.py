"""Klíč kalendáře FF s minutou releasu a doplnění zahozených releasů (#1298).

Do #1298 měl `dedup_hash` položky kalendáře (`kind = scheduled`) v klíči jen den. Když FF
uvedl dvě hodnoty téže řady v jeden den (dohánění po shutdownu 2025: NFP 16. 12. 2025
13:29 a 13:30, PPI 14. 1. 2026, Core PCE 22. 1. 2026; dva projevy členů FOMC), zůstal
v `news_events` jen první a druhý se zahodil. Od #1298 nese klíč minutu releasu
(`compute.newstext.scheduled_key`). Skript srovná historii ve dvou krocích:

1. **rehash** — `dedup_hash` všech řádků `scheduled` přepočte na nový klíč. Bez toho by
   nový news-engine zapsal znovu celý kalendář týdne (jiný hash = nový řádek) a
   `update_actuals` by řádky nenašel. Kolize nejsou možné (titulek + den byl unikátní,
   titulek + minuta je jemnější); skript je přesto kontroluje a s kolizí skončí chybou.
2. **backfill** — stáhne týdny historické stránky FF (`ffhistory.fetch_week`, à 2 s) od
   nejstaršího řádku FF v DB a doplní položky, které v DB chybí. Párování po skupinách
   „řada + den UTC“ (= identita starého klíče): položka historie se páruje s řádkem DB
   nejdřív podle FF `id` (`raw.id`, má ho jen backfill z 29. 7.), pak podle minuty.
   Nespárované položky se zapíšou jen ve skupině, kde nezůstal nespárovaný řádek DB.
   Jeden nespárovaný řádek a jedna položka = **přeplánovaný release** (živý sběr uložil
   čas, který FF později posunul): nezapisuje se nic, report ho vypíše. Víc nespárovaných
   na obou stranách = nejasné, také jen v reportu. Řádky se nemažou ani nepřepisují.
   Po zápisu `ffhistory.recompute_surprise_z` (celý přepočet, idempotentní).

Reakce, klasifikaci a predikce nových řádků doplní běžící news-engine (`ReactionJob`
bere eventy bez reakce bez ohledu na stáří, klasifikace dá směr ze `surprise_z`), stejně
jako u backfillu kalendáře 29. 7.

**Pořadí nasazení:** zastavit news-engine → záloha PG → `--apply` → spustit news-engine
s #1298. Starý news-engine po rehashi zapisuje duplicity (počítá starý klíč).
**Návrat na starší image:** zastavit news-engine → `--revert` → spustit starší image.
`--revert` vrátí denní klíč nejstaršímu řádku každé skupiny „řada + den“; doplněné druhé
releasy si nechají klíč s minutou (starý news-engine je pak nezná a nevadí mu).

Dry-run (výchozí) jen čte (PG `default_transaction_read_only`) a vypíše počty. Stažené
stránky ukládá do `--cache-dir` (další běh je nestahuje znovu). Idempotentní: druhý
`--apply` nic nezmění.

Použití z hostitele (PG produkce na :55432, heslo z `GEXLENS_PG_PASSWORD`, nevypisuje se):
    uv run python scripts/backfill_ff_scheduled_1298.py --cache-dir <adresář>
    pwsh scripts/backup-postgres.ps1
    uv run python scripts/backfill_ff_scheduled_1298.py --cache-dir <adresář> --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import bindparam, func, select, update  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.newstext import (  # noqa: E402
    SCHEDULED_KIND,
    dedup_hash,
    event_dedup_hash,
    normalize_title,
    scheduled_key,
)
from gexlens_engine.storage.sentiment import news_events  # noqa: E402
from gexlens_news.ffhistory import (  # noqa: E402
    FetchPage,
    extract_days,
    fetch_week,
    normalize_entry,
    recompute_surprise_z,
    week_param,
)
from gexlens_news.model import NewsEvent  # noqa: E402
from gexlens_news.store import FF_SOURCE, NewsWriter  # noqa: E402

THROTTLE_S = 2.0
EXAMPLES = 15
#: Řady, které report vypisuje všechny (headline releasy z #1298)
HIGH_IMPACT = "high"


def _utc(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.UTC) if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _group(title: str, ts_event: dt.datetime) -> tuple[str, dt.date]:
    """Identita starého klíče: řada + den UTC."""
    return normalize_title(title), _utc(ts_event).date()


# ── 1) Rehash ──────────────────────────────────────────────────────


@dataclass
class RehashReport:
    rows: int = 0
    changed: int = 0
    reverted_kept: int = 0


def _scheduled_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    news_events.c.id,
                    news_events.c.kind,
                    news_events.c.title,
                    news_events.c.ts_event,
                    news_events.c.dedup_hash,
                )
                .where(news_events.c.kind == SCHEDULED_KIND)
                .order_by(news_events.c.id)
            )
        )


def _write_hashes(engine: Engine, changes: Sequence[tuple[int, str]]) -> None:
    """Dvoufázově přes dočasný hash: výměna klíčů mezi řádky by jinak narazila na UNIQUE."""
    stmt = (
        update(news_events)
        .where(news_events.c.id == bindparam("b_id"))
        .values(dedup_hash=bindparam("b_hash"))
    )
    with engine.begin() as conn:
        conn.execute(stmt, [{"b_id": i, "b_hash": f"tmp-1298-{i}"} for i, _ in changes])
        conn.execute(stmt, [{"b_id": i, "b_hash": h} for i, h in changes])


def _check_unique(engine: Engine, planned: dict[int, str]) -> None:
    """Nový hash nesmí patřit jinému řádku (mimo přepočítávané) ani dvěma přepočítávaným."""
    counts = Counter(planned.values())
    doubled = [h for h, n in counts.items() if n > 1]
    if doubled:
        raise SystemExit(f"Kolize nového klíče mezi řádky scheduled: {len(doubled)} — konec")
    hashes = list(planned.values())
    foreign: list[int] = []
    with engine.connect() as conn:
        for start in range(0, len(hashes), 5000):
            chunk = hashes[start : start + 5000]
            for row in conn.execute(
                select(news_events.c.id, news_events.c.dedup_hash).where(
                    news_events.c.dedup_hash.in_(chunk)
                )
            ):
                if planned.get(int(row.id)) != row.dedup_hash:
                    foreign.append(int(row.id))
    if foreign:
        raise SystemExit(f"Nový klíč už má jiný řádek: id {foreign[:20]} — konec")


def rehash(engine: Engine, *, apply: bool, revert: bool = False) -> RehashReport:
    """Přepočet `dedup_hash` řádků scheduled na klíč s minutou (nebo zpět na den)."""
    rows = _scheduled_rows(engine)
    report = RehashReport(rows=len(rows))
    planned: dict[int, str] = {}
    first_of_group: set[tuple[str, dt.date]] = set()
    for row in rows:
        ts = _utc(row.ts_event)
        if revert:
            group = _group(row.title, ts)
            if group in first_of_group:
                # Doplněný druhý release téhož dne si nechá klíč s minutou
                target = event_dedup_hash(row.kind, row.title, ts)
                report.reverted_kept += 1
            else:
                first_of_group.add(group)
                target = dedup_hash(row.title, ts)
        else:
            target = event_dedup_hash(row.kind, row.title, ts)
        planned[int(row.id)] = target
    stored = {int(row.id): str(row.dedup_hash) for row in rows}
    changes = [(i, h) for i, h in planned.items() if h != stored[i]]
    report.changed = len(changes)
    _check_unique(engine, planned)
    if apply and changes:
        _write_hashes(engine, changes)
    return report


# ── 2) Backfill ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DbRow:
    id: int
    title: str
    ts_event: dt.datetime
    ff_id: str | None


@dataclass
class BackfillReport:
    weeks: int = 0
    weeks_failed: list[str] = field(default_factory=list)
    history: int = 0
    db_rows: int = 0
    missing: list[NewsEvent] = field(default_factory=list)
    rescheduled: list[tuple[DbRow, NewsEvent]] = field(default_factory=list)
    unclear: list[tuple[tuple[str, dt.date], int, int]] = field(default_factory=list)
    db_only: int = 0
    written: int = 0
    surprise_updated: int = 0


def history_weeks(first: dt.date, today: dt.date) -> list[str]:
    """Parametry `?week=` od týdne před `first` po týden s `today` (pondělky, chronologicky).

    Týden navíc na začátku: stránka FF začíná nedělí v pásmu stránky, takže položka
    z neděle nebo z okraje dne v UTC může ležet na stránce předchozího pondělí.
    """
    monday = first - dt.timedelta(days=first.weekday() + 7)
    last = today - dt.timedelta(days=today.weekday())
    weeks = []
    while monday <= last:
        weeks.append(week_param(monday))
        monday += dt.timedelta(days=7)
    return weeks


def cached_fetch(
    cache_dir: Path | None,
    fetch: FetchPage,
    *,
    throttle_s: float,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchPage:
    """Stránka z cache, jinak stažení (à `throttle_s`) a uložení."""

    def get(week: str) -> str:
        path = cache_dir / f"{week}.html" if cache_dir is not None else None
        if path is not None and path.exists():
            return path.read_text(encoding="utf-8")
        sleep(throttle_s)
        html = fetch(week)
        if path is not None:
            path.write_text(html, encoding="utf-8")
        return html

    return get


def _db_rows(engine: Engine, start: dt.datetime, end: dt.datetime) -> list[DbRow]:
    stmt = select(
        news_events.c.id, news_events.c.title, news_events.c.ts_event, news_events.c.raw
    ).where(
        news_events.c.source == FF_SOURCE,
        news_events.c.kind == SCHEDULED_KIND,
        news_events.c.ts_event >= start,
        news_events.c.ts_event <= end,
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    out = []
    for row in rows:
        raw = row.raw if isinstance(row.raw, dict) else {}
        ff_id = raw.get("id")
        out.append(
            DbRow(
                id=int(row.id),
                title=str(row.title),
                ts_event=_utc(row.ts_event),
                ff_id=str(ff_id) if ff_id is not None else None,
            )
        )
    return out


def match_group(
    history: Sequence[NewsEvent], db: Sequence[DbRow]
) -> tuple[list[NewsEvent], list[NewsEvent], list[DbRow]]:
    """Spáruje skupinu „řada + den“: vrací (nespárovaná historie, …, nespárované řádky DB).

    Nejdřív podle FF `id`, pak podle minuty releasu. Prostřední prvek = spárované položky
    historie (pro report se nepoužívá, drží čitelnost testu).
    """
    rest_db = list(db)
    unmatched: list[NewsEvent] = []
    matched: list[NewsEvent] = []
    for event in history:
        ff_id = event.raw.get("id")
        hit = next(
            (r for r in rest_db if ff_id is not None and r.ff_id == str(ff_id)),
            None,
        ) or next(
            (
                r
                for r in rest_db
                if scheduled_key(r.title, r.ts_event) == scheduled_key(event.title, event.ts_event)
            ),
            None,
        )
        if hit is None:
            unmatched.append(event)
        else:
            rest_db.remove(hit)
            matched.append(event)
    return unmatched, matched, rest_db


def plan_backfill(
    history: Iterable[NewsEvent], db: Iterable[DbRow], report: BackfillReport
) -> list[NewsEvent]:
    """Položky historie k zápisu; přeplánované a nejasné skupiny jen do reportu."""
    by_group_h: dict[tuple[str, dt.date], dict[str, NewsEvent]] = defaultdict(dict)
    for event in history:
        # Táž položka dvakrát (týž klíč) je jeden release
        by_group_h[_group(event.title, event.ts_event)].setdefault(
            scheduled_key(event.title, event.ts_event), event
        )
    by_group_db: dict[tuple[str, dt.date], list[DbRow]] = defaultdict(list)
    for row in db:
        by_group_db[_group(row.title, row.ts_event)].append(row)

    to_write: list[NewsEvent] = []
    for group in sorted(set(by_group_h) | set(by_group_db), key=lambda g: (g[1], g[0])):
        events = sorted(by_group_h.get(group, {}).values(), key=lambda e: e.ts_event)
        unmatched, _, rest_db = match_group(events, by_group_db.get(group, []))
        if not unmatched:
            report.db_only += len(rest_db)
            continue
        if not rest_db:
            to_write.extend(unmatched)
        elif len(unmatched) == 1 and len(rest_db) == 1:
            report.rescheduled.append((rest_db[0], unmatched[0]))
        else:
            report.unclear.append((group, len(unmatched), len(rest_db)))
    report.missing = to_write
    return to_write


def backfill(
    engine: Engine,
    *,
    apply: bool,
    fetch: FetchPage,
    now: dt.datetime,
    log: Callable[[str], None] = print,
) -> BackfillReport:
    report = BackfillReport()
    with engine.connect() as conn:
        first = conn.execute(
            select(func.min(news_events.c.ts_event)).where(
                news_events.c.source == FF_SOURCE, news_events.c.kind == SCHEDULED_KIND
            )
        ).scalar()
    if first is None:
        return report
    first_ts = _utc(first)
    history: list[NewsEvent] = []
    for week in history_weeks(first_ts.date(), now.date()):
        try:
            html = fetch(week)
        except Exception as error:  # noqa: BLE001 — chyba týdne se hlásí, běh pokračuje
            report.weeks_failed.append(f"{week} ({type(error).__name__})")
            continue
        report.weeks += 1
        count = 0
        for day in extract_days(html):
            for entry in day.get("events") or []:
                if not isinstance(entry, dict):
                    continue
                event = normalize_entry(entry, fetched_at=now)
                # Jen proběhlé releasy v rozsahu DB; budoucí zapíše živý collector
                if event is not None and first_ts <= event.ts_event <= now:
                    history.append(event)
                    count += 1
        if count == 0:
            report.weeks_failed.append(f"{week} (0 položek)")
        log(f"  {week}: {count} položek")
    report.history = len(history)
    db = _db_rows(engine, first_ts, now)
    report.db_rows = len(db)
    to_write = plan_backfill(history, db, report)
    if apply and to_write:
        report.written = NewsWriter(engine).write(to_write)
        report.surprise_updated = recompute_surprise_z(engine)
    return report


# ── Report a běh ───────────────────────────────────────────────────


def _event_line(event: NewsEvent) -> str:
    return (
        f"| {event.ts_event:%Y-%m-%d %H:%M} | {event.title} | {event.raw.get('impactName')} "
        f"| {event.forecast} | {event.actual} | {event.raw.get('id')} |"
    )


def format_report(rehashed: RehashReport, filled: BackfillReport | None, *, apply: bool) -> str:
    verb = "zapsáno" if apply else "dry-run"
    lines = [
        f"## Rehash ({verb})",
        f"Řádků scheduled: {rehashed.rows}, nový klíč: {rehashed.changed}"
        + (
            f", druhý release dne ponechán s minutou: {rehashed.reverted_kept}"
            if rehashed.reverted_kept
            else ""
        ),
    ]
    if filled is None:
        return "\n".join(lines)
    missing = filled.missing
    by_currency = Counter(str(e.raw.get("currency") or "?") for e in missing)
    by_impact = Counter(str(e.raw.get("impactName") or "?") for e in missing)
    lines += [
        "",
        f"## Backfill ({verb})",
        f"Týdnů: {filled.weeks}, chyb: {len(filled.weeks_failed)}"
        + (f" ({', '.join(filled.weeks_failed[:10])})" if filled.weeks_failed else ""),
        f"Položek historie: {filled.history}, řádků FF v DB: {filled.db_rows}",
        f"Chybí v DB: {len(missing)} (měny {dict(by_currency.most_common())}, "
        f"dopad {dict(by_impact.most_common())})",
        f"Přeplánované (řádek DB s jiným časem než FF, nezapisuje se): {len(filled.rescheduled)}",
        f"Nejasné skupiny (nezapisuje se): {len(filled.unclear)}",
        f"Řádky DB bez položky v historii: {filled.db_only}",
    ]
    if apply:
        lines.append(f"Zapsáno: {filled.written}, surprise_z přepočteno: {filled.surprise_updated}")
    usd_high = [
        e for e in missing if e.title.startswith("USD ") and e.raw.get("impactName") == HIGH_IMPACT
    ]
    if usd_high:
        lines += [
            "",
            "USD high chybějící:",
            "| čas UTC | řada | dopad | forecast | actual | FF id |",
            "|---|---|---|---|---|---|",
        ]
        lines += [_event_line(e) for e in usd_high]
    other = [e for e in missing if e not in usd_high]
    if other:
        lines += ["", f"Ostatní chybějící (prvních {EXAMPLES}):"]
        lines += [_event_line(e) for e in other[:EXAMPLES]]
    if filled.rescheduled:
        shift = Counter(
            round((e.ts_event - r.ts_event).total_seconds() / 60) for r, e in filled.rescheduled
        )
        live = sum(1 for r, _ in filled.rescheduled if r.ff_id is None)
        lines += [
            "",
            f"Přeplánované: z živého sběru {live}, posun v minutách {dict(shift.most_common(10))}",
        ]
        for row, event in filled.rescheduled[:EXAMPLES]:
            lines.append(
                f"  id {row.id} {row.title}: "
                f"DB {row.ts_event:%Y-%m-%d %H:%M} → FF {event.ts_event:%Y-%m-%d %H:%M}"
            )
    if filled.unclear:
        lines += ["", "Nejasné skupiny (řada, den, nespárovaná historie, nespárované DB):"]
        lines += [f"  {g[0]} {g[1]}: {h} / {d}" for g, h, d in filled.unclear[:EXAMPLES]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", default=None, help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument("--apply", action="store_true", help="zapsat (jinak dry-run jen čtením)")
    parser.add_argument(
        "--revert", action="store_true", help="vrátit denní klíč (návrat na starší news-engine)"
    )
    parser.add_argument("--cache-dir", type=Path, default=None, help="stažené stránky FF")
    parser.add_argument("--throttle", type=float, default=THROTTLE_S)
    args = parser.parse_args(argv)

    engine = make_engine(database_url(args.db), read_only=not args.apply)
    if args.revert:
        print(format_report(rehash(engine, apply=args.apply, revert=True), None, apply=args.apply))
        return 0
    if args.cache_dir is not None:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    rehashed = rehash(engine, apply=args.apply)
    fetch = cached_fetch(args.cache_dir, fetch_week, throttle_s=args.throttle)
    filled = backfill(engine, apply=args.apply, fetch=fetch, now=dt.datetime.now(dt.UTC))
    print(format_report(rehashed, filled, apply=args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
