"""Karta „Breaking news“ — výběr, potvrzení, skupina a téma (ADR-0059 bod 4, #1491).

Čisté funkce. Všechno se počítá při čtení a nic se neukládá: importance
přepisuje LLM i ruční korekce (ADR-0045 bod 5) a efektivní tier závisí na
okamžiku *t*, protože kopie z jiných zdrojů jsou viditelné od `fetched_at`
(`compute/news_tier.effective_tier`).

* **Výběr:** zpráva je breaking, když je významná (`is_significant`, ADR-0045
  bod 5) a má efektivní tier 1–3. Tier NULL (kalendář, Reddit, Bluesky bez
  kurátora) na kartu nejde.
* **Čerstvost (varianta C, vlastník 9. 10. večer):** zpráva s efektivním
  tierem 3 jde na kartu jen tehdy, když první doručení přišlo nejvýš 60 min
  po publikaci (`ts_ingested − ts_event`). Šum karty dělaly staré články:
  Finnhub doručuje Reuters s mediánem zpoždění 11 h
  (`docs/research/1491-sum-karty-breaking.md`). Tier 1–2 omezení nemá.
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
* **Dopad na kartě** (ADR-0059 bod 8, E-6.28b): změna ceny ES/NQ od posledního
  close před zprávou a výchylka, průběžně do 5. minuty, pak zafixované.
  Skládá se z týchž čistých funkcí jako `news_reactions` a upozornění
  (`reactions.compute_reactions`, `reactions.measure_excursion`), vzorec se
  nekopíruje. Ze `news_reactions` se nečte, vzniká až po 60 min.
"""

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gexlens_engine.compute.news_significance import is_significant
from gexlens_engine.compute.news_tier import ARTICLE, HEADLINE, OFFICIAL
from gexlens_news import classifier
from gexlens_news.reactions import Bar, compute_reactions, measure_excursion

#: Tiery, se kterými smí významná zpráva na kartu (revize 9. 10.: i článek)
CARD_TIERS = frozenset({OFFICIAL, HEADLINE, ARTICLE})
#: Potvrzená zpráva má viditelné doručení z oficiálního zdroje nebo headline feedu
CONFIRMED_MAX_TIER = HEADLINE
#: Nejvyšší zpoždění příjmu za publikací, se kterým smí na kartu článek (tier 3)
ARTICLE_MAX_INGEST_DELAY = dt.timedelta(minutes=60)

#: Okno dopadu na kartě; v 5. minutě se hodnota zafixuje (ADR-0059 bod 8)
CARD_WINDOW_MIN = 5
#: Stavy dopadu: běží do 5. minuty, pak zafixováno; zpráva při zavřeném trhu
#: dopad nemá (odložená reakce zůstává v `news_reactions`); bez barů je mezera
IMPACT_RUNNING = "running"
IMPACT_FIXED = "fixed"
IMPACT_CLOSED = "closed"
IMPACT_NO_DATA = "no_data"
_MINUTE = dt.timedelta(minutes=1)

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
    effective_tier: int | None,
    kind: str | None,
    importance: int | None,
    category: str | None,
    *,
    ingest_delay: dt.timedelta,
) -> bool:
    """Zpráva patří na kartu: významná, s efektivním tierem 1–3 v čase čtení
    a článek (tier 3) jen čerstvý.

    `ingest_delay` = `ts_ingested − ts_event` prvního doručení.
    """
    if effective_tier not in CARD_TIERS or not is_significant(kind, importance, category):
        return False
    return effective_tier != ARTICLE or ingest_delay <= ARTICLE_MAX_INGEST_DELAY


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


@dataclass(frozen=True)
class CardImpact:
    """Dopad zprávy na jeden symbol v čase čtení.

    `ret_bp` = změna ceny od posledního close před zprávou (hlavní číslo),
    `range_bp` = high − low okna, `excursion_bp` + `excursion_direction` =
    výchylka ADR-0043 od minuty zprávy (None = zatím nejde změřit, např. bez
    hodiny barů pro σ po otevření). `elapsed_min` = celé minuty od zprávy.
    """

    state: str
    elapsed_min: int
    ret_bp: float | None = None
    range_bp: float | None = None
    excursion_bp: float | None = None
    excursion_direction: int | None = None
    contaminated: bool = False


def card_impact(
    ts_event: dt.datetime,
    bars: Sequence[Bar],
    *,
    at: dt.datetime,
    other_event_ts: Sequence[dt.datetime] = (),
    market_closed: bool,
) -> CardImpact:
    """Dopad zprávy v čase `at` z minutových barů viditelných v `at`.

    Volající předá jen bary, které v `at` existovaly (point-in-time), a
    kontaminující eventy podle K1 (`reactions.contaminates`). Zpráva při
    zavřeném trhu (`market_closed`) dopad nemá. Do 5. minuty se hodnota
    počítá z barů, které už jsou, potom je zafixovaná na okně 5 min.
    """
    elapsed = max(0, int((at - ts_event) / _MINUTE))
    if market_closed:
        return CardImpact(IMPACT_CLOSED, elapsed)
    fixed = at >= ts_event + dt.timedelta(minutes=CARD_WINDOW_MIN)
    state = IMPACT_FIXED if fixed else IMPACT_RUNNING
    reactions = compute_reactions(
        ts_event, bars, windows=(CARD_WINDOW_MIN,), other_event_ts=other_event_ts
    )
    if not reactions or reactions[0].deferred:
        # Bez barů po zprávě: do 5. minuty se čeká, potom je to mezera v datech
        # (trh podle rozvrhu otevřený, ale bary chybí) — ne zmrzlé číslo
        return CardImpact(state if not fixed else IMPACT_NO_DATA, elapsed)
    reaction = reactions[0]
    # Výchylka od celé minuty zprávy (jako upozornění, ADR-0043) přes uzavřené
    # minuty; okno roste do 5 min. Okno bez jediného baru by dalo −∞
    start = ts_event.replace(second=0, microsecond=0)
    window = min(CARD_WINDOW_MIN, max(0, int((at - start) / _MINUTE)))
    excursion = None
    if window and any(start <= bar.ts < start + window * _MINUTE for bar in bars):
        excursion = measure_excursion(bars, start, window)
    return CardImpact(
        state,
        elapsed,
        ret_bp=reaction.ret_bp,
        range_bp=reaction.range_bp,
        excursion_bp=excursion.bp if excursion is not None else None,
        excursion_direction=excursion.direction if excursion is not None else None,
        contaminated=reaction.contaminated,
    )
