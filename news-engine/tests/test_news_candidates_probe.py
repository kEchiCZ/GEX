"""Vyhodnocení robots.txt v sondě kandidátů zdrojů (E-6.22, #1474) podle RFC 9309.

Skript žije ve `scripts/`, načítá se přes importlib jako u ostatních skriptů
s testem. Spouštěcí podnět: `urllib.robotparser` ukončí skupinu prázdným
řádkem a `Disallow: /cgi-bin` ze robots.txt SEC tiše ignoroval.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "news_candidates_probe.py"

# Zkrácená struktura www.sec.gov/robots.txt (8. 10. 2026): jedna skupina `*`
# přerušená prázdnými řádky a komentářem
SEC_ROBOTS = """
User-agent: *
# CSS, JS, Images
Allow: /core/*.css$
Disallow: /core/

#SEC
Allow: /Archives/edgar/data
Disallow: /Archives/bin
Disallow: /cgi-bin
"""

UA = "GEXLens research Jan Novak jan@example.com"


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("news_candidates_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["news_candidates_probe"] = module
    spec.loader.exec_module(module)
    return module


def test_blank_line_does_not_end_group(mod: Any) -> None:
    path = "/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"
    assert mod.robots_allows(SEC_ROBOTS, UA, path) is False


def test_allow_rule_and_unmatched_path(mod: Any) -> None:
    assert mod.robots_allows(SEC_ROBOTS, UA, "/Archives/edgar/data/320193/x.htm") is True
    assert mod.robots_allows(SEC_ROBOTS, UA, "/news/pressreleases.rss") is True


def test_longest_match_and_end_anchor(mod: Any) -> None:
    assert mod.robots_allows(SEC_ROBOTS, UA, "/core/theme.css") is True
    assert mod.robots_allows(SEC_ROBOTS, UA, "/core/theme.css?v=1") is False
    assert mod.robots_allows(SEC_ROBOTS, UA, "/core/app.js") is False


def test_allow_wins_tie(mod: Any) -> None:
    robots = "User-agent: *\nDisallow: /feed\nAllow: /feed\n"
    assert mod.robots_allows(robots, UA, "/feed/x.rss") is True


def test_specific_group_overrides_star(mod: Any) -> None:
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: gexlens\nAllow: /\nDisallow: /private\n"
    assert mod.robots_allows(robots, UA, "/rss.xml") is True
    assert mod.robots_allows(robots, UA, "/private/x") is False
    assert mod.robots_allows(robots, "OtherBot/1.0", "/rss.xml") is False


def test_allowlist_robots_like_bea_apps(mod: Any) -> None:
    robots = "User-agent: *\nDisallow: /\nAllow: /api/\n"
    assert mod.robots_allows(robots, UA, "/rss/rss.xml") is False
    assert mod.robots_allows(robots, UA, "/api/data") is True


def test_empty_disallow_and_no_rules(mod: Any) -> None:
    assert mod.robots_allows("User-agent: *\nDisallow:\n", UA, "/anything") is True
    assert mod.robots_allows("", UA, "/anything") is True
