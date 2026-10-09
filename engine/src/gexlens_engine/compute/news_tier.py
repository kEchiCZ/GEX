"""Tier obsahu zprávy (ADR-0059 body 1–2) — jediný zdroj pravdy.

Čistá funkce nad zdrojem a surovým payloadem `(source, raw)`. Výsledek se
ukládá při ingestu do `news_events.content_tier`: obě zápisové cesty
(news-engine `NewsWriter`, engine `newsticks`) a backfill historie
(`scripts/backfill_content_tier.py`) volají tutéž funkci. Uložit ho jde,
protože vstupy (zdroj, autor, příznak kurátora) se po zápisu nemění —
na rozdíl od importance (ADR-0045 bod 5).

Tiery:

* 1 — oficiální zdroj (úřad sám),
* 2 — headline feed (krátké zprávy v řádu sekund),
* 3 — článek nebo analýza,
* None — mimo tiery (kalendář, sociální obsah bez kurátora).

„Tier“ tady není role zdroje (`news_sources.tier` core/extra/test) ani stupeň
významnosti (`news_significance.significance_tier`). Nový zdroj musí dostat
výslovné rozhodnutí v `FIXED_TIERS` nebo ve funkci — test pokrytí registru
`NEWS_SOURCE_SEED` jinak spadne.
"""

from collections.abc import Mapping

OFFICIAL = 1
HEADLINE = 2
ARTICLE = 3

#: Alpaca (Benzinga) nese headliny jen od autora Newsdesk; Insights a ostatní
#: autoři jsou články (audit #1473: Newsdesk 71 % objemu)
ALPACA_HEADLINE_AUTHOR = "Benzinga Newsdesk"
#: Engine zapisuje IBKR pásky jako `ibkr_{provider}` (#922); dostupní
#: provideři jsou sloupky, newslettery a analytické akce (#1474) — články
IBKR_PREFIX = "ibkr_"

#: Zdroje, jejichž tier nezávisí na payloadu (None = výslovně mimo tiery)
FIXED_TIERS: Mapping[str, int | None] = {
    "fed_rss": OFFICIAL,
    "rss_news": ARTICLE,
    "finnhub": ARTICLE,
    "rss_user": ARTICLE,
    "forexfactory": None,
    "reddit_rss": None,
}
#: Zdroje, o jejichž tieru rozhoduje payload (autor, kurátor)
PAYLOAD_SOURCES = frozenset({"alpaca", "bluesky"})


def content_tier(source: str, raw: Mapping[str, object] | None) -> int | None:
    """Tier obsahu 1–3, None = mimo tiery nebo neznámý zdroj (viz docstring modulu)."""
    payload = raw or {}
    if source == "alpaca":
        return HEADLINE if payload.get("author") == ALPACA_HEADLINE_AUTHOR else ARTICLE
    if source == "bluesky":
        return HEADLINE if payload.get("curated") is True else None
    if source.startswith(IBKR_PREFIX):
        return ARTICLE
    return FIXED_TIERS.get(source)
