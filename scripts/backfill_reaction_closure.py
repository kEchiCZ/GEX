"""Doplnění klíče uzavírky `news_reactions.closure_open_ts` z archivu barů (#1311).

Deferred reakce (zpráva při zavřeném trhu) jedné uzavírky — víkend, denní
pauza, svátek — sdílejí základní cenu před uzavřením i první bar po otevření,
tedy i výnos. Model zpráv je od #1311 slučuje do jednoho měření podle
`closure_open_ts` (první obchodovaný bar po zprávě). Nové reakce ho dostávají
z `ReactionJob`; tenhle skript ho doplní historickým deferred řádkům, které ho
nemají. Bez něj se staré řádky dál slučují jen podle času zprávy (#1293)
a `n` deferred bucketů zůstane nafouknuté.

Hodnota je měření z barů, ne kalendář: týž výpočet jako `reactions.compute_reactions`
(první bar ≥ čas zprávy, `first_traded`). Zprávy jedné uzavírky jdou za sebou,
takže se bary čtou jednou na uzavírku.

Režimy: výchozí dry-run (jen počty), `--apply` zapíše (UPDATE jen řádků
s NULL, idempotentní, nic nemaže). Projeví se po nočním přepočtu
`ModelStatsJob`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import and_, create_engine, or_, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events, news_reactions
from gexlens_news.bars import BarsRepository
from gexlens_news.reaction_job import CLOSURE_LOOKAHEAD_DAYS


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def plan_closure_opens(
    engine: Engine, bars: BarsRepository
) -> tuple[list[tuple[int, str, dt.datetime]], int]:
    """(event_id, symbol, první bar) pro deferred řádky bez klíče; + počet bez barů."""
    stmt = (
        select(news_reactions.c.event_id, news_reactions.c.symbol, news_events.c.ts_event)
        .select_from(
            news_reactions.join(news_events, news_events.c.id == news_reactions.c.event_id)
        )
        .where(
            news_reactions.c.closure_open_ts.is_(None),
            or_(
                news_reactions.c.deferred_min.is_(True),
                news_reactions.c.deferred_daily.is_(True),
            ),
        )
        .order_by(news_reactions.c.symbol, news_events.c.ts_event)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    planned: list[tuple[int, str, dt.datetime]] = []
    missing = 0
    # Poslední nalezená uzavírka per symbol: zpráva v [ts, první bar] má týž
    # první bar — žádný bar mezi nimi neleží
    last: dict[str, tuple[dt.datetime, dt.datetime]] = {}
    for event_id, symbol, ts_raw in rows:
        ts_event = _utc(ts_raw)
        cached = last.get(symbol)
        if cached is not None and cached[0] <= ts_event <= cached[1]:
            planned.append((event_id, symbol, cached[1]))
            continue
        window = bars.load_range(
            symbol, ts_event, ts_event + dt.timedelta(days=CLOSURE_LOOKAHEAD_DAYS)
        )
        first = next((bar.ts for bar in window if bar.ts >= ts_event), None)
        if first is None:
            missing += 1
            continue
        last[symbol] = (ts_event, first)
        planned.append((event_id, symbol, first))
    return planned, missing


def apply_plan(engine: Engine, planned: Sequence[tuple[int, str, dt.datetime]]) -> int:
    written = 0
    with engine.begin() as conn:
        for event_id, symbol, first in planned:
            result = conn.execute(
                update(news_reactions)
                .where(
                    and_(
                        news_reactions.c.event_id == event_id,
                        news_reactions.c.symbol == symbol,
                        news_reactions.c.closure_open_ts.is_(None),
                    )
                )
                .values(closure_open_ts=first)
            )
            written += result.rowcount or 0
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
    )
    parser.add_argument("--data", default=os.environ.get("GEXLENS_DATA_DIR", "data"))
    parser.add_argument("--apply", action="store_true", help="zapsat klíč uzavírky")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_DATABASE_URL")
    engine = create_engine(args.db)
    planned, missing = plan_closure_opens(engine, BarsRepository(Path(args.data)))
    closures = len({(symbol, first) for _, symbol, first in planned})
    print(
        f"Deferred řádků bez klíče: {len(planned) + missing}; doplnitelných {len(planned)} "
        f"v {closures} uzavírkách, bez barů {missing}"
    )
    if not args.apply:
        return 0
    print(f"Zapsáno {apply_plan(engine, planned)} řádků.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
