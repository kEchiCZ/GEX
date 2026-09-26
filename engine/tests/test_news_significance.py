"""Významnost zprávy — jediný zdroj pravdy pro upozornění, souhrn i graf (#1293, #1305)."""

import pytest

from gexlens_engine.compute.news_significance import is_key, is_significant, significance_tier


@pytest.mark.parametrize(
    ("kind", "importance", "category", "tier"),
    [
        # Kalendář: importance už nese FF impact podle měny (klasifikátor v2)
        ("scheduled", 3, "MACRO_INFLATION", 0),
        ("scheduled", 2, "FED", 1),
        ("scheduled", 1, "MACRO_INFLATION", None),  # cizí měna nebo Low
        # Zprávy: importance 3 = událost, 2 = téma jako předmět
        ("headline", 3, "FED", 2),
        ("broker", 2, "GEOPOLITICS", 3),
        ("social", 3, "ENERGY", 2),  # kurátor — strop 1 nekurátorů řeší klasifikátor
        ("headline", 1, "FED", None),
        ("headline", None, None, None),  # dosud neklasifikovaná
        # Firemní výsledky nejsou významné ani s importance 3
        ("headline", 3, "EARNINGS", None),
        ("headline", 2, "EARNINGS", None),
        ("headline", 2, None, 3),
    ],
)
def test_significance_tier(
    kind: str, importance: int | None, category: str | None, tier: int | None
) -> None:
    assert significance_tier(kind, importance, category) == tier
    assert is_significant(kind, importance, category) == (tier is not None)


def test_key_is_calendar_or_importance_three() -> None:
    """Zásadní = kalendář (High i Medium) nebo zpráva s importance 3 — bez výčtu kategorií."""
    assert is_key("scheduled", 3, "OTHER")
    assert is_key("scheduled", 2, "FED")
    assert is_key("headline", 3, "ENERGY")  # ropa patří mezi zásadní
    assert is_key("social", 3, "GEOPOLITICS")
    assert not is_key("headline", 2, "FED")
    assert not is_key("social", 2, "OTHER")  # kurátor už není automaticky zásadní
    assert not is_key("headline", 3, "EARNINGS")
    assert not is_key("scheduled", 1, "MACRO_INFLATION")
