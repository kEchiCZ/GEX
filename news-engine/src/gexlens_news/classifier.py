"""Pravidlový klasifikátor (#280, SPEC kap. 4) — čisté funkce bez I/O.

Běží **vždy jako první průchod**, i když je Gemini dostupné: LLM ho jen
přepisuje novou verzí (S11), takže modul funguje i bez klíče a při výpadku
klasifikace degraduje, místo aby se zastavila.

Kategorie i směr jsou keyword mapy — záměrně hloupé a čitelné. Nejde o to
trefit nuance, ale dát každé zprávě bucket, aby vůbec mohla vstoupit do
empirického modelu. Jemnější rozlišení je práce LLM v N3.
"""

import re
from dataclasses import dataclass

# Kategorie z textu. Pořadí rozhoduje — první shoda vyhrává, takže
# specifičtější vzory musí být dřív (FOMC před obecným "rate").
CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FED", re.compile(r"fomc|federal reserve|federal funds|fed chair|beige book|powell", re.I)),
    ("MACRO_INFLATION", re.compile(r"\bcpi\b|\bppi\b|\bpce\b|inflation|price index", re.I)),
    (
        "MACRO_LABOR",
        re.compile(r"non-?farm|payroll|unemployment|jobless|employment change|\bjobs\b", re.I),
    ),
    (
        "MACRO_GROWTH",
        re.compile(
            r"\bgdp\b|retail sales|\bpmi\b|\bism\b|durable goods|industrial production", re.I
        ),
    ),
    (
        "GEOPOLITICS",
        re.compile(r"\bwar\b|missile|invasion|sanction|tariff|ceasefire|airstrike|nato", re.I),
    ),
    ("ENERGY", re.compile(r"crude oil|\bopec\b|natural gas|oil price|refinery", re.I)),
    ("CRYPTO", re.compile(r"bitcoin|ethereum|crypto", re.I)),
    ("EARNINGS", re.compile(r"earnings|quarterly results|guidance|profit beat|revenue miss", re.I)),
    ("TECH", re.compile(r"nvidia|semiconductor|\bai\b|chipmaker|apple|microsoft|meta\b", re.I)),
)

# Zprávy, které trhem hýbou spolehlivě — vysoká důležitost bez ohledu na znění
HIGH_IMPACT = re.compile(
    r"fomc|federal funds|rate decision|\bcpi\b|\bpce\b|non-?farm|payroll|\bgdp\b|"
    r"\bwar\b|invasion|missile|tariff|emergency",
    re.I,
)
MEDIUM_IMPACT = re.compile(
    r"earnings|guidance|\bpmi\b|\bism\b|retail sales|jobless|inflation|opec|sanction", re.I
)

# Směrové fráze. Riziková aktiva: „beats/surges" nahoru, „misses/plunges" dolů;
# geopolitická eskalace je risk-off bez ohledu na sloveso.
BULLISH = re.compile(
    r"\bbeat[s]?\b|surge[sd]?|jump[sd]?|rally|rallie[sd]|soar[sd]?|climb[sd]?|"
    r"gain[sd]?|record high|upgrade[sd]?|stimulus|ceasefire|deal reached",
    re.I,
)
BEARISH = re.compile(
    r"\bmiss(es|ed)?\b|plunge[sd]?|slump[sd]?|tumble[sd]?|sink[s]?|slide[sd]?|"
    r"fall[s]?|drop[sd]?|selloff|sell-off|downgrade[sd]?|warn(s|ed|ing)?|"
    r"\bwar\b|invasion|missile|airstrike|sanction|tariff|default|bankrupt",
    re.I,
)

DEFAULT_CATEGORY = "OTHER"
# Síla pravidlového odhadu — vědomě nízká. Je to hrubý first pass, ne LLM;
# nadhodnocená strength by zkreslila skóre v SentIndexu (SPEC 5.3).
STRENGTH_ONE_SIDED = 0.4
STRENGTH_MIXED = 0.2


@dataclass(frozen=True)
class RuleClassification:
    """Výstup pravidlového průchodu — mapuje se na `news_classifications`."""

    category: str
    importance: int
    direction: int
    strength: float


def classify_category(text: str) -> str:
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return category
    return DEFAULT_CATEGORY


def classify_importance(text: str) -> int:
    if HIGH_IMPACT.search(text):
        return 3
    if MEDIUM_IMPACT.search(text):
        return 2
    return 1


def classify_direction(text: str) -> tuple[int, float]:
    """Směr a síla; při protichůdných signálech raději 0 než tipování.

    Zpráva typu „Stocks fall as chip makers beat estimates" nese obojí —
    pravidlový klasifikátor takové znění nerozplete a nemá předstírat, že ano.
    """
    up = len(BULLISH.findall(text))
    down = len(BEARISH.findall(text))
    if up and down:
        # Mírná převaha rozhoduje, ale se sníženou silou
        if up == down:
            return 0, 0.0
        return (1 if up > down else -1), STRENGTH_MIXED
    if up:
        return 1, STRENGTH_ONE_SIDED
    if down:
        return -1, STRENGTH_ONE_SIDED
    return 0, 0.0


def classify(title: str, summary: str | None = None) -> RuleClassification:
    """Kompletní pravidlový odhad z titulku (a případně shrnutí).

    Kategorie a důležitost se hledají i v shrnutí, směr **jen v titulku** —
    v delším textu se sejde příliš mnoho protichůdných sloves a poměr přestane
    něco znamenat.
    """
    haystack = f"{title} {summary or ''}".strip()
    direction, strength = classify_direction(title)
    return RuleClassification(
        category=classify_category(haystack),
        importance=classify_importance(haystack),
        direction=direction,
        strength=strength,
    )
