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

Efektivní tier a nejdřívější publikace zprávy se neukládají, počítají se při
čtení z prvního doručení a kopií z jiných zdrojů (`news_event_sources`,
ADR-0059 bod 3), viditelných v čase *t*.
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

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


@dataclass(frozen=True)
class SourceCopy:
    """Kopie zprávy z jiného zdroje — řádek `news_event_sources` (ADR-0059 bod 3)."""

    content_tier: int | None
    published_at: dt.datetime
    fetched_at: dt.datetime


def effective_tier(
    first_tier: int | None, copies: Iterable[SourceCopy], *, at: dt.datetime
) -> int | None:
    """Nejnižší tier z prvního doručení a kopií viditelných v `at`, None = žádný.

    Kopie je viditelná od `fetched_at` (point-in-time). Viditelnost prvního
    doručení (`ts_ingested ≤ at`) hlídá volající, bez něj zpráva neexistuje.
    Bez efektivního tieru by o štítku karty rozhodovalo pořadí doručení:
    CNBC RSS před Benzinga Newsdeskem by zprávu nechal jako nepotvrzenou
    (`gexlens_news.breaking.is_confirmed`).
    """
    tiers = [first_tier, *(copy.content_tier for copy in copies if copy.fetched_at <= at)]
    known = [tier for tier in tiers if tier is not None]
    return min(known) if known else None


def earliest_published(
    ts_event: dt.datetime, copies: Iterable[SourceCopy], *, at: dt.datetime
) -> dt.datetime:
    """Nejdřívější publikace `min(ts_event, published_at kopií viditelných v at)`.

    `ts_event` prvního doručení se nepřepisuje, protože od něj se měří reakce.
    """
    return min([ts_event, *(copy.published_at for copy in copies if copy.fetched_at <= at)])
