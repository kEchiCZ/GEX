"""Verdikt dne zapisuje engine (#1595, rozhodnutí vlastníka 17. 10. 2026).

Jediný zapisovatel tabulky `briefing_verdicts`: pro každou obchodní seanci
a symbol pipeline spočítá verdikt (`compute/dayverdict`, vstupy čte
`scenario_auto.gather_context`) a uloží ho upsertem — od začátku seance (17:00 CT
předchozího dne) každých 5 min, v okně [US open − 15 min, open) každou minutu
(rozhodnutí a1); platí poslední stav před openem. Briefing ukazuje uložený řádek
i se stářím, nepočítá. Stráž od openu (#1585) ověřuje úložiště skutečným časem
zápisu. Automatický scénář přebírá verdikt téže minuty (rozhodnutí d1).

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

#: Okno zápisu každou minutu před US openem (#1595). Vlastní konstanta, ne
#: `GEXLENS_SCENARIO_AUTO_MINUTES_BEFORE_OPEN` — 0 (scénář vypnutý) nesmí vypnout track record
VERDICT_WINDOW_MINUTES = 15
#: Kadence zápisu od začátku seance do okna — ráno je v Briefingu vidět, co se uloží
VERDICT_EARLY_EVERY_MINUTES = 5
#: Strop čtení vstupů z API (běžně ~2,4 s na symbol): visící API by se 12–15 GET
#: po 10 s táhlo minuty a zdrželo minutový cyklus sběru dat (pipeline běží za sebou)
VERDICT_READ_TIMEOUT_S = 20.0


@dataclass
class DayVerdictWriter:
    """Od začátku seance do US openu spočítá a uloží verdikt dne symbolu."""

    symbol: str
    repository: BriefingVerdictRepository
    api: ApiReader

    async def on_minute(
        self, now: dt.datetime, spot: float, runtime: EngineRuntime
    ) -> PreOpenVerdict | None:
        """Verdikt téže minuty (i pro automatický scénář), None bez výpočtu nebo zápisu."""
        session = trading_session_date(now)
        if not verdict_writable(session, now):
            return None  # víkend, svátek (#1309) nebo po openu (#1585)
        window_start = us_open_ts(session) - dt.timedelta(minutes=VERDICT_WINDOW_MINUTES)
        if now < window_start and now.minute % VERDICT_EARLY_EVERY_MINUTES:
            return None  # před oknem jen každých 5 min (~2,4 s čtení API na symbol)
        if not math.isfinite(spot) or spot <= 0:
            logger.warning(
                "%s: verdikt dne — bez platné ceny (%s), minuta %s se nezapíše",
                self.symbol,
                spot,
                f"{now:%H:%M}",
            )
            return None
        try:
            ctx = await asyncio.wait_for(
                gather_context(self.api, self.symbol, runtime.expiry, session, now),
                timeout=VERDICT_READ_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "%s: verdikt dne — čtení vstupů z API nedoběhlo do %g s, minuta %s se nezapíše",
                self.symbol,
                VERDICT_READ_TIMEOUT_S,
                f"{now:%H:%M}",
            )
            return None
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
        return PreOpenVerdict(verdict=verdict, context=ctx, spot=spot, complete=complete)
