# ADR-0062: Plán opce QQQ ze scénáře dne — ceny z řetězu 1DTE, pravidla P1–P5, track record, snímky do Telegramu

- **Stav:** navrženo 2. 11. 2026. Body 1–7 oddílu Rozhodnutí přijal vlastník 2. 11. v epice #1738
  (Rozhodnuto, issuecomment-6586014388) a epiku schválil 2. 11. 21:03 SEČ
  (issuecomment-6587181454). Body **K1–K5 čekají na rozhodnutí vlastníka**, PR nese
  `needs-decision`. Body označené „odvozeno“ rozvádějí přijaté volby a vlastník je potvrdí spolu
  s K1–K5.
- **Datum:** 2026-11-02
- **Mění:** nic. Scénář dne NQ (ADR-0035, #1173) zůstává beze změny, plán QQQ je nadstavba.
- **Implementace:** epika #1738, sub-issues #1740 (F1a), #1741 (F2a), #1742 (F2b), #1743 (F4a),
  #1744 (F4b), #1745 (F4c), #1746 (F5a), #1747 (F1b), #1748 (F3), #1749 (F5b), #1750 (F6);
  tento ADR #1739.
- **Podklad:** rozbor 2. 11. (issuecomment-6585788810), skript
  `scripts/measure_option_plan_entries_1738.py` (jen čtení partic 2. 11.), měření v oddílu Kontext.

## Kontext

Auto-scénář dne NQ vzniká 15 min před US openem (9:15 ET) z verdiktu dne
(`engine/src/gexlens_engine/scenario_auto.py`). Vstup je cena v minutě vzniku, cíle jsou nejvýš
dva, dál cesta a termín settle. 2. 11. vznikl scénář #29: SHORT, skóre −5, vstup 28 753,75,
cíle ONL 28 587 a PDC 28 529. Vlastník podle něj obchodoval put QQQ 1DTE z plánu, který se
převáděl ručně. Rozbor ukázal tři věci. Směr i oba cíle seděly. Průrazový vstup B skončil na SL.
Vstup A po odmítnutí došel na TP. Ceny opcí odhadnuté z IV NQ (33 %) vyšly o 8–10 % výš než
skutečná IV QQQ 1DTE (29,8–31 %).

Dnešní stav: Telegram umí jen `sendMessage` (`api/src/gexlens_api/push_telegram.py:48`). Snímek
heatmapy NQ pošle frontend, jen když má otevřený graf (`PUT /scenarios/{id}/image`). Ad-hoc pohled
QQQ (`engine/src/gexlens_engine/tasty/adhoc.py`) sbírá jen front expiraci a jen s otevřeným
pohledem. Matplotlib v závislostech není.

Měření, o která se opírají K1–K4 (partice `data/`, jen čtení):

| co | hodnota | zdroj |
|---|---|---|
| poměr NQ/QQQ v minutě vzniku scénáře (14:15 UTC) | 41,304 (28 757,75 / 696,24); 14:00–14:25 UTC 41,299–41,311 | `derived/NQ/bars`, `derived/QQQ/bars` 2. 11. |
| úrovně NQ 0DTE 14:15 UTC převedené na QQQ | vstup 696,14 · flip 697,63 · ONH 698,33 · těžiště 696,32 · call wall 702,71 · TP1 (ONL) 692,11 · TP2 (PDC) 690,70 | `derived/NQ/20261102/levels`, scénář #29 |
| EM QQQ = straddle ATM 0DTE (strike 696, 9:23 ET) | 5,88 (call 3,02/3,10 + put 2,78/2,86) | `snapshots/QQQ/20261102` |
| největší kladná GEX QQQ 0DTE (9:23 ET) | strike 700, +208 M (698 +90 M, 699 +49 M); 0,63 EM nad cenou 696,30 | týž snímek |
| kotace opcí QQQ dlouho před openem | 23. 9. 2:38–4:23 ET ATM 1,96/2,00 beze změny — zamrzlé z close | `snapshots/QQQ/20260923` |
| kotace těsně před openem | 2. 11. 9:23–9:25 ET se hýbou, medián relativního spreadu 4,1–6,4 % (0,15 < \|delta\| < 0,85) | `snapshots/QQQ/20261102` |
| kotace v RTH | 23. 9. 9:44–9:48 ET medián spreadu 0,9–1,4 % | `snapshots/QQQ/20260923` |
| snímek heatmapy NQ z frontendu | 59 s po vzniku scénáře (14:15:00 → 14:15:59 UTC) | `data/scenarios/NQ/2026-11-02/29.png` |
| matplotlib Agg, graf 1200 × 700 px (lokálně, Windows) | +54 MB RSS, import 1,4 s (první spuštění 8,5 s), kreslení 0,22 s, PNG 40 kB | měření 2. 11. |
| model ceny putu 687 (`compute/gexfield.bs_price`, IV kalibrovaná) | fill 1,40 $ v 16:50 SEČ → 1,398; kotace 2,68/2,70 v 18:31 → 2,706 | skript |

## Rozhodnutí

### Přijato vlastníkem 2. 11. 2026 (#1738)

1. **Scénář NQ beze změny.** Verdikt, cíle, cesta, termín i vyhodnocení se nemění, jejich testy
   a golden data také ne. Pravidla P1–P5 platí jen pro plán QQQ, směr a cíle scénáře neovlivňují.
2. **Plán QQQ = převod scénáře NQ, ne vlastní verdikt QQQ.** Směr, cíle a termín jsou stejné.
   Long → call, short → put, proti verdiktu nikdy. Úrovně NQ se převedou poměrem NQ/QQQ v minutě
   vzniku plánu. Plán vzniká jen k auto-scénáři long/short; při `none`/`wait_news`, o víkendu
   a ve svátek ne (`compute/settle.is_trading_session`). Počítá ho deterministická čistá funkce
   v `engine/compute/` s golden testem. Uloží se point-in-time a zpětně se nepřepočítává.
3. **Q1 B — ceny z řetězu QQQ 1DTE z tastytrade** se zdrojem a časem, nové IBKR market data
   lines 0. Vybrané kontrakty a podklad QQQ se sledují do close.
4. **Q4 — dvě varianty opce.** Hlavní = strike s \|delta\| nejblíž 0,25 při vzniku, záložní = o strike
   dál od peněz (levnější). 2. 11.: put 687 a put 686. Kterou zobchodovat, volí vlastník.
   Varianta nad rozpočtem je viditelně označená, obě nad rozpočtem = `neobchodovat`.
5. **Q3 B — parametry účtu plánu QQQ v Settings, bez výchozí hodnoty v kódu** (repo je veřejné).
   Risk parametry futures setupů (#1185) se nepoužijí.
6. **Q2 B + Q2b II upravená — snímky.** Text zpráv jde hned. Graf plánu QQQ kreslí server a chodí
   vždy, do Briefingu i do Telegramu. NQ: heatmapa z aplikace, je-li otevřená, jinak serverový
   graf NQ ve stejném stylu. Bezhlavý prohlížeč ne.
7. **Q5 A — track record plánu QQQ od začátku**, vedený zvlášť od scénáře NQ: spuštění vstupu,
   SL/TP na podkladu, P&L opce z naměřených kotací. Brána `preliminary` při n < 30 jako u scénáře
   (ADR-0035).

### Pravidla plánu P1–P5 (rozbor 2. 11.)

Stop se nezvětšuje. Poučení z nevyšlé teze zpřísňuje vstup (AGENTS.md).

- **P1** Průrazový vstup B se nenavrhuje, když je největší kladná GEX QQQ 0DTE proti směru
  do 1 EM od ceny (short: nad cenou, long: pod cenou). 2. 11.: strike 700, +208 M, 0,63 EM →
  B vyřazen.
- **P2** SL = 5min close za extrémem výběru stopů + rezerva (definice v K3). Plán k němu uvede
  ekvivalentní cenu opce v čase vzniku.
- **P3** R:R se počítá z cen opce, SL proti TP1, při skutečné spoušti. Pod 2 = neobchodovat. Plán
  při vzniku uvede R:R z modelu, vyhodnocení (a živé upozornění podle K5) z kotace v minutě spouště.
- **P4** Plán uvede ztrátu opce při SL i nejhorší průběžné čerpání. Nevejde-li se do rozpočtu
  (bod 5), varianta je `nad rozpočtem`.
- **P5** Plán doporučí stop navázaný na podklad: podmíněný příkaz v TWS (podmínka ceny QQQ →
  prodat opci). Podmínku TWS spouští cena (výchozí je poslední obchod), ne 5min close, a plán to
  řekne. Dostupnost v mobilní aplikaci ověří vlastník.

### Odvozeno (rozvádí body 1–7)

- **1DTE** = nejbližší expirace s datem po obchodní seanci vzniku (`is_trading_session`, svátky
  NYSE podle ADR-0046): pondělí → úterý, pátek → pondělí.
- **EM QQQ** = straddle ATM 0DTE (mid callu + mid putu na striku nejblíž spotu) ze snímku řetězu
  při vzniku plánu — měřený, bez převodu z NQ. Řádek `em_respect` seance v té chvíli ještě
  neexistuje, vzniká po close (`scenarios_store.em_reference`).
- **Ceny opce při SL, TP1 a TP2** spočítá Black–Scholes (`compute/gexfield.bs_price`) s IV
  kontraktu ze snímku a časem do expirace v čase cíle podle cesty scénáře. Ponesou štítek
  „model z naměřené IV“. Naměřené ceny přinese až vyhodnocení. Na 2. 11. model s IV 29,8–30,9 %
  trefil fill i kotaci na ±0,02 $.
- **Rozpočet výkonu:** IBKR lines 0. Tasty: pásmo 0DTE a 1DTE jen po dobu snímku (ad-hoc pásmo
  0DTE QQQ mělo 2. 11. 272 kontraktů, strop serveru 25 000 položek), přes den 2 kontrakty
  a podklad. Minutový cyklus se nemění, plán vzniká jednou denně.

## K rozhodnutí (vlastník)

Kritéria podle AGENTS.md: rychlost, výkon (notebook 16 GB, WSL 6 GB, engine ~3 GB), relevance
dat pro obchodní rozhodnutí.

### K1 — kdo kreslí PNG

| varianta | výhody | nevýhody |
|---|---|---|
| **A — API v krátkém podprocesu** (doporučeno) | Telegram i `GET …/image` žijí v API, fotka jde bez další cesty; paměť (+54 MB) se po kreslení vrátí; pád kreslení neshodí API ani engine; engine se nemění | podproces na obrázek = ~1,4 s importu, 2–3× denně; nová závislost ve sdíleném python image |
| B — engine při vzniku plánu | data v paměti, nic se nečte | matplotlib v procesu s živými IBKR daty (~3 GB), +54 MB trvale; PNG musí do API přes disk nebo `/internal`; riziko pro kritický proces |
| C — API ve vlákně vlastního procesu | nejméně kódu | +54 MB trvale v API (~0,9 GB) od prvního obrázku; pyplot není thread-safe, jen objektové API `Figure` |

Doporučení **A**: rozhoduje výkon a izolace kritického procesu. Pravidlo „API nepočítá“ platí dál.
Renderer jen kreslí uložené hodnoty plánu a uložené bary a nemá žádný vlastní vzorec.

### K2 — uložení plánu, kotací a parametrů účtu

| varianta | výhody | nevýhody |
|---|---|---|
| **A — nová tabulka `option_plans` + JSON** (doporučeno) | sloupce jen pro dotazy a vazby: `id`, `scenario_id` (unikátní), `day`, `created_at`, `rules_version`, `verdict`, `status`, `plan` (JSON: úrovně, varianty, ceny, důvody), `evaluated_at`, `result` (JSON), `image_path`; vzor `scenarios`; změna pravidel = nová `rules_version`, ne migrace | detailní statistiky čtou JSON (track record vystačí se `status` a `result`) |
| B — normalizované sloupce za každou variantu | SQL nad každým číslem | ~30 sloupců, každá úprava pravidel = migrace PG |
| C — sloupec `option_plan` v tabulce `scenarios` | bez nové tabulky | mění schéma scénáře NQ (bod 1), track record se míchá |

K variantě A dál patří:

- **Kotace sledovaných kontraktů** (F1b) půjdou do partice `derived/QQQ/option_quotes/{den}.parquet`
  (`ts_min`, kontrakt, bid, ask, IV, delta, stáří), stejně jako ostatní minutová data. PG drží
  jen plán a výsledek.
- **Parametry účtu** budou dva klíče v tabulce `settings`. `option_plan_account_usd` slouží jen
  pro „% účtu“. `option_plan_max_loss_usd` je rozpočet P4 na obchod v $ (vlastník uvažuje
  v $ na kontrakt, „−100 $“). Když nejsou vyplněné, plán vznikne a obě varianty dostanou
  `neobchodovat — rozpočet nenastaven`. Tichý default není.

Doporučení **A**.

### K3 — vstupy, SL, časová okna a výstupy

Simulace: bary NQ 2. 11. převedené poměrem 41,304 z minuty scénáře, 5min bary po hodinách od
9:30 ET, časy SEČ. Při K4 A se vezme poměr z minuty 9:31 ET. Bary QQQ z té minuty 2. 11. uložené
nejsou a poměr přes den držel do 0,03 %. Rezerva je 0,05 EM = 0,29. „R:R opce“ = put 687 v modelu (IV 30 %), SL hned po vstupu (nejhorší
případ), TP1 v čase cíle 1 podle cesty scénáře (17:30 SEČ).

| úroveň vstupu A (short) | spoušť A | SL A | TP1 | R:R podkladu | put 687: vstup / SL / TP1 | R:R opce |
|---|---|---|---|---|---|---|
| **nejbližší z {flip, ONH/ONL, PDH/PDL, call/put wall} proti směru do 1 EM** = flip 697,63 | 16:40 za 697,60 | 699,77 | 5min bar do 18:35 | 2,53 | 1,25 / 0,89 (−36 $) / 2,59 (+134 $) | 3,77 |
| jen ONH/ONL = 698,33 | 16:35 za 698,10 | 699,77 | totéž | 3,59 | 1,16 / 0,90 (−27 $) / 2,59 (+143 $) | 5,38 |
| nejbližší ze všech úrovní = těžiště 696,32 | 16:55 za 696,22 | 699,77 | totéž | 1,16 | 1,51 / 0,88 (−63 $) / 2,59 (+108 $) | 1,70 → neobchodovat (P3) |
| úroveň 697,0 z ručního plánu (obchod vlastníka) | 16:50 za 696,76 | 699,77 | totéž | 1,55 | 1,40 / 0,88 (−52 $) / 2,59 (+119 $) | 2,29 |

Rezerva SL u úrovně flip: 0 → SL 699,47, R:R opce 4,28 · **0,05 EM → 699,77, 3,77** · 0,1 EM →
700,06, 3,38. Vstup B (první 5min close pod vstupem scénáře − rezerva = 695,85) by se spustil
v 15:35 za 695,06. Jeho SL nad ONH + rezerva (698,62) by padl v 16:25 (698,70), přesně jako
v rozboru. High dne 699,47 byl 0,14 EM nad SL B. P1 vstup B vyřadí.

**Varianta A (doporučeno):**

1. **Úroveň vstupu A** = nejbližší úroveň proti směru z množiny {flip 0DTE, ONH/ONL, PDH/PDL,
   call/put wall 0DTE} do 1 EM od vstupu scénáře. Množina je podmnožinou úrovní, ze kterých
   scénář bere cíle (`LevelInputs`). Těžiště a PDC v ní nejsou: těžiště je magnet, ne odraz,
   a 2. 11. by dalo R:R 1,70. Žádná úroveň do 1 EM = vstup A není.
2. **Spoušť A:** nejdřív 5min close za úrovní (přijetí za úrovní, výběr stopů), pak první
   5min close zpět. Vstup je v close toho baru.
3. **SL A:** 5min close za extrémem od testu (pro short high) + 0,05 EM.
4. **Vstup B:** první 5min close za vstupem scénáře ∓ 0,05 EM. SL B = 5min close za nejbližší
   úrovní proti směru z téže množiny + 0,05 EM. Vstup B jen tehdy, když P1 nenajde blokující GEX.
5. **Okna:** spoušť v US RTH od prvního 5min close (9:35 ET) do 12:00 ET (ruční vzor „do 18:00
   SEČ“). Exit nejpozději 15:45 ET, přes noc se nedrží. Po TP1 se SL posune na vstup (zpřísnění).
6. **Čerpání (P4):** ztráta opce na úrovni SL a na úrovni SL + 0,2 EM, protože 5min close může
   přestřelit (2. 11. high dne 0,14 EM za SL B).
7. **Track record:** vstup za ask, výstup za bid z naměřených kotací v minutě události. P&L pro
   výstup v TP1 i pro držení do TP2 se stopem na vstupu, obojí na 1 kontrakt.

**Další varianty:**

- **B — úroveň A jen flip** (ruční vzor „odmítnutí pod flipem“): 2. 11. stejný výsledek jako A.
  Když flip není proti směru do 1 EM, vstup A není, i kdyby ONH ležel blízko.
- **C — úroveň A jen ONH/ONL** (výběr stopů nad overnight extrémem): 2. 11. nejlepší R:R (5,38).
  Leží-li ONH/ONL dál než 1 EM, vstup A není. Na flip, kde vlastník 2. 11. skutečně vstoupil,
  nereaguje.
- **D — test knotem** (high za úrovní místo 5min close) dá rychlejší spoušť. 2. 11. vyšla stejně.
  Při poměru 41,31 místo 41,304 by ale knot na úrovni 697,0 spustil vstup už v 16:20 a SL v 16:25.
  Rozdíl 0,1 bodu stačí na falešnou spoušť.

**Důsledek pro AC epiky:** kritérium „vstup A 16:50 SEČ“ odpovídá úrovni 697,0 z ručního plánu.
Při A nebo B bude spoušť v 16:40, při C v 16:35. TP1 a TP2 z poměru minuty vzniku vyjdou 692,11
a 690,70 (v rozboru 691,9 a 690,4 při poměru 41,32).

Doporučení **A**: jedno pravidlo nad úrovněmi, které už scénář má (DRY), přísnější test
a rezerva škálovaná EM. 2. 11. by dala R:R opce 3,77 a ztrátu při SL −36 $. **Jeden den ale není
vzorek.** Pravidla se ověří track recordem (n ≥ 30) a mění se jen novou `rules_version`,
nikdy větším stopem.

### K4 — minuta vzniku plánu a výpadek řetězu

| varianta | výhody | nevýhody |
|---|---|---|
| **A — 9:31 ET, první minuta po US openu** (doporučeno) | živé kotace: spread ~1 % místo 4–6 %, žádné zamrzlé z close; jeden atomický vznik; vstup jde nejdřív v 9:35 ET, takže plán přijde včas (2. 11. by B spustil v 9:35 ET); EM ze straddlu po openu, stejný okamžik jako `em_respect` | plán QQQ přijde ~16 min po scénáři NQ (15:31 místo 15:15 SEČ; v týdnech posunu času o hodinu dřív) |
| B — 9:15 ET se scénářem | jedna zpráva v 15:15 SEČ | ceny před openem jsou zamrzlé z close nebo mají spread 4–6 %, takže R:R, rozpočet i výběr striku stojí na nevěrohodných cenách |
| C — dvoufázově (9:15 úrovně a text, 9:31 opce a ceny) | text hned | dvě zprávy a dva okamžiky jednoho plánu, řádek se po vzniku mění |

**Výpadek řetězu** (snímek do časového limitu neúplný):

- **(a) plán bez opce s důvodem „řetěz QQQ nedostupný“** (doporučeno). Úrovně a pravidla zůstanou,
  místo odhadu je mezera (AGENTS.md: fail fast, měřené > odhadnuté).
- (b) odhad z IV NQ 1DTE přes poměr se štítkem „odhad“. 2. 11. vyšel o 8–10 % výš a znamená další
  cestu v kódu.

Doporučení **A + (a)**.

### K5 — živá upozornění na spoušť

| varianta | výhody | nevýhody |
|---|---|---|
| A — jen večerní vyhodnocení (rozsah schválené epiky) | nic navíc | R:R při spoušti (P3) a čerpání (P4) se vlastník dozví až večer; v okamžiku obchodu počítá sám |
| **B — živý stav v minutovém cyklu** (doporučeno) | stavový automat z F3 (táž čistá funkce) běží nad bary do „teď“; při přechodu jde alert do zvonku a Telegramu („vstup A spuštěn 16:40, put 687 ask …, R:R … → obchodovat/neobchodovat“, dál SL, TP1, TP2, exit); večerní výsledek = konečný stav téže funkce | rozšiřuje schválený rozsah o jeden sub-issue (engine, ~1 session); nejvýš ~5 zpráv denně |

Doporučení **B**: rychlost („kliknu a je hotovo“). P3 je bez živého upozornění v okamžiku
obchodu nepoužitelné. Varianta rozšiřuje rozsah, proto ji musí odsouhlasit vlastník.

## Zvažované a odmítnuté (rozhodnuto 2. 11.)

- Vlastní verdikt QQQ, další instrumenty (SPY…) — až podle track recordu.
- Automatické zadávání příkazů — #924, #1187.
- Bezhlavý prohlížeč pro snímek heatmapy (Q2b).
- Velikost účtu z IBKR (Q3 A/B původního návrhu) — až s #1187.

## Důsledky

- **Nové prvky:** tabulka `option_plans` (před nasazením F2b záloha PG), partice
  `derived/QQQ/option_quotes`, dva klíče v `settings`, druhy alertů `option_plan_created`
  a `option_plan_result` (při K5 B navíc přechody stavu), endpointy `/option-plans*`, závislost
  matplotlib (podle K1).
- **Výkon:** IBKR lines 0. Tasty: krátký snímek pásma a přes den 3 symboly. Minutový cyklus beze
  změny (při K5 B jedna čistá funkce nad dnešními bary, jen ve dnech s plánem). RAM podle K1.
- **Relevance dat:** ceny jsou měřené a nesou stáří. Model (BS z naměřené IV) počítá jen
  hypotetické ceny při SL a TP a nese štítek. Chybějící řetěz = mezera.
- **Riziko:** pravidla stojí na jednom dni (2. 11.). Brána pro jakoukoli úpravu je track record
  (n ≥ 30). Úprava znamená novou `rules_version`, nikdy větší stop.
- Scénář NQ, verdikt dne a jejich golden data zůstávají beze změny.
