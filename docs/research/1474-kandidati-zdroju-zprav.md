# Test kandidátů na zdroje breaking news (E-6.22)

Kandidáti na nové zdroje zpráv z rozhodnutí 8. 10. 2026 v #1385 (blok breaking news, Fáze 6 #1406).
Report je vstup pro ADR-0059 o tierech obsahu (E-6.23) a pro adaptéry E-6.25 a E-6.26. Navazuje na
audit stávajících zdrojů E-6.21 (`docs/research/1473-audit-zdroju-zprav.md`). Nic za paywallem,
přihlášením ani X. Registrace klíčů a účtů je mimo rozsah; chybějící klíč se uvádí jako podmínka.

## Jak se měřilo

**Sonda** `scripts/news_candidates_probe.py`, snímek `as_of` **2026-10-08T21:15:51Z** (čtvrtek
23:15 SELČ, po close US akcií; Globex v denní pauze). Na každého kandidáta jde jeden GET a jeden GET
`robots.txt` se stejnou hlavičkou `User-Agent`, jakou by posílal adaptér:
- prohlížečová `BROWSER_UA` jako dnešní fetcher (`gexlens_news/http.py`);
- u SEC a BLS identifikace s kontaktem z `GEXLENS_NEWS_SEC_CONTACT` (#1457). Bez ní obě vrací 403.
  Kontakt se převádí na ASCII, protože hlavička HTTP diakritiku nepřenese.

Nic se nezapisuje a klíče se nevypisují.

```bash
# proměnné se předají z .env bez výpisu hodnot
uv run python scripts/news_candidates_probe.py
```

Jde o **jednorázový snímek**. Stáří nejnovější položky a medián odstupu popisují, co feed držel
v okamžiku běhu, ne průměrnou frekvenci. Skutečné zpoždění proti publikaci změří až provoz (E-6.29).
Podmínky použití pochází z primárních stránek poskytovatelů (odkazy v oddílu 3), čtených 8. 10. 2026.

## 1. Verdikt: použít / nepoužít

Tier obsahu je **návrh** pro ADR-0059: 1 = oficiální zdroj, 2 = headline feed, 3 = článek / analýza.

| kandidát | verdikt | návrh tieru | proč |
|---|---|---|---|
| Fed — měnová politika a projevy | **už běží** (`fed_rss`) | 1 | beze změny |
| Fed — svědectví v Kongresu (`testimony.xml`) | **použít** | 1 | pololetní svědectví předsedy hýbe trhem, objem ~15 položek za rok, conditional GET |
| Fed — všechny tiskové zprávy (`press_all.xml`) | **nepoužít** | — | nad `press_monetary` přidává hlavně sankce, schválení akvizic bank a regulaci, tedy šum pro ES/NQ |
| BLS — feedy jednotlivých releasů (`/feed/{empsit,cpi,ppi,jolts,eci}.rss`) | **použít s výhradou** | 1 | titulek nese hlavní čísla (např. payrolls a nezaměstnanost). **`published` je ~40 min před releasem**, viz 2.2. Vyžaduje UA s kontaktem. `bls_latest.rss` drží jen 1 položku, nepoužít. |
| BEA — `www.bea.gov/news/rss` | **použít** | 1 | `pubDate` = čas releasu (8:30 ET), robots povoluje, conditional GET |
| BEA — `apps.bea.gov/rss/rss.xml` | **nepoužít** | — | **robots.txt zakazuje** (`apps.bea.gov` povoluje jen vyjmenované cesty) |
| Treasury — GovDelivery RSS | **nepoužít** | — | **robots.txt zakazuje** (`public.govdelivery.com` povoluje jen `/accounts/`) |
| Treasury — HTML výpis tiskových zpráv | **zatím nepoužít** | (1) | robots povoluje a `<time datetime>` je strojově čitelný, ale zaokrouhlený na čtvrthodiny a parsování HTML je křehké. Rozhodne E-6.23 (sankce a cla jsou pro ES relevantní). |
| ECB — tiskové zprávy | **použít** | 1 | robots povoluje, nízký objem; rozhodnutí ECB jsou v kalendáři, feed dodá text a čas |
| White House — prezidentské akty | **použít** | 1 | výnosy a proklamace (cla), ~5/den, conditional GET |
| White House — zprávy | **použít** | 1 | fact sheety ohlašují cla a dohody; zbytek PR odfiltruje klasifikace (E-6.27) |
| SEC EDGAR — aktuální 8-K | **použít s filtrem** | 1 | Atom s přesným časem přijetí, nejnovější 35 s, odstup 55 s. Bez filtru je to celý trh. Filtr na CIK mega caps v indexu (vzor relevance filtru Alpaca backfillu). UA s kontaktem, max. 10 požadavků/s. |
| FinancialJuice RSS | **nepoužít** | — | **podmínky zakazují roboty a sběr bez písemného svolení** a povolují jen osobní použití (oddíl 3). Feed sám funguje: odstup 68 s, nejnovější 31 min. |
| Finnhub general | **nepoužít jako breaking** (collector běží) | 3 | Reuters a Bloomberg s mediánem zpoždění 8,3 h (audit E-6.21). Snímek: nejnovější 98 min, odstup 17 min. Zda ho ponechat jako tier 3, rozhodne E-6.23. |
| Alpha Vantage NEWS_SENTIMENT | **nepoužít** | — | free tier má 25 požadavků **za den**, tedy nejvýš 1 dotaz za hodinu, a navíc potřebuje klíč. Sentiment z textu nemá edge (#740). |
| IBKR FLY (The Fly) | **nepoužít** | — | není mezi providery dostupnými přes API (2.3) |
| IBKR BZ (Benzinga) | **nepoužít** | — | 35 USD/měs. a obsahově duplikuje Benzinga Newsdesk, který chodí zdarma přes Alpaca |
| IBKR BRFUPDN (analytické akce) | **nepoužít** | — | zdarma, ale páska vrací Error 200 (#734). Analytické akce už nese Alpaca Newsdesk. |
| IBKR DJTOP | **nepoužít** | — | páska vrací Error 200 (sonda 21. 8., `newsticks.py:73-81`) a v API seznamu není |

**Shrnutí:** nové zdroje tier 1 jsou Fed testimony, BLS po releasech, BEA, ECB, White House (2 feedy)
a SEC 8-K s filtrem, celkem 7 feedů. Všechny jsou zdarma, bez klíče a s robots.txt, které je
povoluje. **Žádný nový headline feed tier 2 nevyhověl.** FinancialJuice brání podmínky, Alpha Vantage
limit a Finnhub zpoždění. Rychlé headliny dál nese jen Alpaca (Benzinga Newsdesk). **IBKR: nic
nového**, takže počet market data lines se nemění.

## 2. Naměřená čísla a zjištění

### 2.1 Snímek sondy

| tier | kandidát | UA | HTTP | formát | položek | s časem | stáří nejnovější | medián odstupu | conditional GET | rate limit | robots.txt |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Fed — všechny tiskové zprávy | browser | 200 | RSS | 20 | 20/20 | 46 min | 42,5 h | etag, last-modified | — | robots.txt 404 → bez omezení |
| 1 | Fed — měnová politika (dnes) | browser | 200 | RSS | 15 | 15/15 | 27,3 h | 15,0 d | etag, last-modified | — | robots.txt 404 → bez omezení |
| 1 | Fed — projevy (dnes) | browser | 200 | RSS | 15 | 15/15 | 12,8 h | 22,7 h | etag, last-modified | — | robots.txt 404 → bez omezení |
| 1 | Fed — svědectví v Kongresu | browser | 200 | RSS | 15 | 15/15 | 86,4 d | 70,0 d | etag, last-modified | — | robots.txt 404 → bez omezení |
| 1 | BLS — nejnovější releasy | contact | 200 | RSS | 1 | 1/1 | 6,4 d | — | etag, last-modified | — | povoleno |
| 1 | BEA — zprávy (www) | browser | 200 | RSS | 10 | 10/10 | 2,4 d | 14,0 d | etag, last-modified | — | povoleno |
| 1 | BEA — zprávy (apps) | browser | 200 | RSS | 48 | 48/48 | 2,4 d | 27,0 d | etag, last-modified | — | **zakázáno** |
| 1 | Treasury — tiskové zprávy (GovDelivery) | browser | 200 | RSS | 25 | 25/25 | 4,2 h | 11,6 h | etag | — | **zakázáno** |
| 1 | Treasury — výpis tiskových zpráv (HTML) | browser | 200 | HTML | 0 | — | — | — | etag, last-modified | — | povoleno |
| 1 | ECB — tiskové zprávy | browser | 200 | RSS | 15 | 15/15 | 9,8 h | 15,9 h | etag | — | povoleno |
| 1 | White House — zprávy | browser | 200 | RSS | 30 | 30/30 | 91 min | 3,8 h | etag, last-modified | — | povoleno |
| 1 | White House — prezidentské akty | browser | 200 | RSS | 30 | 30/30 | 25,2 h | 5,6 h | etag, last-modified | — | povoleno |
| 1 | SEC EDGAR — aktuální 8-K | contact | 200 | Atom | 100 | 100/100 | 35 s | 55 s | žádné | — | povoleno |
| 2 | FinancialJuice | browser | 200 | RSS | 100 | 100/100 | 31 min | 68 s | žádné | — | robots.txt 404 → bez omezení |
| 2 | Finnhub general | browser | 200 | JSON | 100 | 100/100 | 98 min | 17 min | žádné | x-ratelimit-limit: 60; x-ratelimit-remaining: 58 | povoleno |
| 2 | Alpha Vantage NEWS_SENTIMENT (demo klíč) | browser | 200 | JSON | 50 | 50/50 | 66 min | 19 min | žádné | — | robots.txt 404 → bez omezení |

- Feedy Fedu, BLS, BEA a White House nesou `ETag` nebo `Last-Modified`. Polling à 60 s s conditional
  GET (`ConditionalFetcher`) je proto levný. SEC, FinancialJuice a Finnhub validátory nemají.
- Alpha Vantage s demo klíčem vrací jen ukázkový ticker (AAPL). Pásmo `time_published` neuvádí,
  sonda ho bere jako UTC, takže stáří 66 min je jen orientační.

### 2.2 BLS: čas v feedu předbíhá release

`published` posledních položek feedů BLS (ručně z feedů, ET):

| feed | release (oficiální čas) | `published` ve feedu |
|---|---|---|
| `empsit` (2. 10. 2026) | 8:30 | 7:51:08 |
| `cpi` (11. 9. 2026) | 8:30 | 7:50:40 |
| `ppi` (10. 9. 2026) | 8:30 | 7:50:35 |
| `jolts` (29. 9. 2026) | 10:00 | 9:20:32 |
| `eci` (31. 7. 2026) | 8:30 | 7:50:32 |

Položka nese čas přípravy, typicky ~40 min před zveřejněním. Kdyby adaptér vzal `published` jako
`ts_event`, zpráva by v databázi předběhla release. Reakce trhu by se pak měřila od okamžiku, kdy
o zprávě nikdo nevěděl (look-ahead). **Pro E-6.25:** `ts_event` u BLS brát z času releasu
v kalendáři (shoda s FF) nebo z času prvního stažení, nikdy z `published`. BEA má `pubDate`
přesně v čase releasu (8:30 ET), ale kdy se položka ve feedu skutečně objeví, ověří až provoz.

### 2.3 IBKR provideři a market data lines

- **Přes API jsou jen 4 provideři:** Briefing.com Analyst Actions (BRFUPDN) zdarma, Briefing.com
  General Market Columns (BRFG) zdarma, Dow Jones Newsletters (DJNL) zdarma a Benzinga (BZ) za
  35 USD/měs. jako research subscription „Benzinga Breaking News via API“ ([IBKR: News Data][ibkr-news]).
  FLY ani DJTOP mezi nimi nejsou.
- Warning 321 sice FLY, BZ a DJTOP uvádí mezi platnými kódy pásek (`newsticks.py:91-92`) a sonda #734
  subskripci `BZ:BZ_ALL` a `FLY:FLY_ALL` přijala bez chyby. Bez API předplatného to ale nic neznamená,
  protože „přijato“ v sondě #734 = „do 3 s nepřišel Error 200“, ne „tečou zprávy“.
- BRFUPDN a DJTOP mají pásku s doloženým Error 200 (`newsticks.py:73-81`, `DEAD_TAPES`).
- **Lines:** dokumentace IBKR výjimku pro news neuvádí. Market data lines podle ní zahrnují „all data
  pulled through Trader Workstation watchlist and the API“ ([IBKR: Market Data Lines][ibkr-lines]).
  Engine přesto NEWS pásky do obsazenosti nepočítá a opírá se o neověřené tvrzení
  (`adapters.py:58`) → **#1477** (P3, měření se zastaveným enginem v pauze Globexu, se svolením
  vlastníka). Tento úkol žádnou pásku nepřidává, takže se počet lines nemění.

## 3. Podmínky použití a limity

| poskytovatel | podmínky (primární zdroj) | limit / podmínka přístupu |
|---|---|---|
| Fed | obsah webu je public domain, „may be copied and distributed without permission“ ([disclaimer][fed]) | — |
| BLS | vše publikované je public domain, žádá se citace ([Linking and Copyright][bls]). Roboty, které neodpovídají „BLS usage policy“, blokuje (403 bez identifikace). | UA s kontaktem |
| BEA | federální vládní obsah, public domain ([FAQ][bea]) | robots.txt: `apps.bea.gov` jen vyjmenované cesty |
| Treasury | [site policies][treasury] | GovDelivery robots.txt zakazuje |
| ECB | reprodukce povolena s uvedením zdroje ([disclaimer][ecb]) | — |
| White House | [copyright][wh]: obsah public domain kromě materiálů třetích stran | robots.txt zakazuje jen vyhledávání |
| SEC EDGAR | fair access: deklarovaný User-Agent se jménem a kontaktem ([Accessing EDGAR Data][sec]) | max. 10 požadavků/s |
| FinancialJuice | [Terms of Service][fj]: licence „for your own personal use“, zakázáno „data mining, robots, spiders, or similar data gathering and extraction tools“ bez písemného svolení; ke sběru a agregaci je nutná písemná licence | **nepoužitelné bez písemného svolení** |
| Finnhub | [Terms of Service][finnhub-tos], [ceník][finnhub-pricing] | free: 60 požadavků/min (hlavička `x-ratelimit-limit: 60`), klíč |
| Alpha Vantage | [Terms of Service][av-tos] | free: 25 požadavků/den ([premium][av-premium]), klíč |
| IBKR | [News Data][ibkr-news] | BZ 35 USD/měs.; ostatní jen BRFG, BRFUPDN, DJNL |

## 4. Vstupy pro další úkoly

- **E-6.23 (ADR-0059):**
  - Tier 1 = 7 feedů z oddílu 1 (+ běžící `fed_rss`).
  - Tier 2 dnes = jen Alpaca Newsdesk (žádný nový kandidát nevyhověl).
  - Tier 3 = články (Finnhub, RSS agentur).
  - Rozhodnout Treasury přes HTML výpis. Proměnná `GEXLENS_NEWS_SEC_CONTACT` slouží i BLS, takže
    zvážit obecný název.
- **E-6.25 (adaptéry tier 1):**
  - RSS přes `RssCollector` (ETag/Last-Modified už umí).
  - UA s kontaktem pro SEC a BLS, převedený na ASCII.
  - BLS `ts_event` ne z `published` (2.2).
  - EDGAR přes Atom `getcurrent` nebo CIK feedy mega caps s rozestupem pod 10 požadavků/s.
  - BEA z `www.bea.gov/news/rss`, ne z `apps`.
  - Podle zadání E-6.25 v #1406 tu zároveň vzniknou BLS a BEA místo nepoužitých klíčů z auditu #1473.
- **E-6.26 (adaptéry tier 2):** podle tohoto testu není co stavět. Pokud se objeví nový kandidát,
  stačí stejná sonda (doplnit `CANDIDATES`).

[ibkr-news]: https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/news-data
[ibkr-lines]: https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-lines/introduction
[fed]: https://www.federalreserve.gov/disclaimer.htm
[bls]: https://www.bls.gov/bls/linksite.htm
[bea]: https://www.bea.gov/help/faq
[treasury]: https://home.treasury.gov/subfooter/site-policies-and-notices
[ecb]: https://www.ecb.europa.eu/services/disclaimer/html/index.en.html
[wh]: https://www.whitehouse.gov/copyright/
[sec]: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
[fj]: https://www.financialjuice.com/tos.aspx
[finnhub-tos]: https://finnhub.io/terms-of-service
[finnhub-pricing]: https://finnhub.io/pricing
[av-tos]: https://www.alphavantage.co/terms_of_service/
[av-premium]: https://www.alphavantage.co/premium/
