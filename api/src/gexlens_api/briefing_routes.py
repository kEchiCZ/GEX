"""Verdikt dne z Briefingu (#1090, ADR-0035 §3).

Frontend spočítá verdikt (spíše long / spíše short / bez převahy / počkat na
tisk) z pravidel s pevnými vahami a uloží ho sem — jeden řádek per seance
a symbol, aby šla heuristika vyhodnotit proti průběhu seance (#1091).
Bez uloženého verdiktu by šlo o názor bez zpětné vazby.

Verdikt je předpověď před seancí: od US openu (9:30 ET) se hodnotí pohyb
open → settle, takže pozdější zápis by do předpovědi pustil hodnocený pohyb
(hlas overnight nese aktuální cenu). API proto od openu zápis odmítne —
založení i přepis, i po settle (#1585). Uložený verdikt je nenahraditelný.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.compute.settle import is_trading_session, us_open_ts
from gexlens_engine.storage.briefing_verdicts_store import BriefingVerdictRepository, verdict_stats

VerdictKind = Literal["long", "short", "none", "wait_news"]


class VerdictVoteIn(BaseModel):
    """Jeden hlas hlasování — název složky, hlas a důvod (průhlednost, ADR-0035)."""

    name: str = Field(max_length=32)
    vote: int = Field(ge=-3, le=3)
    reason: str = Field(max_length=200)


class VerdictIn(BaseModel):
    session_date: dt.date
    symbol: str = Field(min_length=1, max_length=16)
    verdict: VerdictKind
    score: int = Field(ge=-20, le=20)
    votes: list[VerdictVoteIn] = Field(default_factory=list, max_length=16)
    rules_version: int = Field(ge=1)


def build_briefing_router(
    repository: MetaRepository,
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> APIRouter:
    router = APIRouter(tags=["briefing"])

    @router.post("/briefing/verdicts", status_code=201)
    def verdict_put(payload: VerdictIn) -> dict[str, Any]:
        """Uloží (nebo před US openem přepíše) verdikt seance pro symbol."""
        if not is_trading_session(payload.session_date):
            # Víkend nemá US open ani settle — verdikt by se nikdy nevyhodnotil (#1309)
            raise HTTPException(422, "Verdikt dne jen pro obchodní den (po–pá)")
        current = now()
        frozen_from = us_open_ts(payload.session_date)
        if current >= frozen_from:
            # Táž hranice jako základ pohybu ve vyhodnocení (briefing_verdicts.session_ohlc)
            raise HTTPException(
                409,
                f"Verdikt seance {payload.session_date.isoformat()} je od US openu "
                f"({frozen_from:%H:%M} UTC) zmrazený — hodnotí se pohyb od openu (#1585)",
            )
        values = payload.model_dump()
        values["votes"] = [vote.model_dump() for vote in payload.votes]
        # Tentýž okamžik jako stráž: uložený `updated_at` je vždy před openem seance
        return repository.briefing_verdict_upsert(values, now=current)

    @router.get("/briefing/verdicts/stats")
    def verdicts_stats(
        symbol: str | None = None, days: int = Query(120, ge=1, le=400)
    ) -> dict[str, object]:
        """Track record verdiktů (#1091): hit-rate per verdikt a per složka hlasování."""
        repository_ = BriefingVerdictRepository(repository.engine())
        repository_.ensure_schema()
        since = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days)
        rows = repository_.evaluated(symbol, since=since)
        return {"symbol": symbol, "days": days, **verdict_stats(rows)}

    @router.get("/briefing/verdicts")
    def verdicts_list(
        symbol: str | None = None, days: int = Query(30, ge=1, le=400)
    ) -> dict[str, object]:
        """Verdikty posledních `days` seancí — podklad vyhodnocení (#1091)."""
        return {"verdicts": repository.briefing_verdicts(symbol, days)}

    return router
