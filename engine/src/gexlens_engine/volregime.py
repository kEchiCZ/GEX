"""Kolektor volatilitního režimu (ADR-0028, #713) — po vzoru `gammacliff`.

Po settle seance přepočte režim; při prvním běhu doplní historii z barů.
Backfill je levný a stojí za to: `session_ranges` čte VŠECHNY bars partice,
kterých máme ~2 roky pro ES i NQ, takže percentily dávají smysl hned první
den místo za rok sbírání.

Rozsah je prakticky ES a NQ. Ostatní instrumenty mají jednotky dnů barů →
`compute_regimes` je pod `MIN_SAMPLE` vynechá a pole zůstane prázdné.
Nikdy se nedosazuje `normal` jako „bezpečný default" — to je tiché selhání.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.compute.volregime import compute_regimes
from gexlens_engine.gammacliff import session_ranges
from gexlens_engine.storage.volregime_store import VolRegimeRepository

logger = logging.getLogger(__name__)

#: Odklad po settle — bary poslední minuty musí stihnout dorazit.
SETTLE_GRACE_MINUTES = 5


@dataclass
class VolRegimeCollector:
    """Jednou po settle přepočte režimy z barů a uloží nové seance."""

    symbol: str
    repository: VolRegimeRepository
    data_dir: Path

    _evaluated_for: dt.date | None = field(default=None, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if now < boundary or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus per seance i při chybě
        await asyncio.to_thread(self._run, now)

    def _run(self, now: dt.datetime) -> None:
        ranges = session_ranges(self.data_dir, self.symbol)
        if not ranges:
            return
        # Přepočítávají se všechny seance, ale zapisují jen chybějící:
        # percentil starších dnů se novými daty nemění (okno se dívá dozadu).
        existing = self.repository.existing_dates(self.symbol)
        written = 0
        for record in compute_regimes(ranges, self.symbol):
            if record.session_date in existing:
                continue
            self.repository.upsert(record, now)
            written += 1
        if written:
            logger.info(
                "%s: volatilitní režim — zapsáno %d seancí (poslední vzorek %d dnů)",
                self.symbol,
                written,
                ranges and len(ranges) or 0,
            )
