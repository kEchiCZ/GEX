# Test kandidátů na zdroje breaking news (E-6.22)

Kandidáti na nové zdroje zpráv z rozhodnutí 8. 10. 2026 v #1385 (blok breaking news, Fáze 6 #1406).
Report je vstup pro ADR-0059 o tierech obsahu (E-6.23) a pro adaptéry E-6.25 a E-6.26. Navazuje na
audit stávajících zdrojů E-6.21 (`docs/research/1473-audit-zdroju-zprav.md`). Nic za paywallem,
přihlášením ani X. Registrace klíčů a účtů je mimo rozsah; chybějící klíč se uvádí jako podmínka.

## Jak se měřilo

**Sonda** `scripts/news_candidates_probe.py`, snímek `as_of` **2026-10-08T21:36:14Z** (čtvrtek
23:36 SELČ = 17:36 ET, po close US akcií; Globex v denní pauze). Na každého kandidáta jde jeden GET
a jeden GET `robots.txt` se stejnou hlavičkou `User-Agent`, jakou by posílal adaptér:
- prohlížečová `BROWSER_UA` jako dnešní fetcher (`gexlens_news/http.py`);
- u SEC a BLS identifikace s kontaktem z `GEXLENS_NEWS_SEC_CONTACT` (#1457). Bez ní obě vrací 403.
  Kontakt se převádí na ASCII, protože hlavička HTTP diakritiku nepřenese.

Robots.txt se vyhodnocuje podle RFC 9309 (`robots_allows`, test
`news-engine/tests/test_news_candidates_probe.py`). Standardní `urllib.robotparser` ukončí skupinu
prázdným řádkem, a proto na SEC chybně hlásil „povoleno“ (`docs/lessons-learned.md`). Položky s časem
před rokem 2000 nebo v budoucnosti se počítají zvlášť. Nic se nezapisuje a klíče se nevypisují.

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
| Fed — svědectví v Kongresu (`testimony.xml`) | **použít s výhradou** | 1 | pololetní svědectví předsedy hýbe trhem. Objem ~5 položek za rok (15 položek od 11/2023). **3 z 15 mají datum `30 Dec 1899`**, které by `RssCollector` uložil jako `ts_event` (2.2). |
| Fed — všechny tiskové zprávy (`press_all.xml`) | **nepoužít** | — | nad `press_monetary` přidává hlavně sankce, schválení akvizic bank a regulaci, tedy šum pro ES/NQ |
| BLS — feedy releasů (`/feed/{empsit,cpi,ppi,jolts,eci}.rss`) | **použít s výhradou** | 1 | titulek nese hlavní čísla (např. payrolls a nezaměstnanost). **Čas položky je ~40 min před releasem** (2.2). Vyžaduje UA s kontaktem. `bls_latest.rss` drží jen 1 položku, nepoužít. |
| BEA — `www.bea.gov/news/rss` | **použít** | 1 | `pubDate` = čas releasu (8:30 ET), robots povoluje, conditional GET. Úplnost feedu je otevřený bod (2.2). |
| BEA — `apps.bea.gov/rss/rss.xml` | **nepoužít** | — | **robots.txt zakazuje** (`apps.bea.gov` povoluje jen vyjmenované cesty) |
| Treasury — GovDelivery RSS | **nepoužít** | — | **robots.txt zakazuje** (`public.govdelivery.com` povoluje jen `/accounts/`) |
| Treasury — HTML výpis tiskových zpráv | **zatím nepoužít** | (1) | robots povoluje a `<time datetime>` je strojově čitelný, ale zaokrouhlený na čtvrthodiny a parsování HTML je křehké. Rozhodne E-6.23 (sankce a cla jsou pro ES relevantní). |
| ECB — tiskové zprávy | **použít** | 1 | robots povoluje, nízký objem; rozhodnutí ECB jsou v kalendáři, feed dodá text a čas |
| White House — prezidentské akty | **použít** | 1 | výnosy a proklamace (cla), odstup 5,6 h, conditional GET |
| White House — zprávy | **použít** | 1 | fact sheety ohlašují cla a dohody, odstup 3,8 h; zbytek PR odfiltruje klasifikace (E-6.27) |
| SEC EDGAR — `getcurrent` 8-K (Atom) | **nepoužít** | — | **robots.txt zakazuje `/cgi-bin`** (2.4). Feed sám funguje: nejnovější 6 min, odstup 53 s. |
| SEC `data.sec.gov` — podání podle CIK (JSON) | **varianta k rozhodnutí v E-6.23** | 1 | robots.txt na hostiteli není (404 → bez omezení). Čas přijetí 8-K je v JSON. Obsah se filtruje na CIK mega caps v indexu. Max. 10 požadavků/s. Varianty jsou v 2.4. |
| FinancialJuice RSS | **nepoužít** | — | **podmínky zakazují roboty a sběr bez písemného svolení** a povolují jen osobní použití (oddíl 3). Feed sám funguje: odstup 68 s, nejnovější 51 min. Při druhém stažení do minuty vrátil 429 s `Retry-After: 60`. |
| Finnhub general | **nepoužít jako breaking** (collector běží) | 3 | Reuters a Bloomberg s mediánem zpoždění 8,3 h (audit E-6.21). Snímek: nejnovější 118 min, odstup 17 min. Zda ho ponechat jako tier 3, rozhodne E-6.23. |
| Alpha Vantage NEWS_SENTIMENT | **nepoužít** | — | free tier má 25 požadavků **za den**, tedy nejvýš 1 dotaz za hodinu, a navíc potřebuje klíč. Sentiment z textu nemá edge (#740). |
| IBKR FLY (The Fly) | **nepoužít** | — | účet ho nemá a mezi providery dostupnými přes API není (2.3) |
| IBKR BZ (Benzinga) | **nepoužít** | — | účet ho nemá; 35 USD/měs. a obsahově duplikuje Benzinga Newsdesk, který chodí zdarma přes Alpaca |
| IBKR BRFUPDN (analytické akce) | **nepoužít** | — | účet ho má zdarma, ale páska vrací Error 200 (#734). Analytické akce už nese Alpaca Newsdesk. |
| IBKR DJTOP | **nepoužít** | — | účet ho nemá, páska vrací Error 200 (`newsticks.py:74-81`) a v API seznamu není |

**Shrnutí:**
- **Nové zdroje tier 1** jsou Fed testimony, 5 feedů BLS, BEA (www), ECB a 2 feedy White House.
  Všechny jsou zdarma, bez klíče a robots.txt je povoluje. BLS a testimony potřebují v adaptéru
  vlastní zdroj `ts_event`.
- **SEC** je k rozhodnutí: `getcurrent` zakazuje robots.txt, cesta přes `data.sec.gov` je povolená.
- **Tier 2 nemá žádný nový headline feed.** FinancialJuice brání podmínky, Alpha Vantage limit
  a Finnhub zpoždění. Rychlé headliny dál nese jen Alpaca (Benzinga Newsdesk).
- **IBKR: nic nového**, takže počet market data lines se nemění.

## 2. Naměřená čísla a zjištění

### 2.1 Snímek sondy

| tier | kandidát | UA | HTTP | formát | položek | s časem | nejnovější (UTC) | stáří nejnovější | medián odstupu | conditional GET | rate limit | robots.txt | poznámka |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Fed — všechny tiskové zprávy | browser | 200 | RSS | 20 | 20/20 | 2026-10-08 20:30:00 | 66 min | 42,5 h | etag, last-modified | — | robots.txt 404 → bez omezení | — |
| 1 | Fed — měnová politika (dnes) | browser | 200 | RSS | 15 | 15/15 | 2026-10-07 18:00:00 | 27,6 h | 15,0 d | etag, last-modified | — | robots.txt 404 → bez omezení | — |
| 1 | Fed — projevy (dnes) | browser | 200 | RSS | 15 | 15/15 | 2026-10-08 08:30:00 | 13,1 h | 22,7 h | etag, last-modified | — | robots.txt 404 → bez omezení | — |
| 1 | Fed — svědectví v Kongresu | browser | 200 | RSS | 15 | 12/15 | 2026-07-14 12:30:00 | 86,4 d | 70,0 d | etag, last-modified | — | robots.txt 404 → bez omezení | 3 s nesmyslným časem (< 2000 nebo v budoucnosti) |
| 1 | BLS — nejnovější releasy | contact | 200 | RSS | 1 | 1/1 | 2026-10-02 12:32:11 | 6,4 d | — | etag, last-modified | — | povoleno | — |
| 1 | BLS — empsit | contact | 200 | Atom | 12 | 12/12 | 2026-10-02 11:51:08 | 6,4 d | 28,0 d | etag, last-modified | — | povoleno | — |
| 1 | BLS — cpi | contact | 200 | Atom | 12 | 12/12 | 2026-09-11 11:50:40 | 27,4 d | 30,0 d | etag, last-modified | — | povoleno | — |
| 1 | BLS — ppi | contact | 200 | Atom | 12 | 12/12 | 2026-09-10 11:50:35 | 28,4 d | 29,0 d | etag, last-modified | — | povoleno | — |
| 1 | BLS — jolts | contact | 200 | Atom | 12 | 12/12 | 2026-09-29 13:20:32 | 9,3 d | 29,0 d | etag, last-modified | — | povoleno | — |
| 1 | BLS — eci | contact | 200 | Atom | 12 | 12/12 | 2026-07-31 11:50:32 | 69,4 d | 92,0 d | etag, last-modified | — | povoleno | — |
| 1 | BEA — zprávy (www) | browser | 200 | RSS | 10 | 10/10 | 2026-10-06 12:30:00 | 2,4 d | 14,0 d | etag, last-modified | — | povoleno | — |
| 1 | BEA — zprávy (apps) | browser | 200 | RSS | 48 | 48/48 | 2026-10-06 12:30:00 | 2,4 d | 27,0 d | etag, last-modified | — | **zakázáno** | — |
| 1 | Treasury — tiskové zprávy (GovDelivery) | browser | 200 | RSS | 25 | 25/25 | 2026-10-08 17:02:18 | 4,6 h | 11,6 h | etag | — | **zakázáno** | — |
| 1 | Treasury — výpis tiskových zpráv (HTML) | browser | 200 | HTML | 0 | — | — | — | — | etag, last-modified | — | povoleno | — |
| 1 | ECB — tiskové zprávy | browser | 200 | RSS | 15 | 15/15 | 2026-10-08 11:30:00 | 10,1 h | 15,9 h | etag | — | povoleno | — |
| 1 | White House — zprávy | browser | 200 | RSS | 30 | 30/30 | 2026-10-08 19:44:36 | 112 min | 3,8 h | etag, last-modified | — | povoleno | — |
| 1 | White House — prezidentské akty | browser | 200 | RSS | 30 | 30/30 | 2026-10-07 20:05:21 | 25,5 h | 5,6 h | etag, last-modified | — | povoleno | — |
| 1 | SEC EDGAR — aktuální 8-K | contact | 200 | Atom | 100 | 100/100 | 2026-10-08 21:30:13 | 6 min | 53 s | žádné | — | **zakázáno** | — |
| 1 | SEC data.sec.gov — podání jedné firmy (Apple, jen 8-K) | contact | 200 | JSON | 104 | 104/104 | 2026-09-02 00:30:35 | 36,9 d | 28,0 d | žádné | — | robots.txt 404 → bez omezení | — |
| 2 | FinancialJuice | browser | 200 | RSS | 100 | 100/100 | 2026-10-08 20:45:23 | 51 min | 68 s | žádné | — | robots.txt 404 → bez omezení | — |
| 2 | Finnhub general | browser | 200 | JSON | 100 | 100/100 | 2026-10-08 19:38:14 | 118 min | 17 min | žádné | x-ratelimit-limit: 60; x-ratelimit-remaining: 58 | povoleno | — |
| 2 | Alpha Vantage NEWS_SENTIMENT (demo klíč) | browser | 200 | JSON | 50 | 50/50 | 2026-10-08 20:10:16 | 86 min | 19 min | žádné | — | robots.txt 404 → bez omezení | — |

- Feedy Fedu, BLS, BEA a White House nesou `ETag` nebo `Last-Modified`. Polling à 60 s s conditional
  GET (`ConditionalFetcher`) je proto levný. SEC, FinancialJuice a Finnhub validátory nemají.
- Alpha Vantage s demo klíčem vrací jen ukázkový ticker (AAPL). Pásmo `time_published` neuvádí,
  sonda ho bere jako UTC, takže stáří 86 min je jen orientační.
- `data.sec.gov` označuje `acceptanceDateTime` jako UTC (`Z`). Že to opravdu je UTC, se neověřovalo
  proti času téhož podání v Atomu EDGAR. Patří to do E-6.25.

### 2.2 Čas položek: BLS předbíhá release, Fed testimony má rok 1899

**BLS:** nejnovější položky ze snímku 2.1, převedené z UTC na ET (EDT, UTC−4):

| feed | release (oficiální čas, ET) | čas položky (ET) | předstih |
|---|---|---|---|
| `empsit` (2. 10. 2026) | 8:30 | 7:51:08 | 39 min |
| `cpi` (11. 9. 2026) | 8:30 | 7:50:40 | 39 min |
| `ppi` (10. 9. 2026) | 8:30 | 7:50:35 | 39 min |
| `jolts` (29. 9. 2026) | 10:00 | 9:20:32 | 39 min |
| `eci` (31. 7. 2026) | 8:30 | 7:50:32 | 39 min |

Položka nese čas přípravy, ne zveřejnění. Kdyby adaptér vzal tento čas jako `ts_event`, zpráva by
v databázi předběhla release. Reakce trhu by se pak měřila od okamžiku, kdy o zprávě nikdo nevěděl
(look-ahead). Kdy se položka ve feedu skutečně objeví, ukáže až provoz kolem příštího releasu.

**Fed testimony:** 3 z 15 položek mají `pubDate` `30 Dec 1899`. Ostatní feedy Fedu ten problém
nemají, takže běžící `fed_rss` zasažený není.

**Proč to řeší adaptér:** `RssCollector.normalize` bere `ts_event = published or fetched_at`
(`news-engine/src/gexlens_news/collectors/rss.py:256-266`). Feedy BLS a testimony proto nejde
připojit jen jako další URL. Adaptér musí zdroj `ts_event` přepsat:
- BLS: čas releasu z kalendáře, nebo čas prvního stažení;
- nesmyslný čas: čas stažení.

**BEA:** `pubDate` = 8:30 ET potvrzuje čas releasu. Snímek má ale mezeru 21. 7. → 24. 9. bez
měsíčních releasů (GDP, PCE, obchod). Úplnost feedu je **otevřený bod** a před E-6.25 se ověří proti
kalendáři releasů.

### 2.3 IBKR provideři a market data lines

- **Co účet má:** `reqNewsProviders` vrátil 18. 8. 2026 (sonda `scripts/news_providers_probe.py`,
  #734) kódy BRFG, BRFUPDN, DJ-N, DJ-RT, DJ-RTA, DJ-RTE, DJ-RTG a DJNL. Účet tedy má BRFUPDN,
  ale ne BZ, FLY ani DJTOP. Pět kódů `DJ-*` je pro článkové API (`reqNewsArticle`), jejich páska
  vrací Warning 321 (`newsticks.py:74-81`).
- **Co jde přes API odebírat:** stránka IBKR uvádí 4 providery:
  - Briefing.com Analyst Actions (BRFUPDN), zdarma;
  - Briefing.com General Market Columns (BRFG), zdarma;
  - Dow Jones Newsletters (DJNL), zdarma;
  - Benzinga (BZ) za 35 USD/měs., jako research subscription „Benzinga Breaking News via API“
    ([IBKR: News Data][ibkr-news]).

  FLY ani DJTOP mezi nimi nejsou.
- Warning 321 sice FLY, BZ a DJTOP uvádí mezi platnými kódy pásek (`newsticks.py:90-91`) a sonda #734
  subskripci `BZ:BZ_ALL` a `FLY:FLY_ALL` přijala bez chyby. „Přijato“ v #734 ale znamená jen „do 3 s
  nepřišel Error 200“, ne „tečou zprávy“, a bez API předplatného to nic neznamená.
- BRFUPDN a DJTOP mají pásku s doloženým Error 200 (`newsticks.py:74-81`, `DEAD_TAPES`).
- **Lines:** dokumentace IBKR výjimku pro news neuvádí. Market data lines podle ní zahrnují „all data
  pulled through Trader Workstation watchlist and the API“ ([IBKR: Market Data Lines][ibkr-lines]).
  Engine přesto NEWS pásky do obsazenosti nepočítá a opírá se o neověřené tvrzení
  (`adapters.py:58`) → **#1477** (P3, měření se zastaveným enginem v pauze Globexu, se svolením
  vlastníka). Tento úkol žádnou pásku nepřidává, takže se počet lines nemění.

### 2.4 SEC: robots.txt a varianty pro E-6.23

`www.sec.gov/robots.txt` má jedinou skupinu `User-agent: *` a v ní `Disallow: /cgi-bin` i
`Allow: /Archives/edgar/data`. Atom `getcurrent` (`/cgi-bin/browse-edgar?…`) i feedy firem
`browse-edgar?action=getcompany` jsou tedy zakázané. Pravidla SEC pro automatizovaný přístup
(fair access, deklarovaný User-Agent, max. 10 požadavků/s) robots.txt neruší. `data.sec.gov`
robots.txt nemá (404, podle RFC 9309 bez omezení).

| varianta | výhody | nevýhody |
|---|---|---|
| **A — SEC nepoužít** | nic nového k údržbě | 8-K mega caps (výsledky, M&A) zůstanou jen přes Benzinga |
| **B — `data.sec.gov/submissions/CIK{10}.json` po firmách** (doporučeno) | robots povoluje, JSON s formulářem a časem přijetí, filtr na CIK je přirozený; 30 firem à 60 s = 0,5 požadavku/s | dotaz na firmu (ne jeden feed), bez conditional GET; čas `Z` ověřit (2.1) |
| **C — `getcurrent` přes robots.txt** | jeden feed pro celý trh, odstup ~1 min | porušuje robots.txt; jen s výslovným rozhodnutím vlastníka |
| **D — `/Archives/edgar/` (Allow)** | robots povoluje | denní indexy a archiv, ne živý tok; pro breaking pomalé |

**Doporučení: B**:
- **Rychlost:** čas přijetí podání ze zdroje, polling à 60 s.
- **Výkon:** desítky malých JSON za minutu.
- **Relevance:** jen firmy, které hýbou indexem.

## 3. Podmínky použití a limity

| poskytovatel | podmínky (primární zdroj) | limit / podmínka přístupu |
|---|---|---|
| Fed | obsah webu je public domain, „may be copied and distributed without permission“ ([disclaimer][fed]) | — |
| BLS | vše publikované je public domain, žádá se citace ([Linking and Copyright][bls]). Roboty, které neodpovídají „BLS usage policy“, blokuje (403 bez identifikace). | UA s kontaktem |
| BEA | federální vládní obsah, public domain ([FAQ][bea]) | robots.txt: `apps.bea.gov` jen vyjmenované cesty |
| Treasury | [site policies][treasury] | GovDelivery robots.txt zakazuje |
| ECB | reprodukce povolena s uvedením zdroje ([disclaimer][ecb]) | — |
| White House | [copyright][wh]: obsah public domain kromě materiálů třetích stran | robots.txt zakazuje jen vyhledávání |
| SEC EDGAR | fair access: deklarovaný User-Agent se jménem a kontaktem ([Accessing EDGAR Data][sec]) | max. 10 požadavků/s; robots.txt zakazuje `/cgi-bin` (2.4) |
| FinancialJuice | [Terms of Service][fj]: licence „for your own personal use“, zakázáno „data mining, robots, spiders, or similar data gathering and extraction tools“ bez písemného svolení; ke sběru a agregaci je nutná písemná licence | **nepoužitelné bez písemného svolení**; 429 při 2 požadavcích do minuty |
| Finnhub | [Terms of Service][finnhub-tos], [ceník][finnhub-pricing] | free: 60 požadavků/min (hlavička `x-ratelimit-limit: 60`), klíč |
| Alpha Vantage | [Terms of Service][av-tos] | free: 25 požadavků/den ([premium][av-premium]), klíč |
| IBKR | [News Data][ibkr-news] | BZ 35 USD/měs.; zdarma jen BRFG, BRFUPDN, DJNL |

## 4. Vstupy pro další úkoly

- **E-6.23 (ADR-0059):**
  - Tier 1: feedy z oddílu 1 a běžící `fed_rss`.
  - Tier 2: dnes jen Alpaca Newsdesk, žádný nový kandidát nevyhověl.
  - Tier 3: články (Finnhub, RSS agentur).
  - Rozhodnout SEC (2.4, doporučeno B) a Treasury přes HTML výpis.
  - Proměnná `GEXLENS_NEWS_SEC_CONTACT` slouží i BLS, takže zvážit obecný název.
- **E-6.25 (adaptéry tier 1):**
  - RSS přes `RssCollector` (ETag/Last-Modified už umí), ale s přepsatelným zdrojem `ts_event`.
    BLS nesmí brát čas položky, nesmyslný čas (rok 1899) nahradit časem stažení (2.2).
  - UA s kontaktem pro SEC a BLS, převedený na ASCII.
  - BEA z `www.bea.gov/news/rss`, ne z `apps`; předtím ověřit úplnost feedu proti kalendáři.
  - SEC podle rozhodnutí E-6.23 a s ověřením pásma `acceptanceDateTime`.
  - Podle zadání E-6.25 v #1406 zde zároveň vzniknou BLS a BEA místo nepoužitých klíčů z auditu #1473.
- **E-6.26 (adaptéry tier 2):** podle tohoto testu není co stavět. Nový kandidát se ověří stejnou
  sondou (doplnit `CANDIDATES`).

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
