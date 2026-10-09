# ADR-0059: Breaking news — tier obsahu, záznam všech zdrojů u duplicit, `is_breaking` a téma, nové oficiální zdroje

- **Stav:** přijato (9. 10. 2026, rozhodnutí vlastníka v chatu, zapsané v PR #1484: doporučené
  varianty všech bodů; „výzkum bank“ varianta A, protože původní záměr skupiny už není znám)
- **Revize 9. 10. 2026 odpoledne (E-6.27, #1491):** bod 4 — významná zpráva tier 3 jde na kartu
  hned se štítkem „článek, zatím nepotvrzeno“ (rozhodnutí vlastníka, Rozhodnuto v #1385);
  výjimka mega caps předregistrované kritérium nesplnila, takže se nezavádí a SEC se nepoužije
  (bod 6 varianta A, E-6.25b zrušen)
- **Datum:** 2026-10-09
- **Souvisí:** #1482 (E-6.23), #1406 (Fáze 6), #1385 (Rozhodnuto 8. 10. — breaking news), audit
  zdrojů #1473 (`docs/research/1473-audit-zdroju-zprav.md`), test kandidátů #1474
  (`docs/research/1474-kandidati-zdroju-zprav.md`), #1457 (kontakt pro SEC), #1338 (objem řad),
  #740 (směr z textu bez edge), #1291 (výsledky firem nejsou významné), ADR-0016 (**bod 2
  nahrazuje**), ADR-0043 (shluky, výchylka), ADR-0045 (klasifikace v2, jeden zdroj významnosti),
  sentiment SPEC kap. 1 a 3.3

## Kontext

Zadání vlastníka 8. 10. 2026 (#1385): karta „Breaking news“ s naměřeným dopadem na ES/NQ, nové
zdroje zpráv, deduplikace se záznamem všech zdrojů, `is_breaking`, kategorie (makro data,
centrální banky, geopolitika, firmy, výzkum bank) a téma. Audit stávajících zdrojů (E-6.21)
a test kandidátů (E-6.22) daly tato tvrdá data:

1. **Rychlé zdroje jsou dnes dva:** Alpaca (Benzinga Newsdesk, push, medián ≈ 0 s) a Bluesky
   (2,6–5,1 s). Oficiální zdroj je jediný, `fed_rss` se 2 feedy a ~0,4 zprávy denně. Finnhub
   general nese Reuters a Bloomberg s mediánem zpoždění 8,3 h (audit, oddíl 1 a 2.2).
2. **`raw.merged_sources` se do DB nikdy nedostane** (0 záznamů v celé historii, audit 2.5).
   Sloučení se ukládá jen k eventu ze stejné dávky (`news-engine/src/gexlens_news/pipeline.py:44-60`),
   každý collector ale zapisuje vlastní dávku (`runner.py:78`). Čítače `merged_total`
   a `duplicates_total` (`pipeline.py:32-33`) nikdo nečte. IBKR pásky zapisuje engine mimo
   deduplikaci, jen přes unikátní `dedup_hash` (`engine/src/gexlens_engine/ibkr/newsticks.py:344-353`).
   `dedup_hash` = normalizovaný titulek + den `ts_event` (`compute/newstext.py:122-134`). Bod 2
   ADR-0016 a sentiment SPEC 3.3 tedy slibují něco, co se neděje. Kdo byl první, se z DB určit
   nedá (audit 3.1).
3. **„Tier“ už v projektu znamená čtyři věci:**
   - Tier A–D v sentiment SPEC kap. 1 (rodina zdrojů);
   - role zdroje `news_sources.tier` = `core`/`extra`/`test` (`engine/src/gexlens_engine/storage/sentiment.py:141-150`).
     Panel Zdroje ji jen zobrazuje (`frontend/src/components/NewsSourcesSection.tsx:254`);
     `PATCH /news/sources/{source}` mění jen `enabled` (`api/src/gexlens_api/sentiment_routes.py:512-528`);
   - `significance_tier` 0–3 (ADR-0045 bod 5);
   - „US data tier 1/2“ v klasifikátoru (`classifier.py:302-311`).

   Pátý význam potřebuje vlastní identifikátor.
4. **Tier obsahu se nedá určit jen podle zdroje.** Alpaca nese ze 71 % headliny Benzinga Newsdesku
   (3 310 ze 4 675 za 7 dní). Zbylých 29 % tvoří šablonové články Insights a články autorů
   (audit 2.1; autor je v `raw.author`, `collectors/alpaca.py:79`). Bluesky rozlišuje
   kurátorované autory (`raw.curated`, `feed_of` v `classifier.py:122-136`). Příznak `raw.curated`
   ale existuje až od 25. 9. a pro starší posty se kurátor určuje podle aktuálního seznamu
   (ADR-0045 bod 8).
5. **Významnost má jeden zdroj** (ADR-0045 bod 5, `compute/news_significance.py:40-60`).
   Importance zapisují čtyři cesty, proto ADR-0045 odmítl ukládat cokoli z ní odvozeného.
   Zásadní zpráva (`is_key`) už existuje. **Výsledky firem nejsou významné nikdy**
   (`EXCLUDED_CATEGORIES = {"EARNINGS"}`, `news_significance.py:30`, #1291 varianta B: přepisy
   hovorů a výsledky drobných firem index nehýbou).
6. **Kategorie `NEWS_CATEGORIES`** (`storage/sentiment.py:77-88`) jsou klíčem bucketu
   empirického modelu (`news_model_stats`, `storage/sentiment.py:405-420`) a pravidla kontaminace
   K1 (`reactions.contaminates`, `news-engine/src/gexlens_news/reactions.py:171`). Analytické akce
   („price target“) klasifikátor v2 záměrně shazuje na importance 1 (`classifier.py:151`, ADR-0045
   bod 1).
7. **Kandidáti (E-6.22, oddíl 1):**
   - Nové zdroje tier 1: Fed testimony, BLS ×5, BEA (www), ECB, White House ×2. Všechny zdarma,
     bez klíče a robots.txt je povoluje.
   - SEC: `getcurrent` robots.txt zakazuje (`/cgi-bin`). `data.sec.gov` robots.txt nemá
     (404 → bez omezení).
   - Treasury: GovDelivery robots.txt zakazuje. HTML výpis povoluje, ale čas má zaokrouhlený
     na čtvrthodiny.
   - Tier 2 nemá nový použitelný feed (FinancialJuice brání podmínky, Alpha Vantage limit
     25 dotazů denně).
   - IBKR nemá nic nového, takže se počet market data lines nemění.
8. **Čas položky není vždy čas zveřejnění.** BLS má čas položky 39 min před releasem a 3 z 15
   položek Fed testimony mají rok 1899 (E-6.22, 2.2). `RssCollector` bere
   `ts_event = published or fetched_at` (`collectors/rss.py:256-266`). Převzatý čas BLS by byl
   look-ahead.
9. **Reakce se zapisuje až po uzavření nejdelšího okna.** `ReactionJob` měří event až
   `max(windows)` = 60 min po `ts_event` (`news-engine/src/gexlens_news/reaction_job.py:160`).
   `news_reactions` nese `ret_<w>` (close-to-close) a `range_<w>` (high − low). Close-to-close
   míjí whipsaw: FOMC 16. 9. má ES −0,3 bp, ale výchylku −19 bp (ADR-0043, Kontext).
10. **Odchylky od sentiment SPEC kap. 1** (ADR má přednost):
    - Tier A plánuje BLS API v2 a BEA API pro oficiální hodnoty releasů. Tento ADR přidává
      **zprávy** z jejich RSS a hodnoty releasů nechává kalendáři FF.
    - Tier B vede Finnhub jako hlavní headline zdroj. Měření ho řadí mezi články (bod 1 níže).

## Rozhodnutí

Vlastník 9. 10. 2026 přijal doporučené varianty všech bodů. Varianty jsou v oddílu „Zvažované
varianty“. Výjimka mega caps (bod 4), a s ní SEC (bod 6), platila jen po splnění
předregistrovaného kritéria. Měření ho nesplnilo (revize bodu 4).

1. **Tier obsahu = `content_tier`** (1 oficiální zdroj, 2 headline feed, 3 článek nebo analýza,
   NULL = mimo tiery). V UI se zobrazuje jako „Tier 1/2/3“. Identifikátor `content_tier` se
   nepoplete s rolí zdroje ani se stupněm významnosti.

   | `content_tier` | zdroj a podskupina |
   |---|---|
   | 1 | `fed_rss` (včetně testimony), nové `bls`, `bea`, `ecb`, `whitehouse` (a `sec` podle bodu 6) |
   | 2 | `alpaca` s autorem Benzinga Newsdesk; `bluesky` s kurátorovaným autorem |
   | 3 | `alpaca` ostatní autoři (Insights, články); `rss_news` (CNBC, MarketWatch, Yahoo); `finnhub`; `ibkr_brfg`; `ibkr_djnl`; `rss_user` |
   | NULL | `forexfactory` (kalendář má vlastní cestu a `is_key`); `bluesky` bez kurátora; `reddit_rss` |

2. **Kde tier žije:**
   - Sloupec `news_events.content_tier SMALLINT NULL`. Zapisuje ho při ingestu jedna čistá
     funkce `content_tier(source, raw)` v `engine/compute/` vedle `news_significance`. Volá ji
     zápis news-enginu i zápis IBKR pásek v enginu.
   - Vstupy nového řádku (zdroj, autor, `raw.curated`, feed) se po zápisu nemění, takže uložená
     hodnota se nerozjede. U importance to neplatí, proto ADR-0045 její odvozeniny neukládá.
   - Historii doplní jednorázový idempotentní backfill. Kurátora u postů před 25. 9. určí
     stejně jako ADR-0045 bod 8: autor má post s příznakem, nebo je v aktuálním seznamu.
   - **Efektivní tier zprávy** je nejnižší tier ze všech doručení viditelných v čase *t*:
     z prvního doručení a z kopií v `news_event_sources` (bod 3). Bez toho by o `is_breaking`
     rozhodovalo pořadí doručení. Když CNBC RSS přijde dřív než Benzinga Newsdesk, zpráva
     by na kartu nepřišla (po revizi bodu 4 z 9. 10.: zůstala by označená jako nepotvrzená).
   - Registr `news_sources.tier` (role zdroje) zůstává beze změny.
   - **Objem (#1338):** sloupec na stávajících řádcích. Zpráv je ~2 200 denně v průměru 7 dní
     (včetně víkendu) a ~2 900 za 24 h pracovního dne (audit 2.1), tedy ~0,8–1,1 mil. řádků
     za rok × 2 B ≈ **2 MB za rok**. Index se nezakládá: karta i report čtou úzké časové okno
     přes `ix_news_events_ts` (`storage/sentiment.py:131`).
3. **Záznam všech zdrojů u duplicit** nahrazuje bod 2 ADR-0016 a větu o `raw.merged_sources`
   v sentiment SPEC 3.3. Vzniká tabulka `news_event_sources`:

   | sloupec | význam |
   |---|---|
   | `event_id` | FK na `news_events.id` (první doručení) |
   | `source` | zdroj kopie |
   | `source_uid` | id položky u zdroje, pokud ho má |
   | `content_tier` | tier kopie (bod 2) |
   | `published_at` | čas publikace podle zdroje kopie, pravidla jako u `ts_event` (bod 5) |
   | `fetched_at` | kdy kopie přišla; od tohoto okamžiku je kopie viditelná (point-in-time pro replay) |

   Primární klíč je `(event_id, source)`, zápis je tedy idempotentní.
   - **Kdy řádek vzniká:** pro každou kopii z **jiného** zdroje, kterou zahodí rolling dedup
     (exaktní i Jaccard) nebo unikátní `dedup_hash`, včetně IBKR pásek z enginu. Kopie
     z téhož zdroje zůstává duplicitou bez záznamu jako dosud.
   - **Co se do tabulky nedává:**
     - první doručení zůstává jen v `news_events` (`source`, `ts_event`, `ts_ingested`);
     - payload kopie se neukládá, protože titulek je podle definice duplicity stejný nebo
       téměř stejný (Jaccard ≥ 0,9).
       Tím se ruší slib SPEC 3.3, že „`raw` uchová všechny payloady“.
   - **`ts_event` se nepřepisuje**, protože od něj se měří reakce. Nejdřívější publikace je
     odvozená hodnota `min(ts_event, min(published_at))`, počítaná při čtení.
   - Tabulka je aditivní, nic se nemaže. Mrtvou cestu `raw.merged_sources`, nečtené čítače
     a test dávky ze dvou zdrojů (`news-engine/tests/test_dedup.py:308`) odstraní E-6.24b.
   - **Dvě úrovně se nepletou:** `news_event_sources` je **táž zpráva** z více zdrojů, shluk
     (ADR-0043, E-6.6, E-6.17) jsou **různé zprávy** o téže události. Srovnání „kdo byl první“
     přes různé titulky (8-K × headline) proto potřebuje shluk nebo párování, ne tuto tabulku.
   - **Objem (#1338):** počet kopií dnes nikdo neměří, protože se čítače nečtou. Hrubý odhad
     s jednou kopií na zprávu: ~1,1 mil. řádků × ~130 B (řádek ~100 B a PK ~30 B, odhad
     z šířky sloupců) ≈ **140 MB za rok**. Skutečný denní objem změří E-6.29.
4. **`is_breaking`, skupina a téma se počítají při čtení, nic z toho se neukládá:**
   - `is_breaking = efektivní tier ∈ {1, 2, 3} ∧ is_significant(kind, importance, category)`
     (ADR-0045 bod 5; **revize 9. 10.:** původně jen tier 1–2). Tier NULL (kalendář, Reddit,
     Bluesky bez kurátora) na kartu nejde.
     - **Potvrzení:** zpráva s efektivním tierem 1–2 je potvrzená. Zpráva s efektivním tierem 3
       jde na kartu **hned** se štítkem „článek, zatím nepotvrzeno“. Štítek zmizí v okamžiku,
       kdy je viditelná kopie tier 1–2 (`fetched_at`, point-in-time). Důvod (vlastník 9. 10.):
       trader potřebuje zprávu co nejdřív, i když ji pomalejší zdroj přinese dřív než Benzinga
       Newsdesk. Kopie je jen táž zpráva (bod 3), takže článek s jiným titulkem o téže události
       potvrdí až shluk (E-6.6, E-6.17).
     - **Šum** změřil E-6.27 (`docs/research/1491-sum-karty-breaking.md`): za 30 dní polovina
       karty nepotvrzená, ~42 zpráv za obchodní seanci, z toho 40 % Finnhub s mediánem zpoždění
       11 h. Zpřísnění (jen `is_key`, jen čerstvé zprávy) jen po rozhodnutí vlastníka;
       přeměření s kopiemi #1492.
     - Předfiltr v SQL (`importance ≥ 2` a tier není NULL v `news_events` **nebo**
       v `news_event_sources`) je nadmnožina. Přesné pravidlo běží v jedné funkci
       (`news-engine/src/gexlens_news/breaking.py`).
     - Výpočet při čtení v API má precedens ve významnosti (ADR-0045 bod 5).
     - Vlastní slovník klíčových slov ani kritérium „krátký headline“ nevzniká. Fed, CPI, NFP
       a cla už jsou spouštěče v2 (`EVENT_KINDS`, `classifier.py:542-544`) a krátký titulek
       zajišťuje tier 2.
     - Chybějící téma (např. stres bank) se doplní do klasifikátoru jako nová verze s golden
       testem a `is_breaking` ho převezme.
   - **Firmy na kartě:** ADR-0045 výsledky firem z významnosti vyřazuje vždy (Kontext 5).
     Na kartě by tak skupina „firmy“ zůstala prázdná a SEC by na ni nikdy nic nedodal.
     Navržená výjimka **jen pro kartu**: zpráva tier 1–2 v kategorii `EARNINGS`
     s importance ≥ 2, jejíž `symbols` obsahují firmu ze seznamu mega caps (bod 6), je
     breaking. Významnost pro upozornění, gate a model by se neměnila.
     - `TECH` výjimku nepotřebuje, s importance ≥ 2 je významné už dnes
       (`news_significance.py:47`).
     - **Podmínka (předregistrované kritérium):** E-6.27 nejdřív z `news_reactions` porovná
       `range_5` a |`ret_5`| NQ u `EARNINGS` mega caps proti ostatním `EARNINGS`, bez
       kontaminovaných oken. Výjimka se zavede, jen když:
       - má každá skupina n ≥ 30;
       - 95% bootstrap CI rozdílu mediánů `range_5` leží nad nulou.
     - **Výsledek (revize 9. 10., `docs/research/1491-mega-caps-earnings.md`): nesplněno,
       výjimka se nezavádí.** Upřesnění zapsaná v #1491 před spuštěním: jen éra živého sběru od
       28. 7. 2026 (backfill Alpaca výsledky malých firem neobsahuje), bez odložených reakcí,
       kontrola robustnosti přes unikátní minutu okna NQ. Mega caps n = 81, ostatní n = 2 876:
       rozdíl mediánů `range_5` −0,90 bp, 95% CI [−1,97; 0,16]; po minutách −0,46 bp
       [−1,52; 0,60]. Celá historie by kritérium splnila, ale srovnává mega caps z backfillu
       s ostatními ze živého sběru, tedy dvě období s různou volatilitou; uvnitř backfillu má CI
       nulu uvnitř. Skupinu „firmy“ tvoří jen `TECH`.
   - Odznak zásadní zprávy je stávající `is_key`.
   - **Skupina na kartě** je zobrazovací mapování stávající `category`. Slovník kategorií se
     nemění, protože je klíčem modelu a K1.

     | skupina | zdroj skupiny |
     |---|---|
     | makro data | `MACRO_INFLATION`, `MACRO_LABOR`, `MACRO_GROWTH` |
     | centrální banky | `FED` a zprávy zdroje `ecb` |
     | geopolitika | `GEOPOLITICS`, `ENERGY` |
     | firmy | `TECH` (`EARNINGS` jen v mapování; výjimka mega caps nesplnila kritérium) |
     | ostatní | `CRYPTO`, `OTHER` |

     „Výzkum bank“ jako skupina nevzniká. Analytické akce v2 shazuje na importance 1 a na
     kartu by se nedostaly. Výhledy stratégů bank pro index by byly nová kategorie
     klasifikátoru (varianta B níže); vlastník její záměr nepotvrdil.
   - **Téma** je jedna entita z jednoho slovníku: aktéři režimu z klasifikátoru (Írán, Izrael,
     Hormuz, Čína/Tchaj-wan, OPEC…), cla, Fed a další. Bere se první shoda podle pořadí ve
     slovníku a počítá se při čtení z titulku.
     - **Upřesnění E-6.27:** shoda se hledá nejdřív v předmětu titulku, pak v celém titulku,
       stejně jako kategorie (`classify_category`). „Oil jumps as Iran seizes tanker“ má téma
       ropa, ne Írán.
     - Slovník (`THEMES` v `breaking.py`) skládá jen vzory klasifikátoru v tomto pořadí:
       regionální průzkum Fedu jako růst, Fed, cla a obchodní dohody, aktéři režimu po jednom
       (`REGIME_ACTORS`, ze kterých vzniká `ACTORS`), energie, inflace, trh práce, růst,
       fiskální riziko USA (shutdown, dluhový strop, rating).
     - Identifikátor je `theme`, protože `topic` už v API znamená index kategorie
       (`topic_value`). Popisky témat a skupin v češtině dodá UI (E-6.28).
5. **Zdroje tier 1 (E-6.25) a jejich `ts_event`, kategorie a importance:**
   - Zdroje podle E-6.22: Fed testimony, BLS `empsit`/`cpi`/`ppi`/`jolts`/`eci`, BEA
     z `www.bea.gov/news/rss` (po ověření úplnosti proti kalendáři), ECB, White House ×2.
     Každá instituce má vlastní `source`, aby šlo měřit zpoždění po zdrojích. Hlídka feedů
     #1451 je pokryje.
   - **Kategorie a importance:**
     - **feedy s pevným obsahem** (Fed, BLS, BEA, ECB, SEC) je určují podle feedu a typu releasu,
       vzorem je `fed_rss` (`news-engine/src/gexlens_news/__main__.py:108-118`,
       `fed_rss_importance` v `classifier.py:727-729`): BLS `cpi`/`ppi` → `MACRO_INFLATION` 3,
       `empsit` → `MACRO_LABOR` 3, `jolts`/`eci` → `MACRO_LABOR` 2; BEA GDP → `MACRO_GROWTH` 3,
       PCE → `MACRO_INFLATION` 3, ostatní 2; ECB rozhodnutí o sazbách 2, ostatní 1
       (shodně s ADR-0045 bod 3); SEC 8-K Item 2.02 → `EARNINGS` 2, ostatní Items 1.
       JSON `data.sec.gov` titulek nemá a regex `EARNINGS` (`classifier.py:466-467`) by
       sestavený titulek formuláře nechytil;
     - **White House** má titulky ve tvaru headline, takže platí pravidla v2 nad titulkem.
   - **`ts_event` = okamžik zveřejnění,** původ se ukládá v `raw.ts_source` (`item`,
     `calendar`, `fetch`). Adaptér čas zveřejnění obecně nezná, proto pravidlo určuje zdroj:
     - **BLS:** čas položky se nepoužije nikdy, protože předbíhá release o 39 min.
       - Bere se čas releasu z kalendáře: řádek FF USD téhož dne v ET **podle mapování
         feed → titulek releasu**, ne libovolný řádek USD.
       - Mapování: `cpi` → „CPI m/m“, `ppi` → „PPI m/m“, `empsit` → „Non-Farm Employment
         Change“, `jolts` → „JOLTS Job Openings“, `eci` → „Employment Cost Index q/q“.
         Přesné titulky FF ověří E-6.25.
       - Bez shody se vezme čas prvního stažení.
     - **Ostatní zdroje:** čas položky, je-li věrohodný (rok ≥ 2000, ne v budoucnosti). Jinak
       čas prvního stažení.
   - **Pojistky (požadavky na E-6.25 s testy):**
     - čas BLS z kalendáře je stabilní, takže restart v jiný den nezmění `dedup_hash`
       a release se nezapíše znovu;
     - párování BLS nesmí vzít jiný řádek téhož dne: test na kolizi s „FOMC Member Speaks“
       v 8:00 a s PMI v 9:45;
     - s časem stažení se zapisuje jen položka, jejíž `(source, source_uid)` v DB ještě není
       (`ix_news_events_source_uid`, `storage/sentiment.py:133`);
     - položka bez věrohodného času, kterou feed nesl už v **prvním stažení po startu
       procesu**, se nezapíše, protože čas zveřejnění nejde doložit. Jinak by se historie
       feedu (např. testimony s rokem 1899) tvářila jako čerstvá zpráva a vstoupila do
       SentIndexu, shluků i souhrnu. Ztrátou je položka bez času, která přibyla během výpadku
       procesu; ta se zaloguje a započte do zdraví zdroje.
   - **Dopad na model a upozornění:** nové zprávy projdou klasifikací jako ostatní a vstoupí
     do SentIndexu, `news_model_stats` a gate, do shluků upozornění (ADR-0043) a do
     předobchodního souhrnu.
     - Položka BLS s časem z kalendáře má stejný `ts_event` a kategorii jako řádek FF, takže
       je K1 navzájem nekontaminuje (ADR-0045 bod 7).
     - Do jednoho vzorku je model nesloučí: položka BLS nemá `surprise_z` a padne do bucketu
       `none`, řádek FF do pos/neg/flat (`news-engine/src/gexlens_news/model_stats.py:106-115`).
       V témž agregátu se tedy nezapočte dvakrát, stejně jako dnes řádek Benzinga
       „USA CPI … Vs Est“.
     - Upozornění spustí jen zpráva s importance ≥ 2 (ADR-0043). Kolik položek White House
       to bude, změří E-6.29; snímek E-6.22 frekvenci neměří.
6. **SEC, Treasury, Finnhub, tier 2:**
   - **SEC = varianta B, ale jen spolu s výjimkou mega caps z bodu 4.** Když výjimka kritérium
     nesplní, SEC na kartu nic nedodá a platí varianta A (nepoužít).
     **Revize 9. 10.: výjimka kritérium nesplnila (bod 4), platí varianta A** — SEC se nepoužije
     a podmíněný návrh níže zůstává jen pro případ nového rozhodnutí.
     - Zdroj: `data.sec.gov/submissions/CIK{10}.json` po firmách, jen 8-K.
     - Seznam mega caps (CIK a symboly) je jedna konstanta: ~30 firem s největší vahou
       v S&P 500 a Nasdaq-100. Sdílí ji výjimka v bodě 4.
     - Polling à 60 s dává ~0,5 požadavku/s (limit SEC je 10/s). Zpoždění
       `fetched_at − acceptanceDateTime` změří provoz a pásmo `Z` se ověří (E-6.22, 2.1).
     - **Kritérium setrvání:** po 30 obchodních dnech provozu včetně sezóny výsledků se změří,
       kolik 8-K nemělo do 30 min před `acceptanceDateTime` zprávu tier 2 se stejným symbolem
       (podíl unikátních). Když je podíl nulový, zdroj se vypne. Měření dostane vlastní
       připomínku při nasazení E-6.25b.
   - **Treasury: zatím ne.**
   - **Finnhub general zůstává jako tier 3.**
   - **Tier 2 beze změny.** E-6.26 nemá co stavět.
7. **Kontakt pro User-Agent:**
   - Proměnná `GEXLENS_NEWS_UA_CONTACT` nahrazuje `GEXLENS_NEWS_SEC_CONTACT`.
   - Kontakt dostanou jen zdroje, které ho vyžadují (SEC, BLS; bez něj vrací 403). Ostatní zdroje
     dál dostávají `BROWSER_UA` (`news-engine/src/gexlens_news/http.py:22`).
   - Kontakt se převádí na ASCII.
   - Když proměnná chybí, zdroj se hlásí jako „nenakonfigurován“, ne jako chyba (#1457).
   - Přejmenování v `.env` udělá vlastník (připomínka). Sondu a adaptér upraví E-6.25.
8. **Karta „Breaking news“ (E-6.28), datový kontrakt podle rozhodnutí 8. 10. v #1385:**
   - **Výběr a čas:** zprávy s `is_breaking`. Čas je `ts_event`, k němu seznam zdrojů se
     zpožděním z `news_event_sources` viditelných v daném okamžiku.
   - **Dopad ES/NQ v bp od `ts_event`:**
     - hlavní číslo je změna ceny od posledního close před zprávou se znaménkem a barvou;
     - vedle něj je výchylka (high/low) podle ADR-0043, aby whipsaw nezmizel;
     - běží průběžně („běží X min“) a v 5. minutě se zafixuje.
   - **Odkud se dopad počítá:** hodnotu i fixaci počítá z minutových barů **tatáž čistá funkce**
     jako `news_reactions` (`compute_reactions`, `news-engine/src/gexlens_news/reactions.py:195`)
     a výchylka ADR-0043 (`measure_excursion`, `reactions.py:425`). Ze `news_reactions` se
     nečte, protože ten se zapisuje až po 60 min.
   - **Kde výpočet běží,** rozhodne E-6.28 s variantami; funkce se nesmí zkopírovat.
     - Push z news-enginu odpovídá pravidlu AGENTS „API jen čte storage, nepočítá“.
     - Výpočet v API by byl další výjimka z tohoto pravidla, i když precedens má
       (významnost při čtení, ADR-0045 bod 5).
   - **Zavřený trh:** místo dopadu se ukáže „trh zavřený“. Odložená reakce zůstává
     v `news_reactions`, karta ji nepočítá.
   - ⚠ označuje kontaminaci podle K1.
   - Karta nese štítek skupiny, štítek tématu a odznak `is_key`.
   - Skóre z textu se nezobrazuje (#740).

## Zvažované varianty

Kritéria volby podle AGENTS.md: rychlost, výkon, relevance dat.

**Přiřazení tierů (bod 1):**

| otázka | varianty | zvoleno |
|---|---|---|
| kurátorovaný Bluesky | **A tier 2**: vlastník autory vybírá kvůli zprávám a medián zpoždění je 2,6 s. B mimo tiery: autoři můžou psát i názory | A; názory odfiltruje `is_significant` |
| sociální sítě jako vlastní typ (audit 5) | **A NULL**: sociální obsah bez kurátora na kartu nepatří. B tier 4 „sociální“: rozšiřuje schéma 1/2/3 ze zadání | A |
| kalendář FF | **A NULL**: kalendář má vlastní markery, upozornění a `is_key`, releasy BLS a BEA přinese tier 1. B tier 1: karta by ukázala i ISM, UMich a další releasy mimo BLS/BEA, ale FF přepisuje `actual` bez času, takže čas hodnoty na kartě by nebyl doložený | A |

**Kde tier žije (bod 2):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — sloupec zapsaný při ingestu** (zvoleno) | levný SQL filtr pro kartu, report i heatmapu; vstupy nového řádku se nemění | migrace a backfill; dva zapisovatelé (news-engine, engine) musí volat tutéž funkci |
| B — čistá funkce při čtení | bez migrace; změna mapování platí hned i zpětně | čte `raw` JSON u každého řádku, který filtr propustí; SQL by uměl jen druhou kopii pravidla |
| C — sloupec v registru `news_sources` | nejjednodušší | nerozliší autory Alpacy ani kurátory Bluesky, takže 29 % Alpacy by mělo špatný tier |

**Záznam zdrojů (bod 3):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — tabulka jen s kopiemi z jiných zdrojů** (zvoleno) | minimum dat, první doručení se neduplikuje | dotaz „všechny zdroje“ = `news_events` ∪ `news_event_sources` |
| B — tabulka se všemi doručeními včetně prvního | jednotný dotaz | +0,8–1,1 mil. řádků za rok, které jen opakují `news_events` |
| C — opravit `raw.merged_sources` (UPDATE prvního eventu) | bez nové tabulky | mutuje surová data (#1027); JSON nejde rozumně dotazovat ani indexovat; žádný point-in-time |

**`is_breaking` (bod 4):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — při čtení z efektivního tieru a významnosti** (zvoleno) | jedna definice; reklasifikace i ruční korekce se projeví hned | SQL umí jen předfiltr (`content_tier` není NULL, `importance ≥ 2`), přesné pravidlo běží v Pythonu |
| B — uložený příznak s vlastním slovníkem (dnešní znění E-6.27) | rychlý SQL filtr | druhá definice „důležité zprávy“, tedy přesně ten rozjezd, který ADR-0045 odstranil; po každé reklasifikaci backfill |

**Zprávy tier 3 na kartě (bod 4, revize 9. 10., vlastník v chatu):**
- A — až po potvrzení kopií tier 1–2: karta bez neověřených článků, ale zpráva, kterou pomalejší
  zdroj přinese dřív než Benzinga Newsdesk, se na kartě objeví až s ním.
- **B — hned se štítkem „článek, zatím nepotvrzeno“** (zvoleno): trader vidí zprávu co nejdřív;
  cenou je šum, který měří `docs/research/1491-sum-karty-breaking.md` (varianty zpřísnění tam).

**Firmy na kartě (bod 4):**
- A — žádné: karta jen s makrem, centrálními bankami a geopolitikou. Skupina „firmy“ odpadne
  a SEC se nepoužije.
- **B — výjimka jen pro kartu: mega caps tier 1–2 s importance ≥ 2** (zvoleno, podmíněně
  měřením v E-6.27; **měření nesplnilo kritérium, výjimka nevznikla** — platí A).
  Významnost pro upozornění a model zůstane podle ADR-0045.
- C — zrušit vyřazení `EARNINGS` z významnosti: mění upozornění, gate a model a vrací šum
  výsledků malých firem, který #1291 odstranil.

**Výzkum bank (bod 4):**
- **A — skupina nevzniká** (zvoleno): analytické akce v2 shazuje na 1.
- B — nová kategorie výhledů stratégů bank pro index v klasifikátoru v3: mění buckety
  `news_model_stats` a K1 a vyžaduje reklasifikaci historie a retro přepočet SentIndexu
  (ADR-0036).
- C — vlastní slovník kategorií jen pro kartu: druhá cesta k témuž údaji, která se rozjede.

**Téma (bod 4):**

| otázka | varianty | zvoleno |
|---|---|---|
| uložené × při čtení | **A při čtení**: změna slovníku platí i pro historii bez backfillu, titulek se nemění. B uložený sloupec: rychlejší heatmapa, ale backfill po každé změně slovníku | A; heatmapa E-6.33 si výsledek cachuje |
| jedno × více na zprávu | **A jedno** (první shoda podle pořadí): heatmapa nepočítá jednu reakci ve více buňkách. B více: zpráva „Írán uzavírá Hormuz, ropa +5 %“ by měla obě témata, ale reakce by se započetla dvakrát | A |

**`ts_event` u BLS (bod 5):**
- A — čas prvního stažení:
  - dopad se měří až od stažení (perioda 60 s a zpoždění feedu), takže CPI a NFP na kartě
    přijdou o první skok;
  - restart v jiný den změní `dedup_hash` a release se zapíše znovu, pokud nepomůže kontrola
    `(source, source_uid)`.
- **B — čas releasu z kalendáře, záložně čas stažení** (zvoleno): přesný čas 8:30:00,
  stabilní hash a týž čas jako řádek FF. Cenou je mapování feed → titulek releasu FF
  a záloha, když kalendář chybí (`raw.ts_source = fetch`, viditelné).
- C — čas položky: look-ahead 39 min.

**Míra dopadu na kartě (bod 8):**
- A — jen změna ceny (close-to-close): jednoduché, ale FOMC 16. 9. by ukázal −0,3 bp místo
  výchylky −19 bp.
- B — jen výchylka (ADR-0043): zachytí whipsaw, ale neřekne, kam trh skončil.
- **C — změna ceny jako hlavní číslo a výchylka vedle** (zvoleno). Obojí dává tatáž data
  a tytéž funkce jako `news_reactions` a upozornění.
- Reakční index G1 (E-6.8) je normalizovaná řada pro model, ne syrový dopad pro kartu. Na kartu
  může přibýt, až vznikne.

**SEC (bod 6, E-6.22 2.4):**
- A — nepoužít: 8-K mega caps zůstanou jen přes Benzingu. Platí, pokud se nepřijme výjimka
  mega caps (bod 4).
- **B — `data.sec.gov` po CIK** (zvoleno, podmíněně výjimkou mega caps): robots.txt to neomezuje,
  JSON nese čas přijetí a filtr na firmy z indexu je přirozený. Cenou je dotaz na každou firmu
  a chybějící conditional GET.
- C — `getcurrent`: porušuje robots.txt, jen s výslovným rozhodnutím vlastníka.
- D — `/Archives/edgar/`: denní indexy, ne živý tok.

**Treasury (bod 6):**
- A — HTML výpis s `ts_event` = časem prvního stažení. Přidá oficiální oznámení (sankce,
  refunding), ale parsování HTML je křehké a změna šablony webu ho tiše rozbije.
- **B — zatím ne** (zvoleno). Titulky Treasury by šly přes pravidla v2 nad titulkem jako
  White House a v2 sankce shazuje na importance 1, takže na kartu by se nedostaly. Cla ohlašuje
  White House, který tier 1 mít bude. Treasury se vrátí na stůl, až se doloží případ, kdy
  oznámení Treasury pohnulo ES a žádný jiný zdroj ho do 5 min nepřinesl.

**Finnhub general (bod 6):**
- **A — ponechat jako tier 3** (zvoleno): jediný nese Reuters a Bloomberg, stojí 1 požadavek
  za minutu a z karty ho vyřadí tier.
- B — vypnout: ušetří 1 požadavek za minutu, ale ztratí se zprávy, které nikdo jiný nedodá.

**Kontakt pro User-Agent (bod 7):**
- **A — obecná proměnná jen pro zdroje, které kontakt vyžadují** (zvoleno).
- B — ponechat název `…_SEC_CONTACT` i pro BLS: zavádějící jméno.
- C — obecná proměnná posílaná všem zdrojům: osobní kontakt by zbytečně dostávaly další strany.

## Důsledky

- **Zadání navazujících úkolů v #1406 jsou upravená podle přijatých variant (9. 10.):**
  - **E-6.24a (nový):** funkce `content_tier`, sloupec a backfill. Předchází záznamu kopií,
    které tier potřebují.
  - **E-6.24b (dřív E-6.24):**
    - zápis kopií ze všech cest včetně `newsticks`, s `content_tier` kopie;
    - oprava docstringu `dedup.py:8-11` a poznámka u SPEC 3.3.
  - **E-6.25:**
    - pravidla `ts_event` a pojistky z bodu 5 s testy (restart v jiný den, první stažení
      po startu procesu, rok 1899, čas BLS z kalendáře, kolize s jiným řádkem FF téhož dne);
    - kategorie a importance podle feedu;
    - kontakt `GEXLENS_NEWS_UA_CONTACT`.
  - **E-6.25b (nový):** SEC podle bodu 6 s připomínkou kritéria setrvání. Jen když výjimka
    mega caps z E-6.27 splní kritérium, jinak se zavře. **Revize 9. 10.: zrušen** (kritérium
    nesplněno, #1491).
  - **E-6.26:** zrušen bez implementace s odkazem na #1474. Nový kandidát se ověří sondou
    `scripts/news_candidates_probe.py`.
  - **E-6.27:**
    - `is_breaking`, skupina a téma při čtení místo ukládání a bez vlastního slovníku;
    - vypadá kritérium „krátký headline“ (nahrazuje ho tier 2) a skupina „výzkum bank“;
    - předregistrované měření mega caps před výjimkou;
    - **revize 9. 10.:** tier 3 na kartě se štítkem „nepotvrzeno“, report šumu a varianty
      zpřísnění; výjimka mega caps nesplnila kritérium a nevznikla.
  - **E-6.28:**
    - změna ceny a výchylka z barů touž funkcí jako `news_reactions`, fixace v 5. minutě;
    - stav „trh zavřený“;
    - štítek skupiny a seznam zdrojů se zpožděním z `news_event_sources`;
    - štítek „článek, zatím nepotvrzeno“ u efektivního tieru 3 a popisky skupin a témat.
  - **E-6.29:** report za 24 h doplní denní objem `news_event_sources` a podíl zpráv, u kterých
    efektivní tier změnila kopie. Návrh přeměřeného `expected_daily_volume` zapíše jednorázový
    skript, protože PATCH registru umí jen `enabled` a seed je insert-if-missing
    (`storage/sentiment.py:152-187`).
- **Nové perzistentní řady:**
  - `news_events.content_tier`, ~2 MB za rok;
  - `news_event_sources`, hrubý odhad ~140 MB za rok; skutečnost změří E-6.29.
- **Co přestává platit:** bod 2 ADR-0016 a v sentiment SPEC 3.3 věta o `raw.merged_sources`
  i slib „`raw` uchová všechny payloady“. Platí bod 3 tohoto ADR. Kód s ním bude v souladu
  až po E-6.24b.
- **Sentiment SPEC kap. 1:** Tier A (BLS a BEA API pro hodnoty releasů) se tímto ADR nemění
  ani neplní. Tier B: Finnhub přestává být hlavním headline zdrojem.
- **Krok vlastníka:** přejmenovat v `.env` proměnnou `GEXLENS_NEWS_SEC_CONTACT` na
  `GEXLENS_NEWS_UA_CONTACT` při nasazení E-6.25 (připomínka #1485).
- **Počet IBKR market data lines se nemění** (žádná nová páska).
