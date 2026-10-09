# ADR-0059: Breaking news — tier obsahu, záznam všech zdrojů u duplicit, `is_breaking` a téma, nové oficiální zdroje

- **Stav:** navrženo (PR s labelem `needs-decision`)
- **Datum:** 2026-10-09
- **Souvisí:** #1482 (E-6.23), #1406 (Fáze 6), #1385 (Rozhodnuto 8. 10. — breaking news), audit
  zdrojů #1473 (`docs/research/1473-audit-zdroju-zprav.md`), test kandidátů #1474
  (`docs/research/1474-kandidati-zdroju-zprav.md`), #1457 (kontakt pro SEC), #1338 (objem řad),
  #740 (směr z textu bez edge), ADR-0016 (**bod 2 nahrazuje**), ADR-0043 (shluky), ADR-0045
  (klasifikace v2, jeden zdroj významnosti), sentiment SPEC kap. 1 a 3.3

## Kontext

Zadání vlastníka 8. 10. 2026 (#1385): karta „Breaking news“ s naměřeným dopadem na ES/NQ, nové
zdroje zpráv, deduplikace se záznamem všech zdrojů, `is_breaking`, kategorie a téma. Audit
stávajících zdrojů (E-6.21) a test kandidátů (E-6.22) daly tato tvrdá data:

1. **Rychlé zdroje jsou dnes dva:** Alpaca (Benzinga Newsdesk, push, medián ≈ 0 s) a Bluesky
   (2,6–5,1 s). Oficiální zdroj je jediný, `fed_rss` se 2 feedy a ~0,4 zprávy denně. Finnhub
   general nese Reuters a Bloomberg s mediánem zpoždění 8,3 h (audit, oddíl 1 a 2.2).
2. **`raw.merged_sources` se do DB nikdy nedostane** (0 záznamů v celé historii, audit 2.5).
   Sloučení se ukládá jen k eventu ze stejné dávky (`news-engine/src/gexlens_news/pipeline.py:44-58`),
   každý collector ale zapisuje vlastní dávku (`runner.py:78`). Čítače `merged_total`
   a `duplicates_total` (`pipeline.py:32-33`) nikdo nečte. IBKR pásky zapisuje engine mimo
   deduplikaci, jen přes unikátní `dedup_hash` (`engine/src/gexlens_engine/ibkr/newsticks.py:344-353`).
   Bod 2 ADR-0016 a sentiment SPEC 3.3 tedy slibují něco, co se neděje. Kdo byl první, se z DB
   určit nedá (audit 3.1).
3. **„Tier“ už v projektu znamená čtyři věci:** Tier A–D v sentiment SPEC kap. 1 (rodina zdrojů),
   `news_sources.tier` = role zdroje `core`/`extra`/`test` (`engine/src/gexlens_engine/storage/sentiment.py:141-150`,
   zobrazuje ho panel Zdroje), `significance_tier` 0–3 (ADR-0045 bod 5) a „US data tier 1/2“
   v klasifikátoru. Pátý význam potřebuje vlastní identifikátor.
4. **Tier obsahu se nedá určit jen podle zdroje.** Alpaca nese z 71 % headliny Benzinga Newsdesku,
   zbytek jsou šablonové články Insights a články autorů (audit, oddíl 1; autor je v `raw.author`,
   `collectors/alpaca.py:79`). Bluesky rozlišuje kurátorované autory (`raw.curated`,
   `feed_of` v `classifier.py:122-136`).
5. **Významnost má jeden zdroj** (ADR-0045 bod 5, `compute/news_significance.py:40-60`).
   Importance zapisují čtyři cesty (pravidlový job, LLM, ruční korekce, reklasifikace), takže
   ADR-0045 odmítl cokoli z ní odvozeného ukládat. Zásadní zpráva (`is_key`) už existuje.
6. **Kategorie `NEWS_CATEGORIES`** (`storage/sentiment.py:77-88`) jsou klíčem bucketu
   empirického modelu (`news_model_stats`, `storage/sentiment.py:405-420`) a pravidla kontaminace
   K1 (ADR-0045 bod 7). Analytické akce („price target“) klasifikátor v2 záměrně shazuje na
   importance 1 (`classifier.py:151`, ADR-0045 bod 1).
7. **Kandidáti (E-6.22, oddíl 1):** nové zdroje tier 1 jsou Fed testimony, BLS ×5, BEA (www), ECB
   a White House ×2. Všechny jsou zdarma, bez klíče a robots.txt je povoluje. SEC `getcurrent`
   robots.txt zakazuje (`/cgi-bin`), `data.sec.gov` povoluje. Treasury GovDelivery robots.txt
   zakazuje; HTML výpis povoluje, ale má čas zaokrouhlený na čtvrthodiny. Tier 2 nemá nový
   použitelný feed (FinancialJuice brání podmínky, Alpha Vantage limit 25 dotazů denně) a IBKR
   nemá nic nového, takže se počet market data lines nemění.
8. **Čas položky není vždy čas zveřejnění:** BLS má čas položky 39 min před releasem a 3 z 15
   položek Fed testimony mají rok 1899 (E-6.22, 2.2). `RssCollector` bere
   `ts_event = published or fetched_at` (`collectors/rss.py:256-266`). Převzatý čas BLS by byl
   look-ahead: reakce by se měřila od okamžiku, kdy o zprávě nikdo nevěděl.

## Rozhodnutí

Návrh. Body 1, 4, 5, 6 a 7 předkládá vlastníkovi k rozhodnutí PR. Varianty jsou v oddílu
„Zvažované varianty“.

1. **Tier obsahu = `content_tier`** (1 oficiální zdroj, 2 headline feed, 3 článek nebo analýza,
   NULL = mimo tiery). V UI se zobrazuje jako „Tier 1/2/3“. Identifikátor `content_tier` se
   nepoplete s rolí zdroje ani se stupněm významnosti.

   | `content_tier` | zdroj a podskupina |
   |---|---|
   | 1 | `fed_rss` (včetně testimony), nové `bls`, `bea`, `ecb`, `whitehouse` a `sec` podle bodu 5 |
   | 2 | `alpaca` s autorem Benzinga Newsdesk; `bluesky` s kurátorovaným autorem (`raw.curated`) |
   | 3 | `alpaca` ostatní autoři (Insights, články); `rss_news` (CNBC, MarketWatch, Yahoo); `finnhub`; `ibkr_brfg`; `ibkr_djnl`; `rss_user` |
   | NULL | `forexfactory` (kalendář má vlastní cestu a `is_key`); `bluesky` bez kurátora; `reddit_rss` |

2. **Kde tier žije:** ve sloupci `news_events.content_tier SMALLINT NULL`. Zapisuje ho při ingestu
   jedna čistá funkce `content_tier(source, raw)` v `engine/compute/` vedle `news_significance`.
   Volá ji zápis news-enginu i zápis IBKR pásek v enginu. Historii doplní jednorázový idempotentní
   backfill. Vstupy (zdroj, autor, příznak kurátora, feed) se po zápisu nemění, takže uložená
   hodnota se nerozjede. U importance to neplatí, proto ADR-0045 její odvozeniny neukládá.
   Registr `news_sources.tier` (role zdroje) zůstává beze změny.
   - **Objem (#1338):** sloupec na stávajících řádcích, ~2 200 zpráv denně (audit 2.1, okno 7 d
     včetně kalendáře) ≈ 0,8 mil. řádků za rok × 2 B ≈ **2 MB za rok**. Index se nezakládá:
     karta i report čtou úzké časové okno přes `ix_news_events_ts`.
3. **Záznam všech zdrojů u duplicit (nahrazuje bod 2 ADR-0016 a větu o `raw.merged_sources`
   v sentiment SPEC 3.3).** Nová tabulka `news_event_sources`:

   | sloupec | význam |
   |---|---|
   | `event_id` | FK na `news_events.id`, tedy na první doručení |
   | `source` | zdroj kopie |
   | `source_uid` | id položky u zdroje, pokud ho má |
   | `published_at` | čas publikace podle zdroje kopie, se stejnými pravidly jako `ts_event` (bod 5) |
   | `fetched_at` | kdy kopie přišla |

   Primární klíč je `(event_id, source)`, takže zápis je idempotentní.
   - Řádek vzniká pro každou kopii z **jiného** zdroje, kterou zahodí rolling dedup (exaktní
     i Jaccard), nebo unikátní `dedup_hash`, včetně IBKR pásek z enginu. Kopie z téhož zdroje
     zůstává duplicitou bez záznamu, beze změny proti dnešku.
   - První doručení zůstává jen v `news_events` (`source`, `ts_event`, `ts_ingested`) a do
     tabulky se nekopíruje.
   - **`ts_event` se nepřepisuje**, protože od něj se měří reakce. Nejdřívější publikace je
     odvozená hodnota `min(ts_event, min(published_at))` a počítá se při čtení.
   - Tabulka je aditivní, nic se nemaže. Mrtvou cestu `raw.merged_sources`, nečtené čítače
     a test dávky ze dvou zdrojů (`news-engine/tests/test_dedup.py:308`) odstraní E-6.24.
   - Dvě úrovně se nepletou: `news_event_sources` je **táž zpráva** z více zdrojů, shluk
     (ADR-0043, E-6.6, E-6.17) jsou **různé zprávy** o téže události.
   - **Objem (#1338):** počet kopií dnes nikdo neměří (čítače se nečtou). Horní odhad je jedna
     kopie na každou zprávu: 0,8 mil. řádků × ~130 B (řádek ~100 B + PK ~30 B) ≈
     **100 MB za rok**. Skutečný objem změří E-6.29.
4. **`is_breaking`, skupina a téma se počítají při čtení, nic z toho se neukládá:**
   - `is_breaking = content_tier ∈ {1, 2} ∧ is_significant(kind, importance, category)`
     (ADR-0045 bod 5). Vlastní slovník klíčových slov nevzniká. Fed, CPI, NFP a cla už jsou
     spouštěče v2 (`EVENT_KINDS`, `classifier.py:536-538`). Chybějící téma (např. stres bank)
     se doplní do klasifikátoru jako nová verze s golden testem a `is_breaking` ho převezme.
   - Odznak zásadní zprávy na kartě je stávající `is_key`.
   - Importance zpráv tier 1 určuje feed, ne regex titulku, stejně jako `fed_rss_importance`
     (`classifier.py:721-723`, ADR-0045 bod 4). Titulky oficiálních feedů nemají tvar headline,
     na kterém pravidla v2 stojí.
   - **Skupina na kartě** je jen zobrazovací mapování stávající `category`:

     | skupina | kategorie |
     |---|---|
     | makro data | `MACRO_INFLATION`, `MACRO_LABOR`, `MACRO_GROWTH` |
     | centrální banky | `FED` a zpráva tier 1 ze zdroje `ecb` |
     | geopolitika | `GEOPOLITICS`, `ENERGY` |
     | firmy | `EARNINGS`, `TECH` |
     | ostatní | `CRYPTO`, `OTHER` |

     Slovník kategorií se nemění, protože je klíčem modelu a K1. Skupina „výzkum bank“ nevzniká:
     analytické akce v2 shazuje na importance 1, na kartu by se tedy nedostaly.
   - **Téma** je jedna entita z jednoho slovníku: aktéři režimu z klasifikátoru (Írán, Izrael,
     Hormuz, Čína/Tchaj-wan, OPEC…), cla, Fed a další. Bere se první shoda podle pořadí ve
     slovníku. Počítá se při čtení z titulku, takže změna slovníku platí i pro historii bez
     backfillu. Zpráva má jedno téma, aby heatmapa E-6.33 nepočítala jednu reakci ve více
     buňkách.
5. **Zdroje (E-6.25):**
   - **Tier 1** podle E-6.22: Fed testimony, BLS `empsit`/`cpi`/`ppi`/`jolts`/`eci`, BEA
     z `www.bea.gov/news/rss` (po ověření úplnosti proti kalendáři), ECB a White House ×2.
     Každá instituce má vlastní `source`, stejně jako dnes `fed_rss`, aby šlo měřit, kdo byl
     první. Hlídka feedů #1451 je pokryje.
   - **`ts_event` = okamžik zveřejnění.** Čas položky platí, jen když není před zveřejněním ani
     mimo rozsah. Jinak se bere čas prvního stažení:
     - BLS: čas prvního stažení. Přesný čas releasu nese řádek kalendáře, od kterého se měří
       reakce, a K1 je navzájem nekontaminuje, protože mají stejnou kategorii.
     - Fed testimony s rokem 1899: čas stažení.
   - **SEC = varianta B:** `data.sec.gov/submissions/CIK{10}.json` po firmách, jen 8-K.
     - Seznam CIK je konstanta: ~30 firem s největší vahou v S&P 500 a Nasdaq-100.
     - Polling à 60 s dává ~0,5 požadavku/s (limit SEC je 10/s).
     - Zpoždění `fetched_at − acceptanceDateTime` změří provoz a pásmo `Z` se ověří (E-6.22, 2.1).
     - **Kritérium setrvání:** po sezóně výsledků za Q3 2026 report E-6.29 ukáže, u kolika 8-K
       byl SEC první nebo jediný zdroj. Při nule se zdroj vypne.
   - **Treasury: zatím ne.**
   - **Finnhub general zůstává jako tier 3.**
   - **Tier 2 beze změny.** E-6.26 nemá co stavět.
6. **Kontakt pro User-Agent:**
   - Proměnná `GEXLENS_NEWS_UA_CONTACT` nahrazuje `GEXLENS_NEWS_SEC_CONTACT`.
   - Kontakt dostanou jen zdroje, které ho vyžadují (SEC, BLS; bez něj vrací 403). Ostatní zdroje
     dál dostávají `BROWSER_UA`.
   - Kontakt se převádí na ASCII.
   - Když proměnná chybí, zdroj se hlásí jako „nenakonfigurován“, ne jako chyba (#1457).
   - Přejmenování v `.env` udělá vlastník (připomínka). Sondu a adaptér upraví E-6.25.
7. **Karta „Breaking news“ (E-6.28), datový kontrakt podle rozhodnutí 8. 10. v #1385:**
   - zobrazuje zprávy s `is_breaking`;
   - čas je `ts_event`, k němu seznam zdrojů se zpožděním z `news_event_sources`;
   - dopad ES/NQ v bp od `ts_event` běží průběžně („běží X min“) a po 5 min se zafixuje
     na okno 5 z `news_reactions`;
   - ⚠ označuje kontaminaci podle K1;
   - nese štítek skupiny, štítek tématu a odznak `is_key`;
   - skóre z textu nezobrazuje (#740).

## Zvažované varianty

Doporučení se řídí kritérii AGENTS.md: rychlost, výkon, relevance dat.

**Kde tier žije (bod 2):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — sloupec zapsaný při ingestu** (doporučeno) | levný SQL filtr pro kartu, report i heatmapu; vstupy se nemění, takže se hodnota nerozjede | migrace a backfill; dva zapisovatelé (news-engine, engine) musí volat tutéž funkci |
| B — čistá funkce při čtení | bez migrace; změna mapování platí hned i zpětně | čte `raw` JSON u každého řádku; filtr v SQL by byl druhá kopie pravidla; heatmapa za 90 dní by četla ~200 tis. `raw` |
| C — sloupec v registru `news_sources` | nejjednodušší, editovatelné v panelu Zdroje | nerozliší autory Alpacy ani kurátory Bluesky, takže 29 % Alpacy by mělo špatný tier |

**Záznam zdrojů (bod 3):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — tabulka jen s kopiemi z jiných zdrojů** (doporučeno) | minimum dat, první doručení se neduplikuje | dotaz „všechny zdroje“ = `news_events` ∪ `news_event_sources` |
| B — tabulka se všemi doručeními včetně prvního | jednotný dotaz | +0,8 mil. řádků za rok, které jen opakují `news_events` |
| C — opravit `raw.merged_sources` (UPDATE prvního eventu) | bez nové tabulky | mutuje surová data (#1027), JSON nejde rozumně dotazovat ani indexovat |

**`is_breaking` (bod 4):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — při čtení z `content_tier` a významnosti** (doporučeno) | jedna definice; reklasifikace i ruční korekce se projeví hned | SQL umí jen předfiltr (`content_tier ≤ 2`, `importance ≥ 2`), přesné pravidlo běží v Pythonu |
| B — uložený příznak s vlastním slovníkem (dnešní znění E-6.27) | rychlý SQL filtr | druhá definice „důležité zprávy“, tedy přesně ten rozjezd, který ADR-0045 odstranil; po každé reklasifikaci backfill |

**Kategorie (bod 4):**

| varianta | výhody | nevýhody |
|---|---|---|
| **A — zobrazovací mapování stávající `category`** (doporučeno) | žádný zásah do modelu, K1 ani SentIndexu | skupina „výzkum bank“ chybí |
| B — nová kategorie analytických akcí v klasifikátoru v3 | karta ukáže výzkum bank | mění buckety `news_model_stats` a K1; reklasifikace historie a retro přepočet SentIndexu (ADR-0036); analytické akce mají dnes importance 1 záměrně |
| C — vlastní slovník kategorií karty | volnost | druhá cesta k témuž údaji, která se rozjede |

**SEC (bod 5, E-6.22 2.4):**
- A — nepoužít: 8-K mega caps zůstanou jen přes Benzingu.
- **B — `data.sec.gov` po CIK** (doporučeno): robots.txt to povoluje, JSON nese čas přijetí
  a filtr na firmy z indexu je přirozený. Cena je dotaz na každou firmu a chybějící conditional GET.
- C — `getcurrent`: porušuje robots.txt, jen s výslovným rozhodnutím vlastníka.
- D — `/Archives/edgar/`: denní indexy, ne živý tok.

**Treasury (bod 5):**
- A — HTML výpis s `ts_event` = časem prvního stažení. Přidá oficiální oznámení (sankce,
  refunding), ale parsování HTML je křehké a změna šablony webu ho tiše rozbije.
- **B — zatím ne** (doporučeno). Klasifikátor v2 shazuje sankce na importance 1, takže na kartu
  by se nedostaly. Cla ohlašuje White House a ten tier 1 bude mít. Treasury se vrátí na stůl,
  až se doloží případ, kdy oznámení Treasury pohnulo ES a žádný jiný zdroj ho do 5 min nepřinesl.

**Finnhub general (bod 5):**
- **A — ponechat jako tier 3** (doporučeno). Jediný nese Reuters a Bloomberg, stojí 1 požadavek
  za minutu a z karty ho vyřadí tier.
- B — vypnout. Ušetří 1 požadavek za minutu, ale ztratí se zprávy, které dnes nikdo jiný nedodá.

**Kontakt pro User-Agent (bod 6):**
- **A — obecná proměnná jen pro zdroje, které kontakt vyžadují** (doporučeno).
- B — ponechat název `…_SEC_CONTACT` i pro BLS: zavádějící jméno.
- C — obecná proměnná posílaná všem zdrojům: osobní kontakt by zbytečně dostávaly další strany.

**`ts_event` u BLS (bod 5):**
- **A — čas prvního stažení** (doporučeno): poctivý point-in-time a žádná vazba na kalendář.
- B — čas releasu z kalendáře: přesný čas 8:30:00, ale párování položky na řádek kalendáře
  a závislost na jeho úplnosti.
- C — čas položky: look-ahead 39 min.

## Důsledky

- **Zadání navazujících úkolů v #1406 se upraví podle ADR:**
  - E-6.24: zápis kopií ze všech cest včetně `newsticks`, oprava docstringu `dedup.py:8-11`.
  - E-6.27: `is_breaking` počítat při čtení místo ukládání a z tieru a významnosti místo
    vlastního slovníku; `content_tier` jako sloupec s backfillem; skupina jako mapování.
  - E-6.25: pravidla pro `ts_event`, SEC varianta B s kritériem setrvání, kontakt
    `GEXLENS_NEWS_UA_CONTACT`.
  - E-6.26: zavřít bez implementace s odkazem na #1474 (nový kandidát se ověří sondou
    `scripts/news_candidates_probe.py`).
- **Nové perzistentní řady:** `news_events.content_tier` (~2 MB za rok) a `news_event_sources`
  (≤ ~100 MB za rok, skutečnost změří E-6.29).
- **Bod 2 ADR-0016 a věta o `raw.merged_sources` v sentiment SPEC 3.3 neplatí**, platí bod 3
  tohoto ADR. Kód je s ním v souladu až po E-6.24.
- **Registr `news_sources`:** nové zdroje dostanou řádek v `NEWS_SOURCE_SEED` s objemem podle
  E-6.22. Přeměřený `expected_daily_volume` stávajících zdrojů navrhne E-6.29 a zapíše se přes
  panel Zdroje. Role `core`/`extra`/`test` se nemění.
- **Krok vlastníka:** přejmenovat v `.env` proměnnou `GEXLENS_NEWS_SEC_CONTACT` na
  `GEXLENS_NEWS_UA_CONTACT` (připomínka issue s `prio:*` po přijetí).
- **Počet IBKR market data lines se nemění** (žádná nová páska).
