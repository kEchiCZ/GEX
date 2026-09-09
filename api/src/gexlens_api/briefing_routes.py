"""Verdikt dne z Briefingu (#1090, ADR-0035 §3).

Frontend spočítá verdikt (spíše long / spíše short / bez převahy / počkat na
tisk) z pravidel s pevnými vahami a uloží ho sem — jeden řádek per seance
a symbol, aby šla heuristika vyhodnotit proti průběhu seance (#1091).
Bez uloženého verdiktu by šlo o názor bez zpětné vazby.
"""

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from gexlens_api.meta_repo import MetaRepository

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


def build_briefing_router(repository: MetaRepository) -> APIRouter:
    router = APIRouter(tags=["briefing"])

    @router.post("/briefing/verdicts", status_code=201)
    def verdict_put(payload: VerdictIn) -> dict[str, Any]:
        """Uloží (nebo přepíše) verdikt seance pro symbol."""
        values = payload.model_dump()
        values["votes"] = [vote.model_dump() for vote in payload.votes]
        return repository.briefing_verdict_upsert(values)

    @router.get("/briefing/verdicts")
    def verdicts_list(
        symbol: str | None = None, days: int = Query(30, ge=1, le=400)
    ) -> dict[str, object]:
        """Verdikty posledních `days` seancí — podklad vyhodnocení (#1091)."""
        return {"verdicts": repository.briefing_verdicts(symbol, days)}

    return router
