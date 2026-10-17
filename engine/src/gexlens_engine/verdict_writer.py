"""Verdikt dne zapisuje engine (#1595, rozhodnutí vlastníka 17. 10. 2026).

Jediný zapisovatel tabulky `briefing_verdicts`: pro každou obchodní seanci
a symbol pipeline každou minutu v okně [US open − 15 min, open) spočítá verdikt
(`compute/dayverdict`, vstupy jako automatický scénář — `scenario_auto.gather_context`)
a uloží ho upsertem; platí poslední stav před openem. Briefing ukazuje uložený
řádek, nepočítá. Stráž od openu (#1585) ověřuje úložiště skutečným časem zápisu.

Neúplný výpočet (výpadek čtení API, chybí levels nebo tendence — třeba první
minuta po restartu) uloží řádek jen tehdy, když ještě žádný není: úplný verdikt
z předchozí minuty nepřepíše. Bez platné ceny se minuta nezapíše vůbec —
hlas overnight by srovnával mrtvou cenu. Jen dopředu, nic se nedopočítává zpětně.
"""

import asyncio
import datetime as dt
import logging
import math
from dataclasses import dataclass

from gexlens_engine.compute.dayverdict import VERDICT_RULES_VERSION, day_verdict
from gexlens_engine.compute.settle import trading_session_date, us_open_ts, verdict_writable
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.scenario_auto import ApiReader, PreOpenVerdict, gather_context, verdict_input
from gexlens_engine.storage.briefing_verdicts_store import (
    BriefingVerdictRepository,
    VerdictFrozenError,
)

logger = logging.getLogger(__name__)

#: Okno zápisu před US openem (#1595). Vlastní konstanta, ne
#: `GEXLENS_SCENARIO_AUTO_MINUTES_BEFORE_OPEN` — 0 (scénář vypnutý) nesmí vypnout track record
VERDICT_WINDOW_MINUTES = 15


@dataclass
class DayVerdictWriter:
    """Každou minutu okna před US openem spočítá a uloží verdikt dne symbolu."""

    symbol: str
    repository: BriefingVerdictRepository
    api: ApiReader

    async def on_minute(
        self, now: dt.datetime, spot: float, runtime: EngineRuntime
    ) -> PreOpenVerdict | None:
        """Verdikt téže minuty (i pro automatický scénář), None mimo okno nebo bez zápisu."""
        session = trading_session_date(now)
        if not verdict_writable(session, now):
            return None  # víkend, svátek (#1309) nebo po openu (#1585)
        if now < us_open_ts(session) - dt.timedelta(minutes=VERDICT_WINDOW_MINUTES):
            return None
        if not math.isfinite(spot) or spot <= 0:
            logger.warning(
                "%s: verdikt dne — bez platné ceny (%s), minuta %s se nezapíše",
                self.symbol,
                spot,
                f"{now:%H:%M}",
            )
            return None
        ctx = await gather_context(self.api, self.symbol, runtime.expiry, session, now)
        verdict = day_verdict(verdict_input(ctx, spot, runtime))
        complete = (
            not ctx.failed and runtime.last_levels is not None and runtime.tendency_band is not None
        )
        try:
            written = await asyncio.to_thread(
                self.repository.upsert_verdict,
                session_date=session,
                symbol=self.symbol,
                verdict=verdict.verdict,
                score=verdict.score,
                votes=[vote.as_dict() for vote in verdict.votes],
                rules_version=VERDICT_RULES_VERSION,
                replace=complete,
            )
        except VerdictFrozenError as exc:
            # Cyklus minuty před openem doběhl až po něm — očekávaný stav, ne chyba
            logger.info("%s: %s", self.symbol, exc)
            return None
        if written:
            state = "uložen" if complete else "neúplný, uložen (dřívější řádek nebyl)"
        else:
            state = "neúplný, nepřepsal uložený řádek"
        logger.info(
            "%s: verdikt dne %s %s (skóre %+d) %s%s",
            self.symbol,
            session.isoformat(),
            verdict.verdict,
            verdict.score,
            state,
            f"; chybí: {', '.join(ctx.missing)}" if ctx.missing else "",
        )
        return PreOpenVerdict(verdict=verdict, context=ctx, complete=complete)
