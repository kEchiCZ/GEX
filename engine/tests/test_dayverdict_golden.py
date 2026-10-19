"""Golden verdiktu dne (#1595): engine je jediný zapisovatel `briefing_verdicts`.

Očekávané hodnoty fixture spočítala dosavadní frontendová implementace
(`instrument/daysummary.ts`, rules v3) před jejím odstraněním — engine musí
uložit přesně to, co Briefing ukazoval: verdikt, skóre i hlasy s důvody.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.dayverdict import VERDICT_RULES_VERSION, VerdictInput, day_verdict
from gexlens_engine.compute.setups import CUMDELTA_PRINTS
from gexlens_engine.compute.trend import TrendReport

FIXTURE = Path(__file__).parent / "golden" / "dayverdict_1595.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _input(raw: dict[str, Any]) -> VerdictInput:
    trend = raw["trend"]
    return VerdictInput(
        trend=None
        if trend is None
        else TrendReport(
            higher=trend["higher"],
            lower=trend["lower"],
            expected=trend["expected"],
            by_timeframe=(),
        ),
        positive_gamma=raw["positive_gamma"],
        tendency_band=raw["tendency_band"],
        sentiment_state=raw["sentiment_state"],
        sentiment_unconfirmed=raw["sentiment_unconfirmed"],
        price=raw["price"],
        prev_close=raw["prev_close"],
        oi_call_delta=raw["oi_call_delta"],
        oi_put_delta=raw["oi_put_delta"],
        oi_call_total=raw["oi_call_total"],
        oi_put_total=raw["oi_put_total"],
        news_before_open=raw["news_before_open"],
        cliff_share=raw["cliff_share"],
        thin_map=raw["thin_map"],
    )


def test_golden_verze_pravidel() -> None:
    """v4 (mechanika v6, ADR-0057 bod 8) má táž pravidla jako v3 — golden platí pro obě."""
    assert DATA["rules_version"] == 3
    expected = 4 if CUMDELTA_PRINTS else 3
    assert expected == VERDICT_RULES_VERSION
    assert len(DATA["cases"]) >= 16


@pytest.mark.parametrize("case", DATA["cases"], ids=[case["name"] for case in DATA["cases"]])
def test_golden_verdikt_dne(case: dict[str, Any]) -> None:
    result = day_verdict(_input(case["input"]))
    expected = case["expected"]
    assert result.verdict == expected["verdict"]
    assert result.score == expected["score"]
    # Hlasy včetně textu důvodu — Briefing je ukazuje tak, jak je engine uložil
    assert [vote.as_dict() for vote in result.votes] == expected["votes"]
