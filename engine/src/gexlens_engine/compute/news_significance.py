"""Významnost zprávy (#1293, #1305, ADR-0045) — jediný zdroj pravdy.

Čistá funkce nad klasifikací `(kind, importance, category)`. Volají ji
news-engine (shluky upozornění `clusters.py`, předobchodní souhrn `preopen.py`,
WS dávka klasifikace) i API (`/news/markers`, `/news`, `/news/upcoming`), které
ji přikládá k řádku jako pole `significance` — frontend pravidla nekopíruje.
Počítá se při čtení, neukládá se: importance zapisují čtyři cesty (pravidlový
job, LLM, ruční korekce, reklasifikace) a uložený příznak by se s ní rozjel.

Stupně (menší = významnější; řadí dialog markeru i výčet upozornění):

* 0 — kalendář s importance 3 (FF USD High),
* 1 — kalendář s importance 2 (FF USD Medium, rozhodnutí ECB/BoE/BoJ),
* 2 — zpráva s importance 3 (událost: akce Fedu, release US dat, pohyb ropy,
  eskalace, cla),
* 3 — zpráva s importance 2 (téma jako předmět) mimo EARNINGS,
* None — nevýznamná.

Strop podle feedu (sociální sítě bez kurátora 1, agregátory jen události)
a importance kalendáře podle měny řeší klasifikátor (`gexlens_news.classifier`),
proto tu stačí tři vstupy. **Zásadní** zpráva (předobchodní souhrn) je
kalendář nebo zpráva s importance 3 — stupeň ≤ 2 (rozhodnutí uživatele 26. 9.).
"""

SCHEDULED_KIND = "scheduled"
SIGNIFICANT_MIN_IMPORTANCE = 2
HIGH_IMPORTANCE = 3
#: Firemní výsledky nejsou významné ani s importance 2–3 (#1291 varianta B):
#: přepisy hovorů a výsledky drobných firem index nehýbou
EXCLUDED_CATEGORIES = frozenset({"EARNINGS"})

TIER_SCHEDULED_HIGH = 0
TIER_SCHEDULED_MEDIUM = 1
TIER_NEWS_HIGH = 2
TIER_NEWS_MEDIUM = 3
#: Zásadní = kalendář (0, 1) nebo zpráva s importance 3 (2)
KEY_MAX_TIER = TIER_NEWS_HIGH


def significance_tier(kind: str | None, importance: int | None, category: str | None) -> int | None:
    """Stupeň významnosti 0–3, None = nevýznamná (viz docstring modulu)."""
    level = importance or 0
    if level < SIGNIFICANT_MIN_IMPORTANCE:
        return None
    if kind == SCHEDULED_KIND:
        return TIER_SCHEDULED_HIGH if level >= HIGH_IMPORTANCE else TIER_SCHEDULED_MEDIUM
    if category in EXCLUDED_CATEGORIES:
        return None
    return TIER_NEWS_HIGH if level >= HIGH_IMPORTANCE else TIER_NEWS_MEDIUM


def is_significant(kind: str | None, importance: int | None, category: str | None) -> bool:
    """Významná zpráva: shluk upozornění, filtr grafu „Významné“."""
    return significance_tier(kind, importance, category) is not None


def is_key(kind: str | None, importance: int | None, category: str | None) -> bool:
    """Zásadní zpráva: výčet a sklon předobchodního souhrnu (podmnožina významných)."""
    tier = significance_tier(kind, importance, category)
    return tier is not None and tier <= KEY_MAX_TIER
