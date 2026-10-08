"""Pravidlový klasifikátor v2 (#280, #1293, ADR-0045) — čisté funkce bez I/O.

Běží **vždy jako první průchod**, i když je Gemini dostupné: LLM ho jen
přepisuje novou verzí (S11), takže modul funguje i bez klíče a při výpadku
klasifikace degraduje, místo aby se zastavila.

Verze 1 dávala importance 3 každé zprávě, která klíčové slovo jen **zmínila**
(„payroll tax“ v osobních financích, „tariff refund“ u drobné firmy, Colin
Powell, `icc-cpi.int` v odkazu). Analýza #1293 (400 zpráv, dva hodnotitelé,
κ 0,81) změřila přesnost importance 3 → zásadní 9 %. Verze 2 stojí na
**předmětu titulku** a **zdroji**:

1. **Jen titulek** (očištěný od HTML, odkazů a přípon agentur). Shrnutí
   o importance ani o kategorii nerozhoduje — spouštěč jen ve shrnutí byl
   relevantní ve 14 % případů.
2. **Importance 3 jen pro vzory UDÁLOSTI**: akce Fedu (rozhodnutí, statement,
   minutes, projekce, předseda Fedu), release US dat tier 1 s číslem, pohyb
   ropy, eskalace/deeskalace s aktéry režimu, akce s cly, snížení ratingu USA.
   **Téma** jako předmět (Fed, data tier 2, výnosy, inflace, práce, růst,
   nabídka ropy, válka, cla, obchodní dohoda, shutdown, earnings) má 2,
   zmínka 1.
3. **Předmět vs. zmínka**: spouštěč jen za spojkou („<předmět> as/after/ahead
   of <důvod>“) platí jen do úrovně předmětu; souhrn amerického trhu smí nést
   driver ze zmínky jako 2.
4. **Vylučovací kontexty → 1**: osobní finance, stock picking, přepisy
   earnings calls, šablony, sport, clickbait, rubriky (podcast, poll…),
   emise akcií, Redbook, sankce, menší cizí centrální banky, cizí data,
   titulek začínající FX, zlatem, kryptem nebo cizím trhem („Indian shares
   …“). Názor a otázka shodí téma na 1 a událost na 2; měsíc „May“ názor
   není, sázka trhu na akci Fedu („Traders bet Fed cuts“) akce není.
5. **Strop podle feedu** (ne podle zdroje — Yahoo i CNBC jsou `rss_news`):
   sociální sítě bez kurátora 1, agregátory (Yahoo — od 8. 10. 2026 headline
   feedy ^GSPC/^IXIC, dřív rssindex vč. syndikace WSJ/Barron's/IBD, #1451 —,
   MarketWatch, uživatelské RSS) **jen události**
   (3 → 2, 2 → 1; rozhodnutí uživatele 26. 9. 2026).
6. **Kalendář FF**: importance = FF impact podle měny — USD High 3, Medium 2;
   rozhodnutí ECB/BoE/BoJ s High 2; ostatní 1. Regex nad titulkem kalendáře
   se nepoužívá (dával „USD PPI m/m“ High importance 1).
7. **fed_rss**: FED/3 pro statement, projekce a minutes FOMC, jinak FED/2.

Kategorie se bere z předmětu titulku, pak z celého titulku; ENERGY má
přednost před GEOPOLITICS (pohyb ropy způsobený válkou je ENERGY).
Směr (`classify_direction`) se proti verzi 1 nemění.

**Údržba:** slovník současného režimu (aktéři `ACTORS`, předseda Fedu
v `FED_TOPIC`) stárne. Po změně režimu (nová válka, nová cla, nový předseda
Fedu) dostanou nové zprávy nejvýš 2, dokud se konstanta neupraví. Změnu
ověř golden testem `test_classifier.py`.
"""

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass

# ── Čištění titulku ────────────────────────────────────────────────

_TAG = re.compile(r"<[^>]+>")
#: Odkazy a domény — `icc-cpi.int` jinak dávalo zprávě CPI a importance 3
_URL = re.compile(
    r"https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|org|net|int|gov|io|rs|ly)(?:/\S*)?", re.I
)
#: Reddit RSS nese autora („submitted by /u/Existing_Inflation17“)
_REDDIT = re.compile(r"submitted by\s+/u/\S+|\[link\]|\[comments\]|/u/\S+|/r/\S+", re.I)
_WIRE_SUFFIX = re.compile(r"\s+-\s+(?:reuters(?:\.com)?|bloomberg|cnbc|wsj)\s*$", re.I)
#: Typografické apostrofy (’ ‘ ʼ) → ' — jinak je minou vzory „here's what“ (NOISE)
#: a „fed's“ (FED_SPEAKER); Yahoo a Benzinga je píší běžně
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


def clean(text: str | None) -> str:
    """HTML entity a značky, apostrofy, odkazy, autor Redditu a přípona agentury pryč."""
    text = html.unescape(text or "").translate(_APOSTROPHES)
    text = _TAG.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _REDDIT.sub(" ", text)
    text = " ".join(text.split())
    return _WIRE_SUFFIX.sub("", text)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I | re.X)


# ── Feed a jeho strop ──────────────────────────────────────────────

FEED_BLUESKY = "bluesky"
FEED_BLUESKY_CURATED = "bluesky:curated"
FEED_REDDIT = "reddit"
FEED_YAHOO = "rss:yahoo"
FEED_MARKETWATCH = "rss:marketwatch"
FEED_CNBC = "rss:cnbc"
FEED_RSS_OTHER = "rss:other"
FEED_RSS_USER = "rss_user"
FEED_FED_RSS = "fed_rss"

#: Sociální sítě bez kurátora (#578): importance ≥ 2 → relevantní 0/29 (Reddit),
#: importance 3 → zásadní 0/42 (Bluesky) — nejvýš 1
FEED_CAP: dict[str, int] = {FEED_BLUESKY: 1, FEED_REDDIT: 1}
#: Agregátory: importance 3 → zásadní 0/18. Jen události — souhrny a výhledy
#: trhu nejsou významné (rozhodnutí uživatele 26. 9. 2026): 3 → 2, 2 → 1
AGGREGATOR_FEEDS = frozenset({FEED_YAHOO, FEED_MARKETWATCH, FEED_RSS_USER})


def _rss_feed(url: str) -> str:
    """Feed `rss_news` podle URL feedu (`raw.feed`), záložně podle odkazu článku.

    Yahoo syndikuje cizí články (`rssindex` WSJ, Barron's a IBD; headline feedy
    indexů od #1451 Fool, TheStreet, Benzinga) — doména odkazu by je zařadila
    jinam, proto rozhoduje URL feedu.
    """
    lowered = url.lower()
    if "yahoo.com" in lowered:
        return FEED_YAHOO
    if "marketwatch" in lowered or "mw_topstories" in lowered:
        return FEED_MARKETWATCH
    if "cnbc.com" in lowered:
        return FEED_CNBC
    return FEED_RSS_OTHER


def feed_of(source: str, raw: Mapping[str, object] | None) -> str:
    """Feed zprávy ze zdroje a surového payloadu — klíč stropu importance.

    Rozlišuje feedy uvnitř zdroje: kurátor na Bluesky (`raw.curated`),
    Yahoo / MarketWatch / CNBC uvnitř `rss_news` (`raw.feed`). Ostatní zdroje
    (alpaca, finnhub, ibkr_*, forexfactory, fed_rss) jsou feed samy o sobě.
    """
    payload = raw or {}
    if source == "bluesky":
        return FEED_BLUESKY_CURATED if payload.get("curated") is True else FEED_BLUESKY
    if source.startswith("reddit"):
        return FEED_REDDIT
    if source == "rss_news":
        return _rss_feed(str(payload.get("feed") or payload.get("link") or ""))
    return source


# ── Vylučovací kontexty → 1 ────────────────────────────────────────

NOISE = _rx(r"""
  \b(?:retire(?:ment|es|s|d)?|social\ security|401\(k\)|ira|annuit(?:y|ies)|savings\ account|
     credit\ card|pet\ insurance|vet\ bill|my\ (?:wife|husband|dad|mom|mother|father|parents)|
     inheritance|net\ worth|paycheck|payroll\ tax|personal\ finance|financial\ advisor)\b
 | \bstocks?\ to\ (?:buy|sell|watch|own)\b | \bstock\ of\ the\ day\b
 | \bbuy\ (?:point|signal|zone)\b
 | \bwhat'?s\ going\ on\ with\b | \bwhy\ (?:is|are)\b.{1,60}\b(?:stock|shares|etf)\b
 | (?<!s&p\ )(?<![$\d.,])\b(?:\d+|two|three|four|five|these\ \d+)
   \ (?:(?!(?:as|amid|after|while|on|points?)\b)\w+\ ){0,2}stocks\b
 | \bredbook\b | \b(?:public|secondary|underwritten)\ offering\b
 | \banalyst\ favorites\b | \bprice\ target\b
 | \bearnings\ call\b | \btranscript\b | \breported\ earnings\ today\b
 | \bwill\ (?:the\ )?s&p\ 500\ open\ (?:up|down)\b
 | \b(?:nfl|nba|mlb|nhl|ufc|super\ bowl|playoffs?|quarterback|world\ cup|premier\ league|
      olympics?|clippers|lakers)\b
 | \b(?:i\ can\ almost\ guarantee|you\ just\ missed|here\ are\ the|here'?s\ why|here'?s\ what)\b
 | \b(?:best-performing|top\ college|colleges?|graduates|students|rooftop)\b
 | ^(?:podcast|commentary)\b
 | \b(?:morning\ bid|trading\ day|breakingviews|chart\ of\ the\ day|explainer|factbox|
      what\ to\ watch|things\ to\ watch|week\ ahead|poll|price\ forecast)\b
 | ^markets\ news\b
""")
#: Zastaralé opakování („Reported Earlier, …“, ICYMI) — nejvýš 2
STALE = _rx(r"^reported\ earlier\b|\bicymi\b")
#: Výhled nebo podmínka („… Decision Preview“, „what to expect“, „if Fed hikes“)
#: — o akci se teprve spekuluje, nejvýš 2
PREVIEW = _rx(r"\bpreview\b|\bwhat\ to\ expect\b|\bif\s+(?:the\s+)?(?:fed|fomc|federal\ reserve)\b")
#: Sankce jsou většinou legislativní mezikroky a rutina (relevantní 14 %) → 1
SANCTIONS = _rx(r"\bsanction(?:s|ed)?\b")

FOREIGN = _rx(r"""
  (?<!\w)(?:canada|canadian|u\.k\.|uk|britain|british|england|german(?:y)?|euro\ ?zone|
     euro\ area|france|french|ital(?:y|ian)|spain|spanish|japan(?:ese)?|china|chinese|india(?:n)?|
     australia(?:n)?|new\ zealand|turkey|turkish|russia(?:n)?|brazil(?:ian)?|mexic(?:o|an)|swiss|
     switzerland|korea(?:n)?|sweden|norway|oecd|
     bundesbank|rbi|rba|rbnz|boc|bank\ of\ canada|snb|pboc|macklem)(?!\w)
""")
#: Menší cizí centrální banky — pro ES/NQ šum (pravidlo 2 konsenzu #1293)
MINOR_FOREIGN_CB = _rx(
    r"\b(?:bundesbank|rbi|rba|rbnz|boc|bank\ of\ canada|snb|pboc|macklem|"
    r"reserve\ bank\ of\ (?:india|australia|new\ zealand))\b"
)
#: ECB, BoJ, BoE — jejich rozhodnutí má relevanci 2
MAJOR_FOREIGN_CB = _rx(
    r"\b(?:ecb|boj|boe|bank\ of\ japan|bank\ of\ england|european\ central\ bank|lagarde|ueda|"
    r"japan\ interest\ rate)\b"
)
CB_ACTION = _rx(
    r"\b(?:rate\ decision|raise[sd]?|hike[sd]?|cut[s]?|hold[s]?|keeps?|leaves?|lift(?:s|ed)?)\b"
    r".{0,20}\b(?:rates?|\d+(?:\.\d+)?%|interest)|\bholds\ rates\b|\binterest\ rate\ decision\b"
)
#: Výhled cizí centrální banky („ECB to hike“, „set to“, ankety) — ne rozhodnutí
CB_PREVIEW = _rx(
    r"\b(?:to\ (?:raise|hike|cut|hold|lift)|set\ to|expected\ to|likely\ to|"
    r"seen\ (?:raising|hiking|cutting|holding))\b"
)
#: Titulek začíná aktivem mimo ES/NQ (FX, zlato, krypto) — rutinní souhrn
ASSET_START = _rx(
    r"^(?:the\ )?(?:u\.s\.\ )?(?:dollar|gold|silver|bitcoin|ethereum|crypto\w*|dogecoin|yen|euro|"
    r"sterling|pound|rupee|yuan)\b"
)
#: Spojka mezi předmětem a důvodem: „<předmět> as/after/ahead of <důvod>“
CONNECTOR = re.compile(
    r"\s(?:as|amid|after|ahead of|before|despite|while|following|even as)\s|\s[—–]\s", re.I
)
#: Předmět titulku je americký trh (souhrn trhu smí nést driver ze zmínky jako 2)
US_MARKET_HEAD = _rx(
    r"\b(?:stocks?|futures|s&p(?:\ 500)?|dow(?:\ jones)?|nasdaq|wall\ street|treasur\w*|yields?|"
    r"bonds?|equities|indices|indexes|markets?|mega-caps?|small-caps?)\b"
)
FOREIGN_MARKET = _rx(
    r"\b(?:indian|europe'?s|european|stoxx|ftse|dax|nikkei|asian|asia|"
    r"gulf\ (?:bourses|equities|stocks)|bourses|chinese|japanese|german|british|canadian|tsx|kospi|"
    r"hang\ seng)\b"
)
#: Titulek je souhrn cizího trhu („Indian shares advance on oil retreat“, „Most Gulf
#: bourses in red on Houthi attack“). Driver za „on“ spojka neoddělí, proto → 1
#: bez ohledu na spojku — se spojkou dává totéž pravidlo předmětu
FOREIGN_MARKET_START = _rx(r"""
  ^(?:(?:most|some|the|major|key)\ )?
  (?:(?:indian|india'?s|european|europe'?s|euro\ ?zone|asian|asia'?s|gulf|mideast|chinese|china'?s?|
       japanese|japan'?s?|german|germany'?s|british|uk|u\.k\.|canadian|canada'?s|hong\ kong|korean|
       australian|emerging(?:-market)?|brazilian|mexican|russian|turkish|saudi|israeli)
     \ (?:\w+\ )?(?:shares|stocks|bourses|equities|markets|indices|indexes|index|benchmarks?)
   | (?:\w+'s\ )?(?:stoxx|ftse|dax|cac|nikkei|kospi|hang\ seng|tsx|sensex|nifty|asx|topix))\b
""")
#: Modální „may“, ne měsíc květen („For May“, „in May“, „since May“, „May 1“,
#: „mid-May“, „May's“, „May CPI“) — měsíc by shodil release z května na 2
_MODAL_MAY = (
    r"(?<!\bin\ )(?<!\bfor\ )(?<!since\ )(?<!\bfrom\ )(?<!\bof\ )(?<!through\ )(?<!until\ )"
    r"(?<!\btill\ )(?<!\bby\ )(?<!early\ )(?<!\blate\ )(?<!\blast\ )(?<!\bnext\ )(?<!-)"
    r"\bmay\b(?!\s*\d)(?!'s\b)(?!-)"
    r"(?!\s+(?:data|figures|report|reading|meeting|cpi|ppi|pce|payrolls|jobs|sales|inflation|"
    r"highs?|lows?|levels?|minutes)\b)"
)
#: Spekulace a názor („says“ samo o sobě ne — agenturní titulky ho mají i u faktů)
OPINION = _rx(
    r"\b(?:hopes?|urges?|calls?\ (?:for|on)|wants?|could|would|might|should|warns?|warned|"
    r"thinks?|believes?|predicts?|argues?|vows?|vowed|opinion|analysis|commentary|scenarios?)\b"
    rf"|{_MODAL_MAY}"
)
REVISION = _rx(r"\brevis(?:ed|es)\b")
SETTLES = _rx(r"\bsettle")
GDP = _rx(r"\bgdp\b")
TARIFF_REFUND = _rx(r"refund")

# ── Fed ────────────────────────────────────────────────────────────

_FED_WORD = r"(?:\bfed\b|\bfed's\b|\bfomc\b|federal\ reserve|federal\ open\ market\ committee)"
#: Akce Fedu: rozhodnutí o sazbách bez modálních sloves, statement, minutes,
#: projekce, předseda Fedu mluví
FED_EVENT = _rx(rf"""
   \bfomc\b\s*(?:statement|decision|minutes|projections|:)
 | \bfomc\s+(?:\w+\s+)?(?:hikes?|raises?|cuts?|lowers?|holds?|keeps?|leaves?|lifts?)\b
 | {_FED_WORD}\s+(?:(?!should|could|would|may|might|must|to|will|wants?|urged?|
                                (?:for|on|of|in|about|over|with|and)\b)\w+\s+)?
     (?:hikes?|hiked|raises?|raised|cuts?|lowers|lowered|holds?|held|keeps?|kept|leaves?|left|
        lifts?|lifted)\s+
     (?:the\s+)?(?:(?:federal\s+funds|benchmark|policy|key|interest)\s+)?
     (?:rates?|by\ \d+|\d+\ ?(?:bps|basis\ points?))
 | {_FED_WORD}.{{0,15}}\binterest\ rate\ decision\b
 | \bminutes\ of\ the\ federal\ open\ market\ committee\b | \bfomc\ minutes\b
 | \bfomc\ statement\b | \beconomic\ projections\b.{{0,60}}\bfomc\b
 | (?<!former\ )(?<!\bex-)\bfed\ chair(?:man)?
   \s+(?!(?:nominee|candidate|pick|race|hopeful|contender|frontrunner)\b)\w+(?:\s+\w+)?
   \s+(?:says|said)\b
""")
#: Sázka trhu na akci Fedu („Traders bet Fed cuts rates“, „markets price in Fed
#: hike“) — Fed tu není podmětem, akce se nestala; bezprostředně před shodou FED_EVENT
FED_BET_BEFORE = _rx(
    r"\b(?:bets?|betting|wagers?|see|sees|expects?|expecting|price[sd]?\ in|pricing\ in)"
    r"\s+(?:(?:that|on|the|a)\s+)*$"
)
#: Regionální průzkumy Fedu jsou data (MACRO_GROWTH), ne měnová politika
REGIONAL_FED_DATA = _rx(
    r"\b(?:philly|philadelphia|richmond|dallas|kansas\ city|new\ york|empire|chicago)\ fed\b.{0,30}"
    r"\b(?:manufacturing|index|survey|business|services|activity)\b|\bempire\ state\b"
)
#: Téma Fedu. Předseda jménem (`warsh`, `jerome powell`) — slovník režimu, při
#: změně předsedy upravit; holé „powell“ chytalo Colina Powella
FED_TOPIC = _rx(
    rf"{_FED_WORD}|\bfed\ chair(?:man)?\b|\bfederal\ funds\b|\bbeige\ book\b|\bwarsh\b"
    r"|\bjerome\ powell\b|\bjackson\ hole\b"
)
US_RATE_TOPIC = _rx(r"\brate[- ](?:hike|cut|decision)s?\b|\binterest[- ]rate\ decision\b")
#: Člen Fedu mluví („Fed's Waller Says…“) — relevance 2 i se „should/could“ v citaci
FED_SPEAKER = _rx(
    r"\bfed's\s+\w+(?:\s+\w+)?\s+(?:says|said|sees|expects|warns|calls|urges|backs|supports|signals|"
    r"tells|adds)\b|\bfed\ (?:governor|president|vice\ chair)\s+\w+(?:\s+\w+)?\s+(?:says|said)\b"
)

# ── US data ────────────────────────────────────────────────────────

#: CPI, ale ne firma „CPI Card Group“ (emise akcií dávala MACRO_INFLATION/3)
_CPI = r"\b(?:core\ )?cpi\b(?!\ card\b)"
#: Payrolls jako data; jednotné „payroll“ jen s datovým slovem — „payroll software
#: company“, „payroll provider“ jsou firmy
_PAYROLLS = r"\bpayrolls\b|\bpayroll\ (?:growth|gains?|report|data|numbers|figures|jobs|print)\b"
#: Mzdová data z reportu o zaměstnanosti — práce, ne výsledky firem (EARNINGS)
_WAGES = r"average\ hourly\ earnings|employment\ cost\ index"

TIER1 = _rx(rf"""
  {_CPI} | consumer\ price(?:s|\ index) | \b(?:core\ )?pce\b
 | personal\ (?:income|spending)
 | non-?farm | {_PAYROLLS} | unemployment\ rate | jobs?\ growth | job\ gains | employers\ added
 | retail\ sales | retail\ control | control\ group
 | \bism\b | \bpmi\b | \b(?:core\ )?ppi\b | producer\ price(?:s|\ index)?
 | \bgdp\b.{{0,40}}\b(?:q[1-4]|quarter|annuali[sz]ed|advance|second\ estimate|third\ estimate)
 | \b(?:usa|u\.s\.|us)\ (?:q[1-4]\ )?gdp\b
""")
TIER2 = _rx(r"""
  jobless\ claims|initial\ claims|continuing\ claims|industrial\ production|
  manufacturing\ production|import\ price|export\ price|\badp\b|durable\ goods|\bdurables\b|
  \bjolts\b|job\ openings|housing\ starts|building\ permits|new\ home\ sales|
  existing\ home\ sales|pending\ home\ sales|
  consumer\ (?:confidence|sentiment|expectations)|\bmichigan\b|\bnahb\b|housing\ market\ index|
  leading\ index|participation\ rate|trade\ balance|factory\ orders|philly\ fed|empire\ state|
  (?:richmond|dallas|kansas\ city|chicago)\ (?:fed\ )?manufacturing|employment\ change|productivity|
  unit\ labor\ costs|wholesale\ inventories|business\ inventories
""")
#: Řádek releasu ve formátu Benzinga („USA … For August 1.4% Vs 0.4% Est.“) — US řada ≥ 2
RELEASE_LINE = _rx(
    r"^(?:usa|u\.s\.)\s.{0,80}\bvs\.?\s"
    r"|\bfor\s+(?:january|february|march|april|may|june|july|august|september|october|november|"
    r"december|q[1-4])\b.{0,40}\bvs\.?\s"
)
#: Tvar releasu: číslo s jednotkou, srovnání s konsenzem nebo sloveso výsledku
RELEASE_FORM = _rx(r"""
  \d+(?:\.\d+)?\s?% | \b\d+(?:\.\d+)?\s?[km]\b | \b\d{2,3},\d{3}\b | \bvs\.?\b
 | \best(?:imate)?\.?\b | \bexpected\b | \bforecast\b | \bconsensus\b | \bprior\b
 | \b(?:rose|rises|fell|falls|jumped|jumps|surged|surges|slowed|slows|accelerated|accelerates|
   cooled|cools|unexpectedly|beat|beats|miss(?:es|ed)?|robust|weak(?:er)?|strong(?:er)?|hotter|
   cooler|unchanged|revised?|climbs?|climbed|drops?|dropped|declines?|declined|increases?|
   increased|decreases?|decreased|shocks?|surprises?|blowout|stronger-than-expected|
   weaker-than-expected|hotter-than-expected|cooler-than-expected)\b
""")
DATA_MENTION = _rx(
    rf"{_CPI}|\bpce\b|\bppi\b|non-?farm|\bpayrolls\b|jobs\ report|retail\ sales|\bpmi\b|\bism\b|"
    r"jobless\ claims|inflation\ data"
)

# ── Energie a geopolitika ──────────────────────────────────────────

#: Pohyb ceny ropy. Ne jedlé oleje (palm, olive…), ne „oil-driven“ a ne ropné
#: firmy a zpracování („oil stocks jump“, „oil major Shell hits“, „oil throughput rises“)
OIL_MOVE = _rx(r"""
  (?<!palm\ )(?<!olive\ )(?<!cooking\ )(?<!soybean\ )(?<!vegetable\ )(?<!fish\ )
  \b(?:oil|crude|brent|wti)\b
  (?!-driven|\s+(?:throughput|stocks?|shares|equities|producers?|majors?|companies|company|firms?|
     drillers?|explorers?|refiners?|executives?|giants?|patch|services)\b)
  (?:\ oil)?(?:\ prices?|\ futures)?\b.{0,20}
  \b(?:surges?|surged|jumps?|jumped|soars?|soared|spikes?|spiked|tumbles?|tumbled|plunges?|
     plunged|slumps?|slumped|sinks?|sank|crash(?:es|ed)?|falls?|fell|drops?|dropped|slides?|slid|rises?|rose|gains?|gained|
     climbs?|climbed|rall(?:y|ies|ied)|skyrockets?|retreats?|retreated|tops?|topped|settles?|settled|nears?|
     approach(?:es)?|hovers?|hits?|breaks?|crosses|extends?)\b
""")
OIL_SUPPLY = _rx(
    r"\b(?:oil|crude|lng|opec\+?|aramco|tankers?|pipeline|barrels?)\b.{0,40}"
    r"\b(?:exports?|imports?|supply|supplies|output|production|shipments?|flows?|halts?|halted|cuts?|"
    r"outage|disruption|inventor(?:y|ies)|stockpiles?|a\ day)\b"
)
ENERGY_TOPIC = _rx(
    r"\b(?:oil|crude|brent|wti|opec\+?|natural\ gas|lng|gasoline|gas\ prices|refiner(?:y|ies|s)?|"
    r"tankers?|aramco|pipeline)\b"
)
SUPPLY_TOPIC = _rx(r"\b(?:hormuz|houthis?|aramco)\b")
#: Aktéři současného režimu (válka s Íránem, Blízký východ, Čína/Tchaj-wan) —
#: JEDINÉ místo se slovníkem režimu, při jeho změně upravit. Rusko/Ukrajina
#: (válka od 2022) jen jako téma „war“ (2).
ACTORS = _rx(
    r"\b(?:iran(?:ian)?|tehran|israel(?:i)?|saudis?|saudi\ arabia|riyadh|houthis?|yemen(?:i)?|"
    r"china|chinese|beijing|taiwan|hormuz|opec|middle\ east|gulf)\b"
)
#: Uzavření / znovuotevření je eskalace jen u průlivu, hranice, přístavu a podobně —
#: „Chinese stocks close higher“ ani „Gulf markets reopen“ eskalace nejsou
_CHOKEPOINT = (
    r"(?:strait|hormuz|airspace|borders?|embass(?:y|ies)|shipping|pipelines?|ports?|waterways?|canal|"
    r"oil\ fields?|refiner(?:y|ies)|terminals?|facilit(?:y|ies))"
)
_CLOSE = r"(?:clos(?:e|es|ed|ing|ure)|reopen(?:s|ed|ing)?|shut(?:s|ting)?|shutdown)"
ESCALATION = _rx(rf"""
  \b(?:attacks?|attacked|strikes?|struck|missiles?|drones?|bomb(?:s|ing|ed)?|intercept(?:s|ed)?|
     seiz(?:e|es|ed)|halt(?:s|ed)?|blockade|invad(?:e|es|ed)|invasion|
     escalat\w*|retaliat\w*|ceasefire|cease-fire|truce|peace\ (?:deal|talks|plan|proposal)|
     resum\w*\ (?:bombing|strikes)|declares?\ war|end\W{{0,3}}(?:of|to)\W+(?:the\W+)?(?:\w+\W+)?war|
     end\ (?:the\ )?fighting|nuclear\ (?:test|strike|deal))\b
 | \b{_CLOSE}\W+(?:\w+\W+){{0,5}}{_CHOKEPOINT}\b
 | \b{_CHOKEPOINT}\W+(?:\w+\W+){{0,5}}{_CLOSE}\b
 | \bclose\ in\ on\b
 | \bwar\b.{{0,15}}\bend(?:s|ed|ing)?\b
""")
#: Místní konflikty mimo režim (Gaza, Západní břeh, Libanon) — Izrael tu není eskalace s Íránem
LOCAL_CONFLICT = _rx(
    r"\b(?:gaza|west\ bank|lebanon|lebanese|hezbollah|settlers?|syria|syrian|medics\ say)\b"
)
#: Deeskalace slovy diplomacie (aktér + jednání) — 2
GEO_DIPLO = _rx(r"\b(?:negotiat\w*|diplomac\w*|peace\ talks|talks\ with)\b")
#: Obchodní dohoda (bez ohledu na aktéra) — 2
TRADE_DEAL = _rx(r"\btrade\s+(?:deal|agreement|truce|talks|framework|pact)\b")
#: Fiskální riziko USA: shutdown, dluhový strop (2), snížení ratingu USA (3)
FISCAL = _rx(r"\bgovernment\s+shutdown\b|\bdebt\s+(?:ceiling|limit)\b")
US_DOWNGRADE = _rx(
    r"\bdowngrades?\s+(?:the\s+)?(?:us|u\.s\.|united\s+states)\b.{0,25}\b(?:credit|rating|sovereign)\b"
    r"|\b(?:us|u\.s\.)\s+(?:credit|sovereign)\s+rating\s+(?:cut|downgrade)"
)
#: „war“ v jiném významu — tug-of-war, trade war, war crimes court, válečné pravomoci…
WAR_NOISE = _rx(
    r"tug-of-war|trade\ war|war\ crimes?|war\ powers|price\ war|culture\ war|war\ chest|war\ room|"
    r"star\ wars|console\ war|bidding\ war|talent\ war|war\ bill|war\ boom"
)
WAR_TOPIC = _rx(r"\bwar\b")
#: Silná válečná slova — nová válka mimo aktéry režimu (invaze, vyhlášení války)
#: nesmí propadnout na 1
WAR_STRONG = _rx(
    r"\binvasion\b|\binvad(?:e|es|ed|ing)\b|\bdeclares?\ war\b|\bair\ ?strikes?\b|\bceasefire\b|"
    r"\bcease-fire\b|\bnuclear\ (?:strike|attack|test)\b|\bmartial\ law\b|\bcoup\b"
)
TARIFF_EVENT = _rx(r"""
  \b(?:trump|white\ house|u\.?s\.?|us|administration|commerce|ustr|china|beijing|eu|
     european\ union|canada|mexico)\b.{0,40}
  \b(?:impos\w+|announc\w+|slap\w*|rais\w+|hik\w+|threat\w+|delay\w*|paus\w+|lift\w*|suspend\w*|
     extend\w*|exempt\w*|retaliat\w+|doubl\w+)\b
  .{0,30}\btariffs?\b
 | \btariffs?\b.{0,15}\b(?:on|of)\b.{0,20}\d+\s?%
""")
TARIFF_TOPIC = _rx(r"\btariffs?\b")

# ── Ostatní střední témata ────────────────────────────────────────

YIELDS = _rx(
    r"\b(?:treasury|bond|benchmark|(?:2|5|10|30)-year|ten-year|two-year|thirty-year)\b"
    r".{0,20}\byields?\b"
)
YIELDS_NOISE = _rx(r"\b(?:etfs?|dividends?|payouts?|covered\ calls?|high-yield)\b")
INFLATION = _rx(r"\binflation\b")
#: Inflace jako předmět (data, report) — holé „inflation“ je v každém textu o Fedu
INFLATION_SUBJECT = _rx(
    rf"{_CPI}|\b(?:core\ )?pce\b|\b(?:core\ )?ppi\b|consumer\ prices|producer\ prices|"
    r"price\ index|inflation\ (?:data|report|rate|reading|figures|print|numbers|expectations|"
    r"rose|rises|fell|falls|accelerat\w*|cool\w*|slow\w*|eas\w*|jump\w*|surge\w*|hotter|cooler)"
)
MARKET_HEAD = _rx(
    r"^(?:.{0,40})\b(?:dollar|gold|silver|yields?|stocks?|futures|treasur\w*|bonds?|shares|"
    r"indices|s&p|nasdaq|dow|markets?|investors|equities|yen|euro|sterling)\b"
)
LABOR = _rx(
    rf"non-?farm|{_PAYROLLS}|{_WAGES}|unemployment|jobless|jobs\ report|job\ openings|\bjolts\b|"
    r"\badp\b|labor\ market|employment\ change|\bjobs\b|hiring|layoffs"
)
LABOR_IMP = _rx(
    rf"non-?farm|{_PAYROLLS}|{_WAGES}|unemployment|jobless|jobs\ report|jobs?\ growth|"
    r"job\ openings|\bjolts\b|\badp\b|labor\ market"
)
GROWTH = _rx(
    r"\bgdp\b|retail\ sales|\bpmi\b|\bism\b|durable\ goods|industrial\ production|"
    r"consumer\ (?:confidence|sentiment|spending)|housing\ starts|home\ sales|factory\ orders|"
    r"philly\ fed|empire\ state|\brecession\b"
)
#: Výsledky firem; „average hourly earnings“ jsou mzdová data (LABOR)
EARNINGS = _rx(
    r"(?<!hourly\ )\bearnings\b|quarterly\ results|\bguidance\b|profit\ beat|revenue\ miss|"
    r"\bq[1-4]\ (?:results|revenue|profit|sales)"
)
CRYPTO = _rx(r"bitcoin|ethereum|crypto|dogecoin")
TECH = _rx(r"nvidia|semiconductor|\bai\b|chipmaker|\bapple\b|microsoft|\bmeta\b|\bchips?\b")

# ── Kategorie ──────────────────────────────────────────────────────

DEFAULT_CATEGORY = "OTHER"


def split_head(title: str) -> str:
    """Předmět titulku = text před první spojkou („as/after/ahead of …“)."""
    match = CONNECTOR.search(title)
    return title[: match.start()] if match and match.start() > 8 else title


def _is_geopolitics(text: str) -> bool:
    war = bool(WAR_TOPIC.search(text) and not WAR_NOISE.search(text)) or bool(
        WAR_STRONG.search(text)
    )
    return (
        war
        or bool(TRADE_DEAL.search(text))
        or bool(SUPPLY_TOPIC.search(text))
        or bool(TARIFF_TOPIC.search(text))
        or bool(SANCTIONS.search(text))
        or bool(LOCAL_CONFLICT.search(text))
        or bool(ACTORS.search(text) and ESCALATION.search(text))
    )


def _category(text: str) -> str:
    """Kategorie textu; pořadí rozhoduje (první shoda vyhrává)."""
    if not text:
        return DEFAULT_CATEGORY
    if REGIONAL_FED_DATA.search(text):
        return "MACRO_GROWTH"
    if ASSET_START.search(text):
        return "CRYPTO" if CRYPTO.search(text) else DEFAULT_CATEGORY
    foreign_rate = FOREIGN.search(text) or MAJOR_FOREIGN_CB.search(text)
    if FED_TOPIC.search(text) or (US_RATE_TOPIC.search(text) and not foreign_rate):
        return "FED"
    # ENERGY před GEOPOLITICS: pohyb ropy způsobený válkou je ENERGY (pravidlo 7 konsenzu)
    if ENERGY_TOPIC.search(text):
        return "ENERGY"
    if INFLATION_SUBJECT.search(text):
        return "MACRO_INFLATION"
    if LABOR.search(text) and not NOISE.search(text):
        return "MACRO_LABOR"
    if GROWTH.search(text):
        return "MACRO_GROWTH"
    if INFLATION.search(text) and not MARKET_HEAD.search(text):
        return "MACRO_INFLATION"
    if _is_geopolitics(text):
        return "GEOPOLITICS"
    if CRYPTO.search(text):
        return "CRYPTO"
    if EARNINGS.search(text) and not NOISE.search(text):
        return "EARNINGS"
    if TECH.search(text):
        return "TECH"
    return DEFAULT_CATEGORY


def classify_category(title: str) -> str:
    """Kategorie z předmětu titulku, jinak z celého titulku (shrnutí se nečte)."""
    text = clean(title)
    category = _category(split_head(text))
    return category if category != DEFAULT_CATEGORY else _category(text)


# ── Importance titulku ─────────────────────────────────────────────

#: Spouštěče UDÁLOSTI — jen ty smí dát 3; názor a otázka je shodí na 2
EVENT_KINDS = frozenset(
    {"fed_event", "tier1_release", "oil_move", "geo_event", "tariff_event", "us_downgrade"}
)
#: Spouštěče odolné vůči názorovým slovům (citace Fedu, release s „may“ v měsíci)
OPINION_IMMUNE_KINDS = frozenset({"fed_event", "tier1_release", "tier2_release", "fed_speaker"})
QUESTION_IMMUNE_KINDS = frozenset({"fed_event", "tier1_release"})
#: Otázka začínající pomocným slovesem je výhled, ne zpráva o akci („Will the
#: Fed hike rates next week?“) — imunitu akce Fedu a releasu ruší
QUESTION_START = _rx(
    r"^(?:will|could|should|can|would|might|may|is|are|does|do|did|has|have|what|why|how|when)\b"
)


def _fed_event(text: str) -> bool:
    """Akce Fedu s Fedem jako podmětem — ne sázka trhu na ni („Traders bet Fed cuts…“)."""
    return any(
        not FED_BET_BEFORE.search(text[: match.start()]) for match in FED_EVENT.finditer(text)
    )


def _trigger_level(text: str) -> tuple[int, str]:
    """Nejvyšší spouštěč v textu (bez stropů feedu, názoru a zmínky)."""
    foreign = bool(FOREIGN.search(text))
    major_cb = bool(MAJOR_FOREIGN_CB.search(text))
    level, reason = 1, "nic"

    def up(value: int, why: str) -> None:
        nonlocal level, reason
        if value > level:
            level, reason = value, why

    # Fed
    if _fed_event(text):
        up(3, "fed_event")
    elif FED_SPEAKER.search(text):
        up(2, "fed_speaker")
    elif FED_TOPIC.search(text) and not REGIONAL_FED_DATA.search(text):
        up(2, "fed_topic")
    elif US_RATE_TOPIC.search(text) and not foreign and not major_cb:
        up(2, "us_rate_topic")
    if (
        major_cb
        and (CB_ACTION.search(text) or US_RATE_TOPIC.search(text))
        and not CB_PREVIEW.search(text)
    ):
        up(2, "major_cb")
    # US data (cizí data jsou pro ES/NQ šum)
    revision = bool(REVISION.search(text))
    if TIER1.search(text) and RELEASE_FORM.search(text) and not foreign:
        up(2 if (OPINION.search(text) or revision) else 3, "tier1_release")
    elif (
        (TIER2.search(text) and RELEASE_FORM.search(text))
        or REGIONAL_FED_DATA.search(text)
        or RELEASE_LINE.search(text)
    ) and not foreign:
        up(1 if revision else 2, "tier2_release")
    elif DATA_MENTION.search(text) and not foreign:
        up(2, "data_mention")
    # Energie a geopolitika
    if OIL_MOVE.search(text):
        up(2 if SETTLES.search(text) else 3, "oil_move")
    elif OIL_SUPPLY.search(text):
        up(2, "oil_supply")
    if ACTORS.search(text) and not LOCAL_CONFLICT.search(text) and not WAR_NOISE.search(text):
        if ESCALATION.search(text):
            up(3, "geo_event")
        elif GEO_DIPLO.search(text):
            up(2, "geo_diplo")
    # Holé „war“ je v současném režimu ve všem (válka s Íránem) — samo nic
    # nezvedá; téma až se silným slovem, událost s aktérem režimu
    if WAR_STRONG.search(text) and not LOCAL_CONFLICT.search(text):
        up(2, "war_strong")
    if US_DOWNGRADE.search(text):
        up(3, "us_downgrade")
    elif FISCAL.search(text):
        up(2, "fiscal")
    if TRADE_DEAL.search(text):
        up(2, "trade_deal")
    if TARIFF_EVENT.search(text):
        up(3, "tariff_event")
    elif TARIFF_TOPIC.search(text) and not TARIFF_REFUND.search(text):
        up(2, "tariff_topic")
    # Sazby, inflace, práce, růst, earnings
    if YIELDS.search(text) and not YIELDS_NOISE.search(text):
        up(2, "yields")
    if INFLATION_SUBJECT.search(text) and not foreign:
        up(2, "inflation_subject")
    if LABOR_IMP.search(text) and not foreign:
        up(2, "labor")
    # Holé „GDP“ nese názory (AI → GDP); release GDP pokrývá TIER1
    if GROWTH.search(text) and not foreign and not GDP.search(text):
        up(2, "growth")
    if EARNINGS.search(text):
        up(2, "earnings")
    return level, reason


def headline_importance(title: str) -> tuple[int, str]:
    """Importance zprávy jen z titulku (bez stropu feedu); vrací (importance, důvod)."""
    text = clean(title)
    if not text:
        return 1, "prázdný"
    if NOISE.search(text):
        return 1, "noise"
    if SANCTIONS.search(text) and not TARIFF_TOPIC.search(text):
        return 1, "sanctions"
    if MINOR_FOREIGN_CB.search(text):
        return 1, "minor_foreign_cb"
    if ASSET_START.search(text):
        return 1, "asset_start"
    if FOREIGN_MARKET_START.search(text):
        return 1, "foreign_market"
    level, reason = _trigger_level(text)
    if level == 1:
        return level, reason
    # Předmět vs. zmínka: spouštěč jen za spojkou platí do úrovně předmětu
    head = split_head(text)
    if head != text:
        head_level, head_reason = _trigger_level(head)
        if head_level < level:
            us_market = (
                bool(US_MARKET_HEAD.search(head))
                and not FOREIGN_MARKET.search(head)
                and not FOREIGN.search(head)
            )
            capped = max(head_level, 2) if us_market else head_level
            if capped < level:
                base = head_reason if capped == head_level and head_level > 1 else reason
                level, reason = capped, base + "+zmínka"
                if level == 1:
                    return level, reason
    kind = reason.split("+")[0]
    # Názor a otázka: téma → 1, událost → 2
    if kind not in OPINION_IMMUNE_KINDS and OPINION.search(text):
        level, reason = (2 if kind in EVENT_KINDS else 1), reason + "+názor"
    if "?" in text and (kind not in QUESTION_IMMUNE_KINDS or QUESTION_START.search(text)):
        target = 2 if kind in EVENT_KINDS else 1
        if level > target:
            level, reason = target, reason + "+otázka"
    if STALE.search(text) and level > 2:
        level, reason = 2, reason + "+zastaralé"
    if PREVIEW.search(text) and level > 2:
        level, reason = 2, reason + "+výhled"
    return level, reason


def feed_capped(importance: int, feed: str) -> int:
    """Strop podle feedu: sociální sítě bez kurátora 1, agregátory jen události."""
    if feed in AGGREGATOR_FEEDS:
        return max(1, importance - 1)
    cap = FEED_CAP.get(feed)
    return min(importance, cap) if cap is not None else importance


# ── Kalendář FF a Fed RSS ──────────────────────────────────────────

#: FF impact → importance u USD (a globálních „All“ událostí)
FF_IMPACT_IMPORTANCE = {"high": 3, "medium": 2}
FF_US_CURRENCIES = frozenset({"USD", "ALL"})
#: ECB, BoE, BoJ: významné jen rozhodnutí centrální banky s impactem High → 2
FF_MAJOR_CURRENCIES = frozenset({"EUR", "GBP", "JPY"})
FF_CB_DECISION = _rx(
    r"\b(?:main\ refinancing\ rate|deposit\ facility\ rate|official\ bank\ rate|policy\ rate|"
    r"monetary\ policy\ (?:statement|summary|report)|press\ conference|outlook\ report)\b"
    r"(?!\ hearings)"
)


def scheduled_importance(title: str, ff_impact: str | None) -> int:
    """Importance události kalendáře FF („USD CPI m/m“) z FF impactu podle měny.

    USD a „All“: High 3, Medium 2, jinak 1. EUR/GBP/JPY: rozhodnutí ECB, BoE
    a BoJ (sazba, statement, tisková konference) s High 2. Ostatní měny 1
    (rozhodnutí uživatele 26. 9. 2026: 63 z 96 „významných“ událostí za 28 dní
    byly CAD, NZD, AUD, GBP a další měny).
    """
    currency = title.split(" ", 1)[0].upper() if title else ""
    impact = FF_IMPACT_IMPORTANCE.get((ff_impact or "").strip().lower(), 1)
    if currency in FF_US_CURRENCIES:
        return impact
    if currency in FF_MAJOR_CURRENCIES and impact == 3 and FF_CB_DECISION.search(title):
        return 2
    return 1


def fed_rss_importance(title: str) -> int:
    """Fed RSS: statement, projekce a minutes FOMC 3, ostatní (projevy) 2."""
    return 3 if FED_EVENT.search(clean(title)) else 2


# ── Směr (beze změny proti verzi 1) ────────────────────────────────

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

# Síla pravidlového odhadu — vědomě nízká. Je to hrubý first pass, ne LLM;
# nadhodnocená strength by zkreslila skóre v SentIndexu (SPEC 5.3).
STRENGTH_ONE_SIDED = 0.4
STRENGTH_MIXED = 0.2


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


# ── Celek ──────────────────────────────────────────────────────────

SCHEDULED_KIND = "scheduled"


@dataclass(frozen=True)
class RuleClassification:
    """Výstup pravidlového průchodu — mapuje se na `news_classifications`.

    `reason` = spouštěč importance (diagnostika, reklasifikační dry-run); do DB
    se neukládá.
    """

    category: str
    importance: int
    direction: int
    strength: float
    reason: str = ""


def classify(
    title: str, *, feed: str, kind: str, ff_impact: str | None = None
) -> RuleClassification:
    """Kompletní pravidlový odhad z titulku.

    `feed` = `feed_of(source, raw)`, `kind` = `news_events.kind`, `ff_impact`
    = FF impact z `raw` (jen kalendář). Směr se čte z titulku; u kalendáře ho
    job přepíše konvencí řady (SPEC kap. 4).
    """
    direction, strength = classify_direction(title)
    category = classify_category(title)
    if kind == SCHEDULED_KIND:
        return RuleClassification(
            category,
            scheduled_importance(clean(title), ff_impact),
            direction,
            strength,
            "ff_impact",
        )
    if feed == FEED_FED_RSS:
        return RuleClassification("FED", fed_rss_importance(title), direction, strength, "fed_rss")
    importance, reason = headline_importance(title)
    capped = feed_capped(importance, feed)
    if capped != importance:
        reason = f"{reason}+strop:{feed}"
    return RuleClassification(category, capped, direction, strength, reason)
