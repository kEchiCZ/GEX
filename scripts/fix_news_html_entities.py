"""Oprava HTML entit v historii titulků Alpaca a Bluesky (#1500).

Collector Alpaca do 10. 10. 2026 ukládal titulek a perex tak, jak je Benzinga
posílá, tedy HTML-kódované (`Fed&#39;s`, `S&amp;P`); boti na Bluesky píšou
entity doslova. Nové řádky dekóduje collector a klíč dedupu dekóduje
`normalize_title` (rozhodnutí vlastníka 10. 10., varianty B + H2 v #1500).
Tenhle skript srovná historii:

- `title` a `summary`, u Bluesky i `body` (collector ho plní týmž textem)
  → `unescape_entities`, tedy totéž, co dnes dělá collector;
- `dedup_hash` → přepočet z čistého titulku, jinak by nesedělo okno dedupu
  po restartu (`NewsWriter.recent` hash přepočítává) a opakovaný
  `alpaca_news_backfill` by zapsal duplicitu;
- kolize: když přepočtený hash už má **jiný** řádek (kopie z jiného zdroje,
  kterou dedup kvůli entitě minul), řádek si nechá starý hash a zůstane
  samostatným řádkem. Nic se nemaže, `raw` se nemění (#1027).

Jen zdroje s opraveným collectorem (`SOURCES`). Kopie v `news_event_sources`
se váží přes `event_id`, přepočet hashe je neodpojí.

Režimy: výchozí dry-run jen čtením (PG `default_transaction_read_only`)
s počty a příklady; `--apply` zapíše jen řádky, kde se text změní
(idempotentní, druhý běh 0 změn). Před `--apply` na produkci záloha
`pwsh scripts/backup-postgres.ps1`. Spouštět až po nasazení news-enginu
s #1500: jeho okno dedupu (6 h v paměti) počítá hash už z dekódovaného
titulku, takže po skriptu sedí s DB bez restartu. Se starší verzí by okno
drželo staré hashe a news-engine by se po skriptu musel restartovat.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import bindparam, or_, select, update  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.newstext import dedup_hash, unescape_entities  # noqa: E402
from gexlens_engine.storage.sentiment import news_events  # noqa: E402

DEFAULT_BATCH = 5000
SOURCES = ("alpaca", "bluesky")
#: Zdroj, u kterého collector dekóduje i `body` (= celý text postu)
BODY_SOURCE = "bluesky"
#: Příkladů změněného titulku na zdroj v reportu
EXAMPLES = 5


@dataclass
class Report:
    """Počty změn po zdrojích, kolize hashe a příklady pro kontrolu."""

    titles: Counter[str] = field(default_factory=Counter)
    summaries: Counter[str] = field(default_factory=Counter)
    bodies: Counter[str] = field(default_factory=Counter)
    rehashed: Counter[str] = field(default_factory=Counter)
    collisions: list[tuple[int, str]] = field(default_factory=list)
    examples: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    changed: int = 0


def _utc(value: dt.datetime) -> dt.datetime:
    """Den v `dedup_hash` je UTC den — stejně jako u collectoru."""
    return value.astimezone(dt.UTC) if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _batches(engine: Engine, batch: int) -> Iterator[list[Any]]:
    """Kandidáti (`&` v titulku, perexu nebo těle postu) po dávkách podle id (keyset)."""
    last_id = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    news_events.c.id,
                    news_events.c.source,
                    news_events.c.ts_event,
                    news_events.c.title,
                    news_events.c.summary,
                    news_events.c.body,
                    news_events.c.dedup_hash,
                )
                .where(
                    news_events.c.id > last_id,
                    news_events.c.source.in_(SOURCES),
                    or_(
                        news_events.c.title.contains("&"),
                        news_events.c.summary.contains("&"),
                        (news_events.c.source == BODY_SOURCE) & news_events.c.body.contains("&"),
                    ),
                )
                .order_by(news_events.c.id)
                .limit(batch)
            ).fetchall()
        if not rows:
            return
        yield list(rows)
        last_id = int(rows[-1].id)


def _taken(engine: Engine, hashes: set[str]) -> set[str]:
    """Které z hashů už v DB nějaký řádek má."""
    if not hashes:
        return set()
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_events.c.dedup_hash).where(news_events.c.dedup_hash.in_(hashes))
        ).fetchall()
    return {row.dedup_hash for row in rows}


def fix(engine: Engine, *, apply: bool, batch: int = DEFAULT_BATCH) -> Report:
    """Dekóduje titulky, perexy a těla postů; s `apply` zapíše jen změněné řádky."""
    report = Report()
    stmt = (
        update(news_events)
        .where(news_events.c.id == bindparam("b_id"))
        .values(
            title=bindparam("b_title"),
            summary=bindparam("b_summary"),
            body=bindparam("b_body"),
            dedup_hash=bindparam("b_hash"),
        )
    )
    # Hashe přidělené v tomto běhu — dva řádky nesmí dostat týž (UNIQUE)
    assigned: set[str] = set()
    for rows in _batches(engine, batch):
        planned = []
        for row in rows:
            title = unescape_entities(row.title)
            summary = unescape_entities(row.summary) if row.summary is not None else None
            body = row.body
            if body is not None and row.source == BODY_SOURCE:
                body = unescape_entities(body)
            if (title, summary, body) == (row.title, row.summary, row.body):
                continue
            new_hash = row.dedup_hash
            if title != row.title:
                report.titles[row.source] += 1
                examples = report.examples.setdefault(row.source, [])
                if len(examples) < EXAMPLES:
                    examples.append((row.title, title))
                new_hash = dedup_hash(title, _utc(row.ts_event))
            if summary != row.summary:
                report.summaries[row.source] += 1
            if body != row.body:
                report.bodies[row.source] += 1
            planned.append((row, title, summary, body, new_hash))

        taken = _taken(engine, {new for row, *_, new in planned if new != row.dedup_hash})
        changes = []
        for row, title, summary, body, new_hash in planned:
            if new_hash != row.dedup_hash:
                if new_hash in taken or new_hash in assigned:
                    report.collisions.append((int(row.id), row.source))
                    new_hash = row.dedup_hash
                else:
                    report.rehashed[row.source] += 1
                    assigned.add(new_hash)
            changes.append(
                {
                    "b_id": row.id,
                    "b_title": title,
                    "b_summary": summary,
                    "b_body": body,
                    "b_hash": new_hash,
                }
            )
        report.changed += len(changes)
        if apply and changes:
            with engine.begin() as conn:
                conn.execute(stmt, changes)
    return report


def format_report(report: Report, *, apply: bool) -> str:
    lines = ["| zdroj | titulků | perexů | těl | hash přepočten |", "|---|---|---|---|---|"]
    for source in SOURCES:
        lines.append(
            f"| {source} | {report.titles[source]} | {report.summaries[source]} "
            f"| {report.bodies[source]} | {report.rehashed[source]} |"
        )
    verb = "zapsáno" if apply else "ke změně (dry-run)"
    lines.append("")
    lines.append(f"Řádků {verb}: {report.changed}")
    ids = ", ".join(str(row_id) for row_id, _ in report.collisions[:20])
    lines.append(
        f"Kolize hashe (starý hash ponechán, kopie zůstává řádkem): {len(report.collisions)}"
        + (f" — id {ids}{' …' if len(report.collisions) > 20 else ''}" if ids else "")
    )
    for source, examples in report.examples.items():
        lines.append(f"Příklady {source}:")
        for before, after in examples:
            lines.append(f"  {before[:90]!r}\n→ {after[:90]!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", default=None, help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument("--apply", action="store_true", help="zapsat dekódované řádky")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    args = parser.parse_args(argv)

    engine = make_engine(database_url(args.db), read_only=not args.apply)
    report = fix(engine, apply=args.apply, batch=args.batch)
    print(format_report(report, apply=args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
