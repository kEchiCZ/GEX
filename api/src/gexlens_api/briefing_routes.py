"""Verdikt dne (#1090, ADR-0035 §3) — jen čtení.

Verdikt (spíše long / spíše short / bez převahy / počkat na tisk) počítá a ukládá
engine (`verdict_writer`, #1595): od začátku seance každých 5 min, v posledních
15 min před US openem každou minutu; platí poslední stav před openem a od openu
se řádek nemění (stráž `settle.verdict_writable` v úložišti, #1585). Briefing ho
odtud čte, nic nezapisuje — zápis z API (`POST`) skončil s #1595, druhý zapisovatel
by závodil s enginem. Výsledek seance doplní `BriefingVerdictCollector` (#1091).
"""

import datetime as dt

from fastapi import APIRouter, Query

from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.storage.briefing_verdicts_store import BriefingVerdictRepository, verdict_stats


def build_briefing_router(repository: MetaRepository) -> APIRouter:
    router = APIRouter(tags=["briefing"])
    ensured: list[BriefingVerdictRepository] = []

    def verdicts_repository() -> BriefingVerdictRepository:
        """Úložiště s aditivními sloupci (výsledek #1091, `missing` #1595) — API může
        naběhnout dřív než engine, výpis by bez nich spadl. Jednou za proces."""
        if not ensured:
            repository_ = BriefingVerdictRepository(repository.engine())
            repository_.ensure_schema()
            ensured.append(repository_)
        return ensured[0]

    @router.get("/briefing/verdicts/stats")
    def verdicts_stats(
        symbol: str | None = None, days: int = Query(120, ge=1, le=400)
    ) -> dict[str, object]:
        """Track record verdiktů (#1091): hit-rate per verdikt a per složka hlasování."""
        since = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days)
        rows = verdicts_repository().evaluated(symbol, since=since)
        return {"symbol": symbol, "days": days, **verdict_stats(rows)}

    @router.get("/briefing/verdicts")
    def verdicts_list(
        symbol: str | None = None, days: int = Query(30, ge=1, le=400)
    ) -> dict[str, object]:
        """Verdikty posledních `days` seancí — podklad vyhodnocení (#1091); `missing` =
        chybějící vstupy výpočtu ([] úplný, null řádek z Briefingu před #1595)."""
        verdicts_repository()
        return {"verdicts": repository.briefing_verdicts(symbol, days)}

    return router
