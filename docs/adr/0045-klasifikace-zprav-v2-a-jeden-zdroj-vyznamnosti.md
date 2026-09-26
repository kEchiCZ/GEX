# ADR-0045: Pravidlová klasifikace zpráv v2, jeden zdroj významnosti a kontaminace jen jinou kategorií

- Stav: **přijato** (26. 9. 2026). Rozhodnutí uživatele 26. 9. (komentář v #1293): agregátory jen
  události, kalendář významný jen USD High/Medium a rozhodnutí ECB/BoE/BoJ, reklasifikace historie
  hned, zásadní zpráva = kalendář nebo zpráva s importance 3, kontaminace reakcí varianta K1;
  k #1305 varianta A (čistá funkce, výpočet při čtení). Body 9 a 10 a kurátoři v bodě 8
  doplněny 26. 9. po recenzi; **uživatel je 26. 9. potvrdil** (včetně důsledku, že se gate
  intradenních signálů ze zpráv zavře, dokud data neprokážou efekt).
- Souvisí: #1293, #1305, #1291 a ADR-0043 (upozornění na reakci trhu, předobchodní souhrn),
  #1290 (markery po seancích, proklik ze zvonečku), #1296 a ADR-0044 (upozornění před releasem —
  čte FF impact, ne importance), ADR-0042 a #1265/#1267 (gate signálů), ADR-0036 (retro přepočet
  SentIndexu), ADR-0037 a #565 (epizody sentimentu), #578 (registr zdrojů), #740 (ngram), #1264
  (vzorek H1), SPEC 5.1 (kontaminace), S11 (verze klasifikace).

## Kontext

Analýza #1293 (400 zpráv z 14 dní, dva hodnotitelé, shoda κ = 0,81; holdout 200 zpráv z jiného
týdne hodnocený naslepo):

- **Pravidlový klasifikátor v1 s lidmi nesouhlasil** (κ ≈ 0,05 mezi importance a relevancí pro
  ES/NQ). Importance 3 byla zásadní u 9 % zpráv ve vzorku (asi 2 % v populaci).
- **Hlavní vzorec chyb je zmínka místo předmětu** (53 % falešně důležitých zpráv): „payroll
  tax“ v osobních financích, „tariff refund“ u drobné firmy, „rate decision“ v textu o kryptu.
  Dalších 36 % mělo předmět, ale pro ES/NQ bezvýznamný (cizí data, rutinní sankce, názor)
  a 10 % byly falešné shody regexu (`opec` v „alopecia“, `cpi` v doméně `icc-cpi.int`,
  Colin Powell, „state of emergency“ kvůli počasí, tug-of-war).
- **Zdroj rozhoduje víc než slovník**: sociální sítě bez kurátora tvořily 69 % falešně důležitých
  zpráv v populaci; Yahoo `rssindex` (se syndikací WSJ, Barron's, IBD) měl u importance 3
  zásadních 0 z 18. Registr zdrojů (#578) je nerozliší — Yahoo i CNBC jsou `rss_news`, `core`.
- **Úplnost**: 86 % titulků o ropě a Hormuzu mělo importance 1; řádky releasů Benzinga
  („USA … For August … Vs … Est“) importance 3 neměly nikdy; job přepisoval `fed_rss` (collector
  zapisuje FED/3) na OTHER/1.
- **Kalendář FF měl importance z regexu nad titulkem** („USD PPI m/m“ High → 1, „FOMC Member
  Speaks“ Low → 3). Upozornění proto bralo kalendář podle `raw.impact` a graf („Významné“ =
  importance ≥ 2) podle importance — **dvě definice téhož**. 63 z 96 „významných“ událostí
  kalendáře za 28 dní byly cizí měny.
- Filtr grafu podle `importance ≥ 2` propouštěl 1 000–1 250 markerů denně při „Vše“ a asi 400
  při „Významné“; řazení v dialogu „importance 3 nad 2“ skoro nic neznamenalo.

## Rozhodnutí

1. **Klasifikátor v2** (`news-engine/src/gexlens_news/classifier.py`, čisté funkce, směr beze změny):
   - Importance i kategorie **jen z titulku** očištěného od HTML, odkazů, autora Redditu
     a přípony agentury; shrnutí nerozhoduje (spouštěč jen ve shrnutí byl relevantní ve 14 %).
   - **Importance 3 jen pro vzory UDÁLOSTI**: akce Fedu (rozhodnutí o sazbách bez modálního
     slovesa, statement, minutes, projekce, předseda Fedu mluví), release US dat tier 1 v tvaru
     releasu (číslo, konsenzus, sloveso výsledku; revize 2), pohyb ropy, eskalace či deeskalace
     s aktérem režimu (Írán, Izrael, Saúdové, Húsíové, Hormuz, Čína/Tchaj-wan, OPEC; uzavření
     jen u průlivu, hranice, přístavu či zařízení), akce s cly, snížení ratingu USA.
   - **Importance 2 = téma jako předmět**: Fed a jeho mluvčí, US data tier 2 a řádky releasů,
     rozhodnutí ECB/BoJ/BoE, výnosy Treasuries, inflace/práce/růst jako předmět, nabídka ropy,
     silná válečná slova, cla, obchodní dohoda, shutdown a dluhový strop, earnings.
   - **Předmět vs. zmínka**: spouštěč jen za spojkou („as/after/ahead of …“) platí do úrovně
     předmětu; souhrn amerického trhu smí nést driver ze zmínky jako 2.
   - **→ 1**: osobní finance, stock picking, přepisy earnings calls, šablony a rubriky (podcast,
     poll, week ahead…), sport, clickbait, studenti a absolventi, sankce, menší cizí centrální
     banky, cizí data, titulek začínající FX, zlatem nebo kryptem. Názor a otázka shodí téma
     na 1 a událost na 2; výhled („Preview“, „if Fed hikes“), otázka s pomocným slovesem na začátku
     a „Reported Earlier“/ICYMI nejvýš 2.
   - Kategorie z předmětu, pak z celého titulku; **ENERGY před GEOPOLITICS** (pohyb ropy
     způsobený válkou je ENERGY); `\bfed\b`, FOMC, „interest rate decision“ a Jackson Hole
     jsou FED, holé „powell“ ne; mzdy z reportu o zaměstnanosti (Average Hourly Earnings,
     Employment Cost Index) jsou MACRO_LABOR, ne EARNINGS. Slovník režimu (aktéři, jméno
     předsedy Fedu) je jedna konstanta s pokynem k údržbě v docstringu.
   - Opravy z recenze (26. 9.): měsíc „May“ není modální sloveso (release „For May“ nesmí
     spadnout na 2); cenová úroveň nebo body před „as stocks“ nejsou stock picking;
     typografické apostrofy se normalizují; importance 3 nedostane firma „CPI Card Group“,
     emise akcií, Redbook, „payroll“ firma, ropné akcie a firmy, zpracování ropy, jedlé oleje,
     souhrn cizí burzy i bez spojky („Indian shares … on …“ → 1), bývalý předseda či kandidát
     na předsedu Fedu, sázka trhu na akci Fedu („Traders bet Fed cuts…“) ani výhled členů FOMC.
2. **Strop podle feedu** (`feed_of(source, raw)`): sociální sítě bez kurátora (`raw.curated`)
   a Reddit nejvýš 1; **agregátory jen události** — Yahoo `rssindex` (URL feedu `raw.feed`,
   syndikace WSJ/Barron's/IBD se pozná podle feedu, ne podle odkazu), MarketWatch a uživatelské
   RSS: 3 → 2, 2 → 1. Ostatní feedy (CNBC RSS, Alpaca, Finnhub, Briefing, kurátor, Fed RSS) bez
   stropu. Strop mění jen importance, ne směr ani skóre.
3. **Kalendář FF podle měny** (`scheduled_importance`): USD a „All“ High 3, Medium 2, jinak 1;
   EUR/GBP/JPY jen rozhodnutí centrální banky s High (sazba, statement/summary/report, tisková
   konference, outlook report BoJ) 2; ostatní měny 1. Tutéž funkci volá collector, backfill
   (`ffhistory`) i klasifikační job — kalendář se regexem nepřepisuje. FF impact job čte ze
   surového payloadu (`raw.impact`, backfill `raw.impactName`).
4. **Fed RSS**: statement, projekce a minutes FOMC FED/3, ostatní (projevy) FED/2.
5. **Jeden zdroj významnosti** — čistá funkce
   `engine/src/gexlens_engine/compute/news_significance.py` (varianta A), vstupy
   `(kind, importance, category)`:
   - `significance_tier`: 0 kalendář importance 3, 1 kalendář importance 2, 2 zpráva
     importance 3, 3 zpráva importance 2 mimo EARNINGS, None nevýznamná;
   - `is_significant` = stupeň není None; `is_key` (zásadní) = stupeň ≤ 2, tedy **kalendář nebo
     zpráva s importance 3** — výčet kategorií odpadl, ropa je zásadní, kurátor jen s importance 3.
   - Volají ji `clusters.py` (shluky upozornění), `preopen.py` (předobchodní souhrn), WS dávka
     pravidlového i LLM jobu a API (`/news/markers` i s `ids`, `/news`, `/news/upcoming`), které
     přikládá pole `significance`. Počítá se při čtení — importance zapisují čtyři cesty
     (pravidlový job, LLM, ruční korekce, reklasifikace) a uložený sloupec by se rozjel.
   - `ClusterEvent` ztratil `ff_impact` a `curated`; `anomaly_job` je nečte.
6. **Graf (#1305)**: filtr „Významné“ = `significance != null` nebo připnuté id z prokliku
   zvonečku; výchozí stav „Významné“ pod novým klíčem `newsMarkerFilter.v2` (uložené „Vše“ ho
   nepřebije), volba „Vše“ zůstává. Proklik zobrazí marker i nevýznamné zprávy upozornění
   (upozornění z doby před reklasifikací) a posune graf jako dosud. Dialog markeru řadí podle
   stupně (kalendář High → Medium → importance 3 → 2), pak nevýznamné podle importance, v rámci
   stupně čas; sbalují se nevýznamné. Frontend kopii pravidel nemá.
7. **Kontaminace K1** (`reactions.contaminates`): okno reakce kontaminuje jen jiný event
   s importance ≥ 2 a **jinou kategorií** (neznámá kategorie kontaminuje). Pokrytí téže události
   (řádek Benzinga „USA CPI … Vs Est“ minutu po kalendáři CPI) by jinak vyřadilo 92 % reakcí na
   USD High releasy z modelu.
8. **Reklasifikace historie hned** (`scripts/reclassify_news_rules.py`, ADMIN kap. 12): jen
   eventy s `sentiment_source = rule` (LLM, ruční korekce ani stín ngram se nemění); nová verze
   v `news_classifications` (max+1 přes všechny zdroje, směr a síla z poslední pravidlové verze)
   jen při rozdílu, druhý běh 0 změn; přepočet `cont_1/5/15/60` pravidlem K1 (u deferred reakcí
   od prvního obchodovaného baru); dry-run jen čtením; rollback novou verzí. Potom restart
   news-engine (přepočet `news_model_stats` a gate) a retro přepočet SentIndexu.
   **Kurátoři pro historii**: příznak `raw.curated` existuje až od #1291 (25. 9.), starší post
   je kurátorovaný, když jeho autor (`raw.did`) má post s příznakem nebo je v aktuálním
   seznamu kurátorů (`settings` + env, handly přes resolveHandle jako collector) —
   jinak by strop 1 padl i na historické posty kurátorů (z 32 763 postů Bluesky nese
   příznak jen 197 z doby od 25. 9.) (varianty: A autor
   v aktuálním seznamu — zvoleno jako doporučení; B jednorázově doplnit `raw.curated` do
   historie — totéž s trvalým zápisem do surových dat; C přijmout strop 1).
9. **Jedno měření = jeden vzorek** (`model_stats`, doporučení recenze): vzorky téhož bucketu
   se stejným `ts_event` se v `news_model_stats` slučují (směr = převaha, remíza se
   neposuzuje, výnos je u souběžných eventů shodný). Řádky FF jednoho releasu (CPI m/m,
   Core CPI m/m, y/y — např. 12. 3. 2025 čtyři řádky s týmž výnosem 26,7 bp) jinak nafukují
   `n` a gate by se otevřel na pseudoreplikacích (K1 je navíc přestal vyřazovat navzájem).
   Varianty: A sloučení v agregaci (zvoleno — čistá změna s testem, job řadí stream podle
   času); B sloučení už při měření reakcí (mění `news_reactions` a všechny konzumenty);
   C přijmout a zdokumentovat.
10. **Váhy podle času zprávy** (`PredictionJob.load_outcomes`, doporučení recenze): klouzavé
    okno 90 dní filtruje `news_events.ts_event`, ne `news_prediction_outcomes.computed_at`.
    Po ostrém běhu přepne fáze kontaminace ~106 tis. oken w5 z True na False a `evaluate`
    jednorázově vyhodnotí i měsíce staré predikce s `computed_at` = teď — noční `news_weights`
    (a SentIndex) by se jinak 90 dní počítaly převážně z historie. Varianty: A okno podle času
    zprávy (zvoleno); B skript dopíše outcomes s `computed_at` = čas zprávy + okno; C přijmout.
    Okna přepnutá z False na True (~18 tis. w5) si dřívější outcomes ponechají — v okně vah
    jen za posledních 90 dní.

## Zvažované varianty

- **Významnost**: A) čistá funkce při čtení (zvoleno) — bez migrace a backfillu, reklasifikace
  i ruční korekce se projeví hned; B) uložený sloupec `news_events.significance` — levný SQL filtr,
  ale čtyři zapisovatelé by museli volat totéž a změna definice by znamenala backfill.
- **Agregátory**: A) strop 2 — ~66–78 významných denně, přesnost ~80 %; B) jen události
  (zvoleno) — ~44–51 denně, přesnost ~89 %, zmizí asi třetina relevantních souhrnů a výhledů trhu,
  driver zůstane ze zpráv Reuters a CNBC.
- **Kontaminace**: K1 jen jiná kategorie (zvoleno); K2 jen importance 3; K3 beze změny (plánovaná
  makra vypadnou z gate kvůli vlastnímu pokrytí).
- **Kdy reklasifikovat**: hned (zvoleno; vzorek H1 #1264 se restartuje), nebo až po vyhodnocení
  H1 (~20. 10.) — model by do té doby míchal dvě definice.
- **LLM (Gemini) pro kandidáty** a **ngram (#740)** jako náhrada regexu: LLM je od 13. 8.
  vypnuté, ngram má dnes přesnost 0,33 a úplnost 0,19 — obojí změřit na hotové testovací sadě
  (400 zpráv s konsenzem) a nasadit, jen když porazí v2.

## Měření (finální kód po recenzi, 26. 9. 2026)

Vzorek 400 (konsenzus A+B; pro v2 in-sample — pravidla se ladila na něm):

| pravidlo | v1: P / R (populace P) | v2: P / R (populace P [95% CI]) |
|---|---|---|
| importance ≥ 2 → relevance ≥ 2 | 0,36 / 0,76 (0,21) | 0,91 / 0,51 (0,92 [0,78–0,98]) |
| importance 3 → relevance 3 | 0,09 / 0,64 (0,02) | 0,81 / 1,00 (0,97 [0,82–1,00]) |
| významné → relevance ≥ 2 | 0,45 / 0,54 (0,37) | 0,91 / 0,49 (0,92 [0,78–0,98]) |
| významné → relevance 3 (úplnost) | 0,82 | 1,00 |
| zásadní → relevance 3 | 0,17 / 0,59 | 0,81 / 1,00 |
| přesnost kategorie | 53 % (61 %) | 68 % (81 %) |

Nižší úplnost u relevance ≥ 2 je záměr (sociální sítě bez kurátora, souhrny agregátorů).

Holdout 200 (29. 8.–12. 9., naslepo, jeden hodnotitel; v2.0 zmrazená před holdoutem měla
poctivý out-of-sample výsledek 50 % [40–60 %], finální kód obsahuje opravy podle holdoutu
a recenze): přesnost významných 91 % [77–97 %] (31 z 34; v1 odhadem 30 %), všech 17 zásadních
zpráv má importance 3 a je zásadních (35 % zásadních je relevance 3, 45 z 48 relevance ≥ 2);
významných bez kalendáře ~41,5 denně. **Makro kontrola 45/45** typických titulků (FOMC,
CPI/PCE/PPI/NFP/retail sales/ISM/GDP, claims, Michigan, cla, Írán/Hormuz, invaze, příměří,
shutdown, downgrade USA) na očekávané úrovni — golden test `news-engine/tests/test_classifier.py`
spolu s případy z recenze (měsíc May, cenové úrovně, apostrofy, falešné trojky).

Objemy populace 29. 8.–26. 9. (66 420 zpráv, export, kurátor jen podle příznaku): významných
47/den (vzorek 12.–26. 9. 47,8, holdout 43,0), z toho kalendář 1,5; zásadních 19,2/den;
importance 3 bez kalendáře 17,7/den (dřív ~185). Dry-run reklasifikace nad produkcí (jen
čtení) proběhl 26. 9. **před opravami z recenze** (52 500 ze 184 397 pravidlových eventů,
kontaminace w5 48 % → 24 %) — před ostrým během se přeměří.

## Důsledky

- **Mechanika signálů se mění**: buckety `news_model_stats` (kategorie × importance) se přeskupí
  a gate w5 se přepočítá. Offline simulace finálním kódem (w5, režim `all`, exporty 26. 9.;
  v0 reprodukuje 10/10 otevřených bucketů produkce): v2 + K1 bez sloučení 8 otevřených, **v2 +
  K1 + sloučení (bod 9) 6** — všechny **odložené GEOPOLITICS** (importance 1, 2, 3 × ES/NQ,
  n 118–371). Zavřou se všechny živé gate v0 (MACRO_GROWTH|2|neg_small NQ, MACRO_INFLATION|3
  pos/neg_small a neg_large, MACRO_GROWTH|3|pos_small ES). USD CPI neg_small (bez sloučení n 37
  ES / 31 NQ, Ø +27 / +43 bp) stojí na 18 / 15 releasech a zůstane zavřený, dokud nepřibude
  releasů. Odložené buckety mají silnější pseudoreplikaci, kterou sloučení podle času nechytí:
  zprávy jedné uzavírky (víkend) sdílejí základní cenu i první bar, tedy výnos — ~130 uzavírek
  na ~370 vzorků GEOPOLITICS|1; s proxy sloučením podle uzavírky zůstanou otevřené 2 buckety
  (Ø −7 až −25 bp místo −27 až −35). Týká se i produkce dnes (GEOPOLITICS|3 def, TECH|1 def)
  — samostatné issue (klíč uzavírky v `news_reactions`). Vzorek H1 (#1264) začíná znovu
  (pravidlo #453); minulé predikce a signály nesou verzi klasifikace a nepřepisují se. Drift
  hlídka (#403) může po přepočtu ohlásit posun — přepočítává nálezy celé znovu, reset není
  potřeba. Po ostrém běhu ověřit v logu přepočtu model stats.
- **SentIndex**: bez vah korelace úrovní ~0,78, σ ~0,54× — retro přepočet (ADR-0036) a přeměření
  epizod #565 (ADR-0037, připomínka ~5. 11.) na nové řadě; `news_weights` se přepočtou v noci
  z outcomes zpráv posledních 90 dní (bod 10), ngram `category_mean` při dalším tréninku.
- **Kalendář mimo USD** přestane být významný; `news_anomaly` se přestane kotvit na datech CAD,
  NZD, GBP. Burst dotažení výsledku (#386, `importance ≥ 3`) běží jen u USD High, cizí měny dostanou
  výsledek hodinovým dotažením. `publisher.upcoming` (T−10 min, `importance ≥ 3`) nově ohlásí
  USD High (dřív regex). Upozornění před releasem (ADR-0044) čte FF impact a změna se ho netýká.
- **LLM importance** strop feedu nemá — LLM je vypnuté; před jeho zapnutím je potřeba strop
  doplnit (nový issue).
- **Údržba slovníku režimu**: po změně režimu (nová válka, nová cla, nový předseda Fedu) dostanou
  zprávy nejvýš 2, dokud se konstanta neupraví.
- **Odhady jsou optimistické** (pravidla laděná na vzorku i holdoutu). Akceptace po nasazení:
  slepě ohodnotit 100 náhodných významných markerů z prvního týdne (cíl přesnost ≥ 70 %), všechny
  FF USD High a FOMC přítomné, `scripts/measure_news_anomaly.py --preopen-weeks 8`.
- Historické posty kurátorů z doby před příznakem `raw.curated` (#1291) dostanou kurátora podle
  autora (bod 8) — aproximace aktuálním seznamem: autor přidaný později zpětně zkurátoruje
  i své starší posty, odebraný je ztratí. Emulace kurátorů v `measure_news_anomaly.py` odpadla,
  replay čte klasifikaci z DB.
- Sloupec „Významné“ v auditu zdrojů (`GET /news/sources`) počítá podíl toutéž funkcí
  `significance_tier` (dřív importance ≥ 2 a skóre).
- Taxonomie (MARKETS, RATES, CB_FOREIGN) zůstává beze změny — samostatné issue.
