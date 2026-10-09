"""Doplnění tieru obsahu `news_events.content_tier` do historie (ADR-0059, #1486).

Nové zprávy dostávají tier při ingestu (`NewsWriter`, `newsticks`) z čisté
funkce `compute.news_tier.content_tier`; tenhle skript ji spustí nad historií.
Vstupy (zdroj, autor, příznak kurátora) se po zápisu nemění, takže jde
o jednorázové doplnění, ne o pravidelný přepočet.

Kurátor Bluesky: příznak `raw.curated` zapisuje collector až od #1291 (25. 9.).
Posty přijaté před prvním postem s příznakem dostanou kurátora podle ADR-0045
bod 8 (`load_curated` z `reclassify_news_rules.py`: autor má post s příznakem,
nebo je v aktuálním seznamu kurátorů). Novější posty se řídí jen příznakem —
jeho absence tehdy znamenala „autor není kurátor“.

Režimy: výchozí dry-run jen čtením (PG `default_transaction_read_only`) s počty
zdroj × tier a počtem změn; `--apply` zapíše jen řádky, kde se tier liší
(idempotentní, druhý běh 0 změn, nic nemaže). Před `--apply` na produkci
záloha `pwsh scripts/backup-postgres.ps1`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import (  # noqa: E402
    database_url,
    load_curated,
    make_engine,
    resolve_authors,
)
from sqlalchemy import bindparam, func, select, update  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.news_tier import content_tier  # noqa: E402
from gexlens_engine.storage.sentiment import news_events  # noqa: E402

DEFAULT_BATCH = 5000
BLUESKY = "bluesky"


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


@dataclass
class Report:
    """Počty zdroj × tier po backfillu a počet změněných řádků."""

    tiers: Counter[tuple[str, int | None]] = field(default_factory=Counter)
    changed: int = 0
    curated_by_list: int = 0


def flag_cutover(engine: Engine) -> dt.datetime | None:
    """Příjem prvního postu Bluesky s příznakem `raw.curated` (od #1291)."""
    with engine.connect() as conn:
        first = conn.execute(
            select(func.min(news_events.c.ts_ingested)).where(
                news_events.c.source == BLUESKY,
                news_events.c.raw["curated"].as_boolean().is_(True),
            )
        ).scalar()
    return _utc(first) if first is not None else None


def _batches(engine: Engine, batch: int) -> Iterator[list[Any]]:
    """Všechny zprávy po dávkách podle id (keyset), každá dávka vlastním spojením."""
    last_id = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    news_events.c.id,
                    news_events.c.source,
                    news_events.c.ts_ingested,
                    news_events.c.content_tier,
                    news_events.c.raw["author"].as_string().label("author"),
                    news_events.c.raw["curated"].as_boolean().label("curated"),
                    news_events.c.raw["did"].as_string().label("did"),
                )
                .where(news_events.c.id > last_id)
                .order_by(news_events.c.id)
                .limit(batch)
            ).fetchall()
        if not rows:
            return
        yield list(rows)
        last_id = int(rows[-1].id)


def row_tier(
    row: Any, curated_dids: frozenset[str], cutover: dt.datetime | None
) -> tuple[int | None, bool]:
    """(tier, kurátor jen ze seznamu) — kurátor ze seznamu jen u postů před příznakem."""
    curated = row.curated is True
    by_list = False
    if (
        row.source == BLUESKY
        and not curated
        and (cutover is None or _utc(row.ts_ingested) < cutover)
        and row.did in curated_dids
    ):
        curated = by_list = True
    return content_tier(row.source, {"author": row.author, "curated": curated}), by_list


def backfill(
    engine: Engine,
    curated_dids: frozenset[str],
    *,
    apply: bool,
    batch: int = DEFAULT_BATCH,
) -> Report:
    """Spočte tier každé zprávy; s `apply` zapíše jen rozdílné hodnoty."""
    cutover = flag_cutover(engine)
    report = Report()
    stmt = (
        update(news_events)
        .where(news_events.c.id == bindparam("b_id"))
        .values(content_tier=bindparam("b_tier"))
    )
    for rows in _batches(engine, batch):
        changes = []
        for row in rows:
            tier, by_list = row_tier(row, curated_dids, cutover)
            report.tiers[(row.source, tier)] += 1
            report.curated_by_list += by_list
            if tier != row.content_tier:
                changes.append({"b_id": row.id, "b_tier": tier})
        report.changed += len(changes)
        if apply and changes:
            with engine.begin() as conn:
                conn.execute(stmt, changes)
    return report


def format_report(report: Report, *, apply: bool) -> str:
    lines = ["| zdroj | tier | zpráv |", "|---|---|---|"]
    for (source, tier), count in sorted(report.tiers.items(), key=lambda item: item[0][0]):
        lines.append(f"| {source} | {'—' if tier is None else tier} | {count} |")
    verb = "zapsáno" if apply else "ke změně (dry-run)"
    lines.append("")
    lines.append(f"Řádků {verb}: {report.changed}")
    lines.append(f"Bluesky kurátor jen podle seznamu (před příznakem): {report.curated_by_list}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", default=None, help="SQLAlchemy URL (jinak z prostředí)")
    parser.add_argument("--apply", action="store_true", help="zapsat rozdílné tiery")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    args = parser.parse_args(argv)

    engine = make_engine(database_url(args.db), read_only=not args.apply)
    curated = load_curated(
        engine, os.environ.get("GEXLENS_NEWS_BLUESKY_CURATED_AUTHORS", ""), resolve_authors
    )
    print(
        f"Kurátoři Bluesky: {len(curated.dids)} DID (s příznakem {curated.flagged}, "
        f"ze seznamu {curated.listed}, nepřeložených {len(curated.unresolved)})"
    )
    report = backfill(engine, curated.dids, apply=args.apply, batch=args.batch)
    print(format_report(report, apply=args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
