"""Karta „Breaking news“ — výběr, potvrzení, skupina a téma (ADR-0059 bod 4, #1491).

Čisté funkce. Všechno se počítá při čtení a nic se neukládá: importance
přepisuje LLM i ruční korekce (ADR-0045 bod 5) a efektivní tier závisí na
okamžiku *t*, protože kopie z jiných zdrojů jsou viditelné od `fetched_at`
(`compute/news_tier.effective_tier`).

* **Výběr:** zpráva je breaking, když je významná (`is_significant`, ADR-0045
  bod 5) a má efektivní tier 1–3. Tier NULL (kalendář, Reddit, Bluesky bez
  kurátora) na kartu nejde.
* **Potvrzení:** efektivní tier 1–2. Zpráva tier 3 jde na kartu hned se
  štítkem „článek, zatím nepotvrzeno“ a štítek zmizí s první viditelnou kopií
  tier 1–2 (rozhodnutí vlastníka 9. 10. 2026, revize ADR-0059 bod 4).
  Kopie je jen **táž zpráva** (dedup, Jaccard ≥ 0,9); jiný titulek o téže
  události zprávu nepotvrdí, to umí až shluky (E-6.6, E-6.17).
* **Výsledky firem** na kartu nejdou ani u mega caps: předregistrované měření
  výjimky kritérium nesplnilo (`docs/research/1491-mega-caps-earnings.md`).
* **Skupina** je zobrazovací mapování stávající `category`; slovník kategorií
  se nemění, protože je klíčem modelu a pravidla kontaminace K1.
* **Téma** je jedna entita, první shoda podle pořadí `THEMES` — nejdřív
  v předmětu titulku, pak v celém titulku, stejně jako kategorie
  (`classifier.classify_category`). Vzory jsou jen z klasifikátoru; vlastní
  slovník nevzniká. Chybějící téma se doplní do klasifikátoru jako nová verze
  s golden testem. Identifikátor je `theme`, protože `topic` už v API znamená
  index kategorie (`topic_value`).
"""

import re
from collections.abc import Mapping

from gexlens_engine.compute.news_significance import is_significant
from gexlens_engine.compute.news_tier import ARTICLE, HEADLINE, OFFICIAL
from gexlens_news import classifier

#: Tiery, se kterými smí významná zpráva na kartu (revize 9. 10.: i článek)
CARD_TIERS = frozenset({OFFICIAL, HEADLINE, ARTICLE})
#: Potvrzená zpráva má viditelné doručení z oficiálního zdroje nebo headline feedu
CONFIRMED_MAX_TIER = HEADLINE

GROUP_MACRO = "macro"
GROUP_CENTRAL_BANKS = "central_banks"
GROUP_GEOPOLITICS = "geopolitics"
GROUP_COMPANIES = "companies"
GROUP_OTHER = "other"
#: Skupina karty podle `category` (tabulka ADR-0059 bod 4); „výzkum bank“ nevzniká
CATEGORY_GROUPS: Mapping[str, str] = {
    "MACRO_INFLATION": GROUP_MACRO,
    "MACRO_LABOR": GROUP_MACRO,
    "MACRO_GROWTH": GROUP_MACRO,
    "FED": GROUP_CENTRAL_BANKS,
    "GEOPOLITICS": GROUP_GEOPOLITICS,
    "ENERGY": GROUP_GEOPOLITICS,
    "EARNINGS": GROUP_COMPANIES,
    "TECH": GROUP_COMPANIES,
    "CRYPTO": GROUP_OTHER,
    "OTHER": GROUP_OTHER,
}
#: Zdroje centrální banky — skupina bez ohledu na kategorii (ECB přidá E-6.25)
CENTRAL_BANK_SOURCES = frozenset({"ecb"})


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{pattern})\b", re.I | re.X)


#: Slovník témat; pořadí rozhoduje (první shoda vyhrává). Klíč se smí opakovat,
#: když téma pokrývá víc vzorů klasifikátoru.
THEMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Regionální průzkum Fedu je růst, ne Fed — stejně jako `classifier._category`
    ("growth", classifier.REGIONAL_FED_DATA),
    ("fed", classifier.FED_TOPIC),
    ("tariffs", classifier.TARIFF_TOPIC),
    ("tariffs", classifier.TRADE_DEAL),
    *((actor, _words(pattern)) for actor, pattern in classifier.REGIME_ACTORS.items()),
    ("energy", classifier.ENERGY_TOPIC),
    ("inflation", classifier.INFLATION_SUBJECT),
    ("labor", classifier.LABOR_IMP),
    ("growth", classifier.GROWTH),
    ("fiscal", classifier.FISCAL),
    ("fiscal", classifier.US_DOWNGRADE),
)


def is_breaking(
    effective_tier: int | None, kind: str | None, importance: int | None, category: str | None
) -> bool:
    """Zpráva patří na kartu: významná a s efektivním tierem 1–3 v čase čtení."""
    return effective_tier in CARD_TIERS and is_significant(kind, importance, category)


def is_confirmed(effective_tier: int | None) -> bool:
    """Potvrzená zpráva: viditelné doručení tier 1–2; tier 3 nese štítek „nepotvrzeno“."""
    return effective_tier is not None and effective_tier <= CONFIRMED_MAX_TIER


def card_group(category: str | None, source: str) -> str:
    """Skupina karty; neklasifikovaná nebo neznámá kategorie patří do „ostatní“."""
    if source in CENTRAL_BANK_SOURCES:
        return GROUP_CENTRAL_BANKS
    return CATEGORY_GROUPS.get(category or "", GROUP_OTHER)


def _first_theme(text: str) -> str | None:
    for key, pattern in THEMES:
        if pattern.search(text):
            return key
    return None


def theme(title: str) -> str | None:
    """Téma titulku: první shoda v předmětu, jinak v celém titulku; None = bez tématu."""
    text = classifier.clean(title)
    return _first_theme(classifier.split_head(text)) or _first_theme(text)
