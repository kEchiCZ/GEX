# ADR-0062: Plán opce QQQ ze scénáře dne — ceny z řetězu 1DTE, pravidla P1–P5, track record, snímky do Telegramu

- **Stav:** rozhodnuto vlastníkem 2.–3. 11. 2026. Body 1–7 oddílu Rozhodnutí přijal vlastník
  2. 11. v epice #1738 (Rozhodnuto, issuecomment-6586014388) a epiku schválil 2. 11. 21:03 SEČ
  (issuecomment-6587181454). K1–K5 rozhodl v chatu 2.–3. 11.: **K1 A, K2 A** s doporučenými
  dílčími volbami, **K3 A** s oknem spouště až do exitu (bod 5), **K4 A + (a), K5 B** (#1757).
  Body „odvozeno“ platí s nimi. Pravidla verze 1 se před přijetím ADR přehrála zpětně na všech
  uložených scénářích NQ (#1756, 3. 11.). Přehrání ukázalo slabá místa. K6–K11 rozhodl vlastník
  3. 11. v chatu podle doporučení: **K6 B** (jen vstup A, K7 tím odpadá), **K8 A, K9 A, K10 B,
  K11 A**. Tím je ADR **přijat** (3. 11. 2026). Rozpor P2/P3 s K3 A, který se ukázal při
  implementaci F2a (#1741), rozhodl vlastník 3. 11. jako **K12 A**. **Revize 5. 11.:** vlastník
  změnil K4 na **C**. Opce QQQ se od 19.–20. 10. obchodují i před openem, takže plán vzniká
  v 9:15 ET se scénářem dne a v 9:31 ET se přecení (K4, oddíl „Revize 5. 11.“). (a) a K12 A
  platí dál.
- **Datum:** 2026-11-02 (revize K4 2026-11-05)
- **Mění:** nic. Scénář dne NQ (ADR-0035, #1173) zůstává beze změny, plán QQQ je nadstavba.
- **Implementace:** epika #1738, sub-issues #1740 (F1a), #1768 (F1c, ranní snímek 9:15 ET),
  #1741 (F2a), #1742 (F2b), #1769 (F2c, přecenění 9:31 ET), #1743 (F4a),
  #1744 (F4b), #1745 (F4c), #1746 (F5a), #1747 (F1b), #1748 (F3), #1757 (F3b), #1749 (F5b),
  #1750 (F6); tento ADR #1739, zpětné přehrání #1756 (F0b).
- **Podklad:** rozbor 2. 11. (issuecomment-6585788810); skripty
  `scripts/measure_option_plan_entries_1738.py` (jen čtení partic a GET API; měření 2. 11.
  a zpětné přehrání #1756) a `scripts/measure_matplotlib_png_1738.py`; měření v oddílu Kontext.

## Kontext

Auto-scénář dne NQ vzniká 15 min před US openem (9:15 ET) z verdiktu dne
(`engine/src/gexlens_engine/scenario_auto.py`). Vstup je cena v minutě vzniku, cíle jsou nejvýš
dva, dál cesta a termín settle. 2. 11. vznikl scénář #29: SHORT, skóre −5, vstup 28 753,75,
cíle ONL 28 587 a PDC 28 529, cíl 1 cesty v 16:30 UTC. Vlastník podle něj obchodoval put QQQ
1DTE z plánu, který se převáděl ručně. Rozbor ukázal tři věci. Směr i oba cíle seděly. Průrazový
vstup B skončil na SL. Vstup A po odmítnutí došel na TP. Ceny opcí odhadnuté z IV NQ (33 %)
vyšly o 8–10 % výš než skutečná IV QQQ 1DTE (29,8–31 %).

Dnešní stav: Telegram umí jen `sendMessage` (`api/src/gexlens_api/push_telegram.py:48`). Snímek
heatmapy NQ pošle frontend, jen když má otevřený graf (`PUT /scenarios/{id}/image`). Ad-hoc pohled
QQQ (`engine/src/gexlens_engine/tasty/adhoc.py`) sbírá jen front expiraci a jen s otevřeným
pohledem. Matplotlib v závislostech není. Rozhodnutí z 15. 10. v #1385 („Telegram PNG grafu jen
k odpovědím“) se týká odpovědí Clauda přes MCP. Snímky scénáře a plánu jsou nové rozhodnutí
vlastníka z 2. 11. (bod 6) a tamto rozhodnutí nemění.

Měření, o která se opírají K1–K4 (partice `data/`, jen čtení; skript výše):

| co | hodnota | zdroj |
|---|---|---|
| poměr NQ/QQQ, poslední uzavřený bar před vznikem scénáře (14:14 UTC) | 41,303 (bar 14:15 končí až 59 s po vzniku); 14:00–14:25 UTC 41,299–41,311 | `derived/NQ/bars`, `derived/QQQ/bars` 2. 11. |
| úrovně NQ 0DTE ve snímku 14:15 UTC převedené na QQQ | vstup 696,17 · flip 697,65 · ONH 698,35 · těžiště 696,34 · call wall 702,73 · TP1 (ONL) 692,13 · TP2 (PDC) 690,72 | `derived/NQ/20261102/levels`, bary od 17:00 CT 1. 11. |
| EM QQQ = `compute/emrespect.straddle_em`, 0DTE 9:23 ET | 5,88 (strike 696: call 3,02/3,10 + put 2,78/2,86) | `snapshots/QQQ/20261102` |
| největší kladná GEX QQQ 0DTE (9:23 ET) | strike 700, +208 M (698 +90 M, 699 +49 M); 0,63 EM nad spotem 696,30 | týž snímek |
| kotace opcí QQQ dlouho před openem | 23. 9. 2:38–4:23 ET ATM 1,96/2,00 beze změny — zamrzlé z close | `snapshots/QQQ/20260923` |
| kotace těsně před openem | 2. 11. 9:23–9:25 ET se hýbou, medián relativního spreadu 4,1–6,4 % (0,15 < \|delta\| < 0,85) | `snapshots/QQQ/20261102` |
| kotace v RTH | 23. 9. 9:44–9:48 ET medián spreadu 0,9–2,7 % | `snapshots/QQQ/20260923` |
| snímek heatmapy NQ z frontendu | 59 s po vzniku scénáře (14:15:00 → 14:15:59 UTC) | `data/scenarios/NQ/2026-11-02/29.png` |
| matplotlib Agg, graf 1200 × 700 px (lokálně, Windows) | +54 MB RSS, import 1,4–1,6 s (první spuštění 8,5 s), kreslení 0,2–0,3 s, PNG 40 kB | `measure_matplotlib_png_1738.py` |
| paměť kontejnerů 2. 11. ~22:00 SEČ | engine 1,2 GB, api 0,84 GB (strop Dockeru 5,8 GB) | `docker stats` |
| model ceny putu 687 (`compute/gexfield.bs_price`, jedna IV 30 %) | fill 1,40 $ v 16:50 SEČ → 1,42 (+1 %); kotace 2,68/2,70 v 18:31 → 2,58 (−4 %) | skript |

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

### Pravidla plánu P1–P5 (rozbor 2. 11., znění podle schválené epiky)

Stop se nezvětšuje. Poučení z nevyšlé teze zpřísňuje vstup (AGENTS.md). Kde se znění rozboru
a epiky liší, říká to poznámka u pravidla.

- **P1** Průrazový vstup B se nenavrhuje, když je největší kladná GEX QQQ 0DTE proti směru
  do 1 EM od spotu QQQ v minutě vzniku plánu (short: nad spotem, long: pod spotem). 2. 11.:
  strike 700, +208 M, 0,63 EM → B vyřazen. *Rozbor má navíc první větu „Short jen po odmítnutí
  (vstup A)“. Epika B jen podmiňuje. Volba výkladu je v K3, bod 8.* **Rozhodnuto 3. 11. (K6 B):**
  hlavní plán vstup B nemá. P1 rozhoduje jen o stínu vstupu B v track recordu (K10 B).
- **P2** SL = 5min close za extrémem výběru stopů + rezerva (definice v K3). Plán k němu uvede
  ekvivalentní cenu opce v čase vzniku (model) i v čase spouště (kotace). *Rozbor říká „v čase
  vstupu“, epika „v čase vzniku“. Plán uvede obojí.* **Rozhodnuto 3. 11. (K12 A):** SL podle K3
  bodu 3 je známý až při spoušti, takže model ceny opce při SL se počítá při spoušti, kotace ve
  vyhodnocení.
- **P3** R:R se počítá z cen opce, SL proti TP1, při skutečné spoušti. Pod 2 = neobchodovat.
  Plán při vzniku uvede R:R z modelu, vyhodnocení (a živé upozornění podle K5) z kotace v minutě
  spouště. **Rozhodnuto 3. 11. (K12 A):** R:R z modelu počítá plán při spoušti, ne při vzniku. Čas, ve kterém se SL a TP1 oceňují, je v K3, bod 6.
- **P4** Plán uvede ztrátu opce při SL i nejhorší průběžné čerpání (K3, bod 7). Nevejde-li se do
  rozpočtu (bod 5), varianta je `nad rozpočtem`.
- **P5** Plán doporučí stop navázaný na podklad: podmíněný příkaz v TWS (podmínka ceny QQQ →
  prodat opci). Podmínku TWS spouští cena (výchozí je poslední obchod), ne 5min close, a plán to
  řekne. Dostupnost v mobilní aplikaci ověří vlastník.

### Odvozeno (rozvádí body 1–7)

- **Minuta vzniku:** poměr NQ/QQQ = close poslední uzavřené 1m minuty NQ a QQQ před vznikem
  plánu (bez pohledu dopředu). Úrovně NQ ze snímku minuty vzniku. ONH/ONL od začátku seance
  (17:00 CT předchozího dne), tedy i z partice D−1 (#1002). Upřesnění 3. 11. (F1a #1740):
  živě je to stav na hranici 9:31:00 ET, tedy mid QQQ (bary QQQ se nesbírají) a spot NQ
  pipeline (IBKR → 5s bar → tasty) v témže okamžiku. Close baru by se od něj lišil o zlomek
  ticku. Od K4 C (5. 11.) je hranicí vzniku 9:15:00 ET (ranní snímek F1c #1768, minuta
  scénáře). Hranice 9:31:00 ET slouží přecenění (F2c #1769). Pravidlo poměru, spotu a úrovní
  je v obou okamžicích stejné.
- **1DTE** = nejbližší expirace s datem po obchodní seanci vzniku (`is_trading_session`, svátky
  NYSE podle ADR-0046): pondělí → úterý, pátek → pondělí.
- **EM QQQ** = `compute/emrespect.straddle_em` (ATM strike nejblíž spotu se zaplacenýma oběma
  stranama, do 3 kroků) nad snímkem 0DTE při vzniku plánu. EM je měřená, bez převodu z NQ. Řádek
  `em_respect` seance v té chvíli ještě neexistuje, vzniká po close.
- **Ceny opce při SL, TP1 a TP2** spočítá Black–Scholes (`compute/gexfield.bs_price`) s IV
  kontraktu ze snímku. Ponesou štítek „model z naměřené IV“, naměřené ceny přinese až
  vyhodnocení. 2. 11. vyšel model s jednou IV 30 % proti fillu o +1 % a proti kotaci v 18:31 o
  −4 %. IV QQQ 1DTE v minutě vzniku se 2. 11. neměřila.
- **Pásmo snímku a rozpočet tasty:** ±3 % kolem spotu v 0DTE i 1DTE (strike \|delta\| 0,25 1DTE
  ležel 2. 11. 1,3 % od spotu, největší GEX 0,5 %). To je ~170 kontraktů × 3 eventy (Quote,
  Greeks, Summary) ≈ 520 položek, dočasně v rezervě ad-hoc `ADHOC_RESERVE_ENTRIES` = 2 000
  (`tasty/budget.py:41`). Vejde se tam i s jedním otevřeným ad-hoc pohledem (~300 symbolů ×
  3 eventy). Od K4 C jsou to dva krátké snímky denně (9:15 a 9:31 ET), každý po svém skončení
  rezervu uvolní. Přes den 2 kontrakty a podklad. IBKR lines 0. Minutový cyklus se nemění, plán
  vzniká jednou denně.
- **Snímek NQ do Telegramu:** heatmapa z frontendu, dorazí-li do 3 min od vzniku scénáře
  (2. 11. dorazila za 59 s), jinak serverový graf NQ. Text odchází hned.

## K1–K5 (rozhodl vlastník 2.–3. 11.)

Kritéria podle AGENTS.md: rychlost, výkon (notebook 16 GB, Docker 5,8 GB), relevance dat pro
obchodní rozhodnutí.

### K1 — kdo kreslí PNG

| varianta | výhody | nevýhody |
|---|---|---|
| **A — API v krátkém podprocesu** (doporučeno) | Telegram i `GET …/image` žijí v API, fotka jde bez další cesty; paměť (+54 MB) se po kreslení vrátí; pád kreslení neshodí API ani engine; engine se nemění | podproces na obrázek = ~1,5 s importu, 2–3× denně; nová závislost ve sdíleném python image |
| B — engine při vzniku plánu | data v paměti, nic se nečte | matplotlib v procesu s živými IBKR daty (1,2 GB), +54 MB trvale; PNG musí do API přes disk nebo `/internal`; riziko pro kritický proces |
| C — API ve vlákně vlastního procesu | nejméně kódu | +54 MB trvale v API (0,84 GB) od prvního obrázku; pyplot není thread-safe, jen objektové API `Figure` |

**Rozhodnuto 2. 11.: A** (vlastník). Rozhoduje výkon a izolace kritického procesu. Pravidlo „API nepočítá“ platí dál.
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

**Rozhodnuto 2. 11.: A** včetně dílčích voleb výše (vlastník).

### K3 — vstupy, SL, časová okna, oceňování a výstupy

Simulace: bary NQ 2. 11. od 17:00 CT 1. 11. převedené poměrem 41,303, 5min bary po hodinách od
9:30 ET, časy SEČ. Rezerva je 0,05 EM = 0,29. Při K4 A se poměr vezme z poslední uzavřené minuty
před 9:31 ET. Bary QQQ z té minuty 2. 11. uložené nejsou a poměr přes den držel do 0,03 %.

Sloupce opce = put 687 v modelu (jedna IV 30 %). **R:R opce** oceňuje SL i TP1 v témže čase
(cíl 1 podle cesty scénáře, 17:30 SEČ, nejdřív v čase vstupu). **R:R přísně** oceňuje SL v čase
exitu (settle − 15 min, 21:45 SEČ). Put s časem ztrácí, takže pozdější SL znamená větší ztrátu.
**P4** = ztráta na úrovni SL + 0,2 EM v čase exitu.

| úroveň vstupu A (short) | spoušť A | SL A | R:R podkladu | put: vstup / SL / TP1 | R:R opce (přísně) | P4 |
|---|---|---|---|---|---|---|
| **nejbližší z {flip, ONH/ONL, PDH/PDL, call/put wall} proti směru do 1 EM** = flip 697,65 | 16:40 za 697,62 | 699,79 | 2,53 | 1,24 / 0,85 (−39 $) / 2,58 (+134 $) | 3,40 (2,27) | −72 $ |
| jen ONH/ONL = 698,35 | 16:35 za 698,12 | 699,79 | 3,59 | 1,16 / 0,85 (−31 $) / 2,58 (+142 $) | 4,63 (2,83) | −63 $ |
| nejbližší ze všech úrovní = těžiště 696,34 | 16:55 za 696,24 | 699,79 | 1,16 | 1,51 / 0,85 (−66 $) / 2,58 (+107 $) | 1,63 (1,26) → neobchodovat | −98 $ |
| úroveň 697,0 z ručního plánu (obchod vlastníka) | 16:50 za 696,78 | 699,79 | 1,55 | 1,40 / 0,85 (−55 $) / 2,58 (+118 $) | 2,16 (1,59) | −87 $ |

TP1 zasáhly všechny varianty v 5min baru 18:30–18:35 SEČ, SL žádná. Rezerva SL u úrovně flip:
0 → SL 699,49, R:R opce 3,80 (přísně 2,42) · **0,05 EM → 699,79, 3,40 (2,27)** · 0,1 EM →
700,08, 3,08 (2,14). Vstup B (první 5min close pod vstupem scénáře − rezerva = 695,87) by se
spustil v 15:35 za 695,08. SL B za úrovní A (flip + rezerva = 697,95) i za ONH jako v rozboru
(698,64) by padl v 16:25 (close 698,72). Bar, který SL B spustil, měl high 0,16 EM a close
0,13 EM za SL za úrovní A. P1 vstup B vyřadí.

**Varianta A (doporučeno):**

1. **Úroveň vstupu A** = nejbližší úroveň proti směru z množiny {flip 0DTE, ONH/ONL, PDH/PDL,
   call/put wall 0DTE} do 1 EM od vstupu scénáře. Množina je podmnožinou úrovní, ze kterých
   scénář bere cíle (`LevelInputs`). Těžiště a PDC v ní nejsou: těžiště je magnet, ne odraz,
   a 2. 11. by dalo R:R 1,63. Žádná úroveň do 1 EM = vstup A není.
2. **Spoušť A:** nejdřív 5min close za úrovní (přijetí za úrovní, výběr stopů), pak první
   5min close zpět. Vstup je v close toho baru.
3. **SL A:** 5min close za extrémem od testu (pro short high) + 0,05 EM.
4. **Vstup B:** první 5min close za vstupem scénáře ∓ 0,05 EM. SL B = 5min close za nejbližší
   úrovní proti směru z téže množiny (bez omezení 1 EM) + 0,05 EM. Když je úroveň A, je to ona.
   Vstup B jen tehdy, když P1 nenajde blokující GEX. *Od 3. 11. (K6 B) jen stín v track recordu
   (K10 B), hlavní plán vstup B nemá.*
5. **Okna a časový stop:** spoušť v US RTH od prvního 5min close (9:35 ET) až do exitu
   (vlastník 2. 11.: „až do exitu (settle − 15 min)“; navržené okno do 12:00 ET podle ručního
   vzoru „do 18:00 SEČ“ nepřijato, 2. 11. beze změny výsledku). Časový stop epiky („bez spuštění
   vstupu do časového stopu = bez vstupu“) = čas exitu. Exit = `settle_ts(den)` − 15 min
   (15:45 ET, ve zkrácené seanci 12:45 ET), přes noc se nedrží. Pozdní spoušť má do exitu málo
   času; plán ji nezakáže a track record ukáže, jestli dává smysl (jinak nová `rules_version`
   s uzávěrkou). Po vstupu se další časový stop nezavádí: ruční vzor ho 2. 11. vázal na průraz
   max pain a z jednoho dne se obecné pravidlo odvodit nedá. Po TP1 se SL posune na vstup
   (zpřísnění).
6. **Oceňování pro P3:** SL i TP1 v témže čase = čas cíle 1 podle cesty scénáře, nejdřív v čase
   vstupu a nejpozději v exitu (K11 A, 3. 11.). Přísné ocenění (SL v čase exitu) plán uvede
   vedle něj, rozhoduje ale tohle.
7. **Čerpání (P4):** ztráta opce na úrovni SL + 0,2 EM v čase exitu. Je to nejhorší rozumný
   případ: 5min close může přestřelit (2. 11. bar SL B 0,16 EM) a put s časem ztrácí.
8. **Výklad P1:** vstup B podmíněně podle GEX, pro long zrcadlově (znění schválené epiky).
   Alternativa je první věta rozboru: short jen vstupem A vždy. *Od 3. 11. (K6 B) platí jen pro
   stín vstupu B.*
9. **Track record:** vstup za ask, výstup za bid z naměřených kotací v minutě události. P&L pro
   výstup v TP1 i pro držení do TP2 se stopem na vstupu, obojí na 1 kontrakt. Spoušť, kterou P3
   vyřadí (R:R z kotace < 2), dostane stav `spuštěno — vyřazeno P3`. Do výsledku obchodů se
   nepočítá, ale její hypotetický výsledek se uloží zvlášť, aby šlo změřit, jestli filtr P3
   pomáhá.

**Další varianty:**

- **B — úroveň A jen flip** (ruční vzor „odmítnutí pod flipem“): 2. 11. stejný výsledek jako A.
  Když flip není proti směru do 1 EM, vstup A není, i kdyby ONH ležel blízko.
- **C — úroveň A jen ONH/ONL** (výběr stopů nad overnight extrémem): 2. 11. nejlepší R:R (4,63).
  Leží-li ONH/ONL dál než 1 EM, vstup A není. Na flip, kde vlastník 2. 11. skutečně vstoupil,
  nereaguje.
- **D — test knotem** (high za úrovní místo 5min close) dá rychlejší spoušť. 2. 11. vyšla stejně.
  Při poměru 41,31 místo 41,303 by ale knot na úrovni 697,0 spustil vstup už v 16:20 a SL v 16:25.
  Rozdíl 0,1 bodu stačí na falešnou spoušť.
- **Oceňování P3** jinak: SL v čase exitu (nejpřísnější). 2. 11. by vstup vlastníka (697,0)
  vyšel 1,59 → neobchodovat, flip 2,27. Nebo SL v čase vstupu, jak počítal rozbor. To je nejmírnější
  varianta: put s časem ztrácí, takže SL hned po vstupu podhodnotí ztrátu.

**Důsledek pro AC epiky:** kritérium „vstup A 16:50 SEČ“ odpovídalo úrovni 697,0 z ručního plánu.
Při zvolené A je spoušť v 16:40 (při B také, při C v 16:35); AC epiky a #1741 upraveny 3. 11. TP1 a TP2 z poměru minuty vzniku vyjdou 692,13
a 690,72 (v rozboru 691,9 a 690,4 při poměru 41,32). Tato čísla stojí na vstupech z 9:15 ET.
Podle K4 A (plán v 9:31 ET) vycházejí jinak. Rozhodnuto K9 A (3. 11.): AC epiky, #1741, #1748
a #1757 platí pro 9:31 ET, tedy vstup A 16:35 za 697,99, SL 699,66, TP1 691,99 / TP2 690,59.

**Rozhodnuto 3. 11.: A** (vlastník; body 1–4 a 6–9 podle návrhu, výklad P1 v bodě 8 = vstup B
podmíněně podle GEX, bod 5 s oknem do exitu). Pravidla nejsou pevný scénář: postup je stálý,
vstupy (směr verdiktu, úrovně, EM, GEX a řetěz QQQ) jsou každý den nové, takže plán se dni
přizpůsobí. Ruční plán 2. 11. byl úsudek nad týmiž vstupy a tato pravidla ho zapisují; zpětné
přehrání #1756 to ověří na všech uložených scénářích.

Důvod volby A: jedno pravidlo nad úrovněmi, které už scénář má (DRY), přísnější test,
rezerva škálovaná EM a oceňování SL, které theta nepodhodnotí. 2. 11. by dala R:R opce 3,40
(přísně 2,27), ztrátu při SL −39 $ a nejhorší čerpání −72 $. **Jeden den ale není vzorek.**
Pravidla se ověří track recordem (n ≥ 30) a mění se jen novou `rules_version`, nikdy větším
stopem.

### K4 — minuta vzniku plánu a výpadek řetězu

| varianta | výhody | nevýhody |
|---|---|---|
| **A — 9:31 ET, první minuta po US openu** (doporučeno) | živé kotace (v RTH spread 0,9–2,7 %, před openem 4–6 % nebo zamrzlé z close); jeden atomický vznik; vstup jde nejdřív v 9:35 ET, takže plán přijde včas (2. 11. by B spustil v 9:35 ET); EM ze straddlu po openu, stejný okamžik jako `em_respect` | plán QQQ přijde ~16 min po scénáři NQ (15:31 místo 15:15 SEČ; v týdnech posunu času o hodinu dřív) |
| B — 9:15 ET se scénářem | jedna zpráva v 15:15 SEČ | ceny před openem jsou zamrzlé z close nebo mají spread 4–6 %, takže R:R, rozpočet i výběr striku stojí na nevěrohodných cenách |
| C — dvoufázově (9:15 úrovně a text, 9:31 opce a ceny) | text hned | dvě zprávy a dva okamžiky jednoho plánu, řádek se po vzniku mění |

**Výpadek řetězu** (snímek do časového limitu neúplný):

- **(a) plán bez opce s důvodem „řetěz QQQ nedostupný“** (doporučeno). Úrovně a pravidla zůstanou,
  místo odhadu je mezera (AGENTS.md: fail fast, měřené > odhadnuté).
- (b) odhad z IV NQ 1DTE přes poměr se štítkem „odhad“. 2. 11. vyšel o 8–10 % výš a znamená další
  cestu v kódu.

**Rozhodnuto 3. 11.: A + (a)** (vlastník). **Nahrazeno 5. 11. variantou C**, (a) platí dál
(viz níže).

#### Revize 5. 11. — C: plán v 9:15 ET, přecenění v 9:31 ET (rozhodl vlastník)

**Proč znovu:** tabulka výše počítala s tím, že ceny před openem jsou zamrzlé z close. To platilo
při měření 23. 9. (2:38–4:23 ET beze změny). Cboe od 19. 10. 2026 a NYSE American od 20. 10.
2026 obchodují opce QQQ i v ranní seanci 7:30–9:25 ET, takže v 9:15 ET ceny už vznikají.
Vlastník proto 5. 11. otevřel K4 znovu.

| čas (QQQ 0DTE, pásmo ±3 %) | živé kotace | medián spreadu (0,15 < \|delta\| < 0,85) |
|---|---|---|
| 2. 11. 9:23 ET | 56/84 (67 %) | 4,1 % |
| 2. 11. 9:24 ET | 75/75 | 6,2 % |
| 4. 11. 9:31 ET (snímek F1a, po openu) | 84/84 | 1,1 % (0DTE), 1,4 % (1DTE) |

Spready před openem jsou 3–5× širší. U putu za 1,40 $ to dělá nejistotu mid ceny asi
±0,03–0,04 $, tedy ±3–4 $ na kontrakt. Měření v 9:15 ET ani 1DTE před openem zatím není. První
přinese F1c (#1768).

**Rozhodnuto 5. 11.: C** (vlastník v chatu: „Ano“). Pravidla:

1. **Vznik v 9:15 ET** se scénářem dne (15:15 SEČ, v týdnech posunu času o hodinu dřív) z ranního
   snímku řetězu (F1c). Úrovně NQ a poměr se berou z minuty scénáře. Plán dál nese EM ze straddlu
   0DTE, hlavní a záložní strike, kotace, model ceny v TP1/TP2, úroveň vstupu A a cíle. Stav
   varianty se určí jako v K12 A.
2. **Přecenění v 9:31 ET** ze snímku F1a: nové kotace vybraných kontraktů, IV a model v TP1/TP2.
   Strike, úrovně, poměr, vstup A a EM zůstávají z 9:15. Plán drží obě sady cen, každou se svým
   časem, a zpětně se nic nepřepisuje (K2 A).
3. **R:R, P3, P4 a rozpočet** se počítají beze změny podle K12 A, tedy až při spoušti (nejdřív
   v 9:35 ET) z tehdejších kotací (F1b, K5 B).
4. **Zpráva v 9:31 ET** (zvonek, Telegram F4c) nese přecenění. Když se změní stav varianty
   (`čeká` × `neobchodovat`), začíná „ZMĚNA“.

Odvozeno z C (zapsal agent 5. 11., aby C šlo implementovat):

5. **Neúplný ranní snímek** znamená, že kotace nebo OI jsou pod prahem, nebo chybí vstup (EM,
   poměr, spot). Pak plán vznikne v 9:15 bez opce s důvodem (a) a v 9:31 se sestaví celý z 9:31
   jako dřív K4 A, včetně úrovní, poměru, EM a striku. Neúplnost není výsledek pravidla, jako
   „žádná úroveň do 1 EM“ nebo „rozpočet nenastaven“. Takový plán z 9:15 platí a jen se
   přecení. Plán stojí vždy na jednom okamžiku, 9:15 a 9:31 se nemíchají (K9 B s úrovněmi z 9:15
   a opcí z 9:31 vlastník 3. 11. odmítl). Stav se tak mění, a „ZMĚNA“ vzniká, prakticky jen v tomto
   případě. Při samotném přecenění se nemění nic, na čem stav stojí.
6. **Neúplný snímek v 9:31 u úplného plánu:** přecenění se nezapíše a důvod jde do plánu. Platí
   ceny z 9:15 se svým časem. Když jsou neúplné oba snímky, plán zůstane bez opce (a).
7. **Ranní snímek F1c** bere jen kotace z ranní seance téhož dne (od 7:30 ET). Poslední kotaci
   z close, kterou DXLink pošle při odběru, nepočítá. OI bere ze Summary téhož rána. Že nese
   dnešní publikaci, F1c ověří proti OI po openu. Práh pokrytí 90 % platí, dokud ho měření v 9:15
   nevyvrátí. Změnit ho může jen vlastník.
   *Upřesnění 5. 11. (F1c #1768, rozhodl vlastník: Q1 A, Q2 A):* feed čas kotace nenese
   (`bidTime` = 0), takže kotace a greeks se počítají jen po aktualizaci, která přijde po eventu
   z odběru. Pásmo se odebere už v náběhu 9:13:30 ET, 90 s před hranicí. OI se počítá jen ze
   Summary s `dayId` = den seance.
8. **Souběh:** scénář vzniká v minutě 9:15 ET a snímek skončí nejpozději 40 s po hranici 9:15:00.
   Plán čeká na obojí (F2b #1742).

**Golden a AC:** 2. 11. ranní řetěz uložený není, takže platí bod 5. Čísla K9 A a golden #1741 se
nemění (vstup A 16:35 SEČ za 697,99). Pro plán úplný už v 9:15 golden z uloženého dne není. Test
přecenění s pevným strikem přinese F2c (#1769), naměřený golden první seance F6 (#1750).

**Důvod:** rychlost, protože plán přijde se scénářem v 15:15 SEČ, a relevance dat, protože přesné
ceny po openu přijdou v 15:31 a o obchodu rozhodují živé ceny při spoušti (K12 A). Cena: dvě
zprávy a dvě sady cen jednoho plánu a dva sub-issues navíc (F1c #1768, F2c #1769).

### K5 — živá upozornění na spoušť

| varianta | výhody | nevýhody |
|---|---|---|
| A — jen večerní vyhodnocení (rozsah schválené epiky) | nic navíc | R:R při spoušti (P3) a čerpání (P4) se vlastník dozví až večer; v okamžiku obchodu počítá sám |
| **B — živý stav v minutovém cyklu** (doporučeno) | stavový automat z F3 (táž čistá funkce) běží nad bary do „teď“; při přechodu jde alert do zvonku a Telegramu („vstup A spuštěn 16:40, put 687 ask …, R:R … → obchodovat/neobchodovat“, dál SL, TP1, TP2, exit); večerní výsledek = konečný stav téže funkce | rozšiřuje schválený rozsah o jeden sub-issue (engine, ~1 session); nejvýš ~5 zpráv denně |

**Rozhodnuto 3. 11.: B** (vlastník; rozšíření rozsahu o sub-issue #1757). Důvod: rychlost
(„kliknu a je hotovo“). P3 je bez živého upozornění v okamžiku obchodu nepoužitelné.

## Zpětné přehrání pravidel verze 1 (#1756, 3. 11.)

Skript `scripts/measure_option_plan_entries_1738.py` přehrál všechny auto scénáře z
`GET /scenarios` týmiž funkcemi, kterými ověřuje měření K3. Jen čte partice a GET API.
Na vstupech K3 dává 2. 11. táž čísla jako tabulka K3: vstup A 16:40 za 697,62, SL 699,79,
R:R podkladu 2,53, R:R opce 3,40 (přísně 2,27), P4 −72 $, TP1 692,13 / TP2 690,72.

**Vzorek:**

- 29 scénářů ve 21 dnech = **15 NQ** (12 long, 3 short) + **14 ES** (#1, 2, 4, 6, 8, 10, 12,
  15, 18, 19, 20, 22, 25, 27). Plán QQQ je převod scénáře NQ (bod 2), ES se proto nepřehrává.
- Dny se dvěma scénáři jsou vždy ES + NQ:
  - 22. 9. (#2, #3), 26. 9. (#6, #7), 27. 9. (#8, #9), 29. 9. (#10, #11);
  - 30. 9. (#12, #13), 2. 10. (#15, #16), 29. 10. (#25, #26), 30. 10. (#27, #28).
- Mezery NQ:
  - #7 (so 26. 9.) a #9 (ne 27. 9.) vznikly mimo obchodní seanci (#1309);
  - #3 (22. 9.) nemá poměr NQ/QQQ bez pohledu dopředu, první pár barů je až 22. 9. 13:02 ET.
- Přehráno je **12 scénářů NQ ve 12 dnech** (9 long, 3 short). 2. 11. je den, ze kterého
  pravidla vznikla, mimo vzorek je tedy 11 scénářů.

**Vstupy plánu v 9:31 ET (K4 A) a jejich limity:**

- **Poměr NQ/QQQ** je naměřený jen 1. 10. a 2. 11. jako poslední pár barů před 9:31 ET (bar QQQ
  9:30 ET aplikace nesbírala). V ostatních 10 dnech je převzatý z posledního dřívějšího páru
  (23. 9. nebo 1. 10.). Na poměru nezávisí spouště, SL, TP ani R:R podkladu. Ceny QQQ a $ opce
  se mění úměrně, o ≤ 0,5 % (poměr 41,30–41,51).
- **EM:** 2. 11. ze straddlu QQQ 0DTE (6,01), jinak ze straddlu NQ 0DTE vyděleného poměrem.
- **Opce:** \|delta\| nejblíž 0,25, IV = ATM IV NQ 1DTE (12,7–31,4 %). Ceny jsou **model**:
  2. 11. byla IV NQ o 8–10 % nad skutečnou IV QQQ.
- **P1:** GEX QQQ byla jen 2. 11. (P1 blokuje). Ostatních 11 dní je **bez dat**, takže vstup B
  zůstal povolený.
- **P3 oceňuje cíl nejpozději v exitu** (upřesnění K3 bodu 6, rozhodnuto K11 A).

**Výsledek pravidel v1** (čas Praha; R:R opce a P&L = model, $ na 1 kontrakt; u vyřazených P3
hypotetický výsledek; plná tabulka se spouští A a B, SL a TP je v #1738):

| # | den | směr | úroveň A (od vstupu) | vstup plánu | R:R opce (přísně) | P4 | výsledek | P&L TP1 / TP2 |
|---|---|---|---|---|---|---|---|---|
| 5 | 25. 9. | long | PDH (0,44 EM) | B 15:55 za 744,61 (A až 17:05) | 0,09 (0,08) | −98 $ | vyřazeno P3 (TP1, pak stop na vstupu) | +10 / −9 $ |
| 11 | 29. 9. | long | žádná do 1 EM | B 16:30 za 738,82 | 0,24 (0,22) | −122 $ | vyřazeno P3 (exit) | −59 / −59 $ |
| 13 | 30. 9. | long | PDH (0,06 EM) | B 15:35 za 741,15 | −0,42 (−0,42) | −82 $ | vyřazeno P3 (TP1, jediný cíl) | +8 / +8 $ |
| 14 | 1. 10. | long | put wall (0,30 EM) | A 15:55 za 742,32 | 1,04 (0,78) | −94 $ | vyřazeno P3 (SL) | −81 / −81 $ |
| 16 | 2. 10. | long | PDH (0,06 EM) | A 15:45 za 751,76 | 0,03 (0,03) | −108 $ | vyřazeno P3 (TP1, jediný cíl) | +16 / +16 $ |
| 17 | 6. 10. | long | flip (0,76 EM) | B 15:45 za 759,94 | 0,10 (0,08) | −65 $ | vyřazeno P3 (TP1, pak stop na vstupu) | +10 / −10 $ |
| 21 | 21. 10. | long | flip (0,08 EM) | B 15:35 za 720,47 (A až 15:50) | 0,02 (0,01) | −78 $ | vyřazeno P3 (SL) | −35 / −35 $ |
| 23 | 26. 10. | long | put wall (0,91 EM) | B 14:40 za 706,73 | −0,01 (−0,01) | −183 $ | vyřazeno P3 (TP2) | +7 / +47 $ |
| 24 | 28. 10. | long | PDH (0,18 EM) | A 17:55 za 721,30 | 0,29 (0,29) | −104 $ | vyřazeno P3 (TP1, jediný cíl) | +34 / +34 $ |
| 26 | 29. 10. | short | call wall (0,05 EM) | B 14:35 za 709,77 | −0,71 (−0,63) | −211 $ | vyřazeno P3, cena už za TP1 i TP2 | — |
| 28 | 30. 10. | short | žádná do 1 EM | B 14:35 za 691,86 | −0,29 (−0,29) | −218 $ | vyřazeno P3, cena už za TP1 | — |
| 29 | 2. 11. | short | flip (0,36 EM) | A 16:35 za 697,99 (B vyřadil P1) | 4,45 (2,69) | −70 $ | **obchod: TP1 18:35, TP2 18:45** | +141 / +190 $ |

**Souhrn:**

- **Vstup plánu:** A 4×, B 8×, bez vstupu 0×.
- **R:R opce pod 2:** 11 z 12. Ve 2 případech přišla spoušť až s cenou za cílem 1.
- **Obchod:** jediný, 2. 11. (TP2).
- **Vyřazené P3 hypoteticky** (bez 2 spouští za TP1): −90 $, tedy 6 zisků +7 až +34 $ a 3 ztráty
  −35 až −81 $.
- **Úroveň A:** PDH 4, flip 3, put wall 2, call wall 1, žádná do 1 EM 2.
- **Proč je R:R nízké** (mimo 2. 11.):
  - TP1 scénáře NQ leží 0,14–0,42 EM od vstupu scénáře (2. 11. 0,67 EM);
  - riziko do SL je 0,22–1,81 EM;
  - R:R podkladu při spoušti nejvýš 1,05 (1. 10.);
  - cena po spoušti pokračovala ve směru o 0,15–1,67 EM (medián 0,54), o ≥ 1 EM ve 3 dnech.

**Alternativy na týchž 12 dnech** (každá spoušť se hledá jen jednou, „čekat“ je proto dolní mez):

| varianta | vstup A / B / bez | R:R < 2 | obchody | P&L obchodů TP1 / TP2 | vyřazené P3 hypoteticky |
|---|---|---|---|---|---|
| pravidla v1 (K3 A) | 4 / 8 / 0 | 11 (2 za TP1) | 1: TP2 | +141 / +190 $ | −90 $ (6 / 3) |
| úroveň jen flip (K3 B) | 3 / 5 / 4 | 6 (0 za TP1) | 2: SL 1. 10., TP2 | +82 / +131 $ | +54 $ (5 / 1) |
| úroveň jen ONH/ONL (K3 C) | 2 / 10 / 0 | 10 (2 za TP1) | 2: SL 1. 10., TP2 | +82 / +131 $ | −20 $ (6 / 2) |
| test knotem (K3 D) | 7 / 5 / 0 | 11 (2 za TP1) | 1: TP2 | +141 / +190 $ | −68 $ (6 / 3) |
| rezerva SL 0 | 4 / 8 / 0 | 11 (2 za TP1) | 1: TP2 | +141 / +190 $ | −90 $ (6 / 3) |
| short jen vstupem A | 4 / 6 / 2 | 9 (0 za TP1) | 1: TP2 | +141 / +190 $ | −90 $ (6 / 3) |
| **jen vstup A (B nikdy) = hlavní pravidla od K6 B** | 6 / 0 / 6 | 5 (0 za TP1) | 1: TP2 | +141 / +190 $ | +21 $ (4 / 1) |
| jen A + test knotem (stín K10 B) | 7 / 0 / 5 | 6 (0 za TP1) | 1: TP2 | +141 / +190 $ | −26 $ (4 / 2) |
| po vyřazení P3 čekat na další spoušť | 4 / 8 / 0 | 11 (2 za TP1) | 1: TP2 | +141 / +190 $ | −90 $ (6 / 3) |
| spoušť za TP1 = bez vstupu | 4 / 6 / 2 | 9 (0 za TP1) | 1: TP2 | +141 / +190 $ | −90 $ (6 / 3) |
| R:R do TP2 (když je) | 4 / 8 / 0 | 11 (2 za TP1) | 1: TP2 | +141 / +190 $ | −90 $ (6 / 3) |

Úroveň jen flip a jen ONH/ONL přidají 1. 10. (R:R 2,01), který skončil na SL (−59 $). Práh P3 = 1
by přidal 1. 10. s úrovní A put wall (R:R 1,04, SL −81 $). Práh 1,5 nepřidá nic.

### K6 — vstup B (rozhodnuto 3. 11.: B)

B se spouští prvním 5min close za vstupem scénáře ∓ 0,05 EM. Vstup scénáře je cena z 9:15 ET,
takže B neměří průraz úrovně, jen pohyb ceny po vzniku scénáře. SL B leží za nejbližší úrovní
(0,2–1,8 EM), cíl 1 je 0,1–0,4 EM od vstupu.

Ve vzorku se B spustil 8× z 12: 6× do 15 min po openu a 2× s cenou už za cílem 1. R:R měl
nejvýš 0,24, takže ani jednou nevedl k obchodu.

| varianta | výhody | nevýhody |
|---|---|---|
| A — ponechat (B podmíněně podle P1, K3 bod 8) | schválená pravidla se nemění; P3 B stejně vyřadí | při K5 B skoro každé ráno upozornění „vstup B spuštěn → neobchodovat“; B jako první spoušť ukončí plán, A už nepřijde (25. 9., 21. 10.; viz K7) |
| **B — jen vstup A, B nikdy** (doporučeno) | ve vzorku týž obchod (2. 11.); místo spouští s R:R ≤ 0,24 je 6× „bez vstupu“ a A se spustí 6×; P1 plán nepotřebuje (řetěz QQQ zůstává pro ceny a EM); odpovídá poučení 2. 11. (B skončil na SL) | mění schválený výklad P1 (K3 bod 8); den s průrazem bez návratu k úrovni nedá vstup (ve vzorku žádný B neměl R:R ≥ 2, ztráta se nedá změřit) |
| C — short jen vstupem A (první věta rozboru) | mění jen short | ve vzorku jen 3 short; long dál 6× B s R:R ≤ 0,24 |

**Rozhodnuto 3. 11.: B** (vlastník v chatu, „Ano“ k doporučení). Hlavní plán má jen vstup A
(K3 body 1–3). Vstup A se hledá jednou: spoušť vyřazená P3 je konečný stav `spuštěno — vyřazeno
P3`, takže K7 odpadá. Vstup B podle K3 bodu 4 a P1 zůstává jako stín v track recordu (K10 B).
Ve vzorku dají hlavní pravidla vstup A 6×, bez vstupu 6× a týž jediný obchod (2. 11.).

### K7 — spoušť vyřazená P3: konec plánu, nebo čekat? (odpadá s K6 B)

Pravidla to neříkají. Přehrání počítá variantu A.

| varianta | výhody | nevýhody |
|---|---|---|
| A — konec plánu (`spuštěno — vyřazeno P3` je konečný stav) | jedno upozornění, jednoduchý stav | vyřazený B zablokuje pozdější A |
| **B — čekat na další spoušť do exitu** (doporučeno) | vyřazená spoušť neblokuje lepší pozdější | víc přechodů a upozornění |

Ve vzorku mezi A a B rozdíl není: A po vyřazeném B nedosáhl R:R 2. Jde o dolní mez, protože skript
hledá A i B jen jednou a opakovaný test úrovně nezkouší. Při K6 B otázka odpadá, pokud se A smí
spustit jen jednou.

### K8 — R:R ≥ 2 jen 2. 11. (rozhodnuto 3. 11.: A)

| varianta | výhody | nevýhody |
|---|---|---|
| **A — přijmout** (doporučeno) | P3 dělá, k čemu je: vyřazené spouště by daly −90 $; pravidla se nemění; track record počítá i vyřazené (K3 bod 9), takže důkaz roste s každým plánem, ne jen s obchodem | plán QQQ bude většinou „neobchodovat — R:R < 2“; obchodů málo (ve vzorku 1 z 12), n ≥ 30 obchodů potrvá dlouho |
| B — snížit práh P3 na 1,5 nebo 1 | víc obchodů | 1,5 ve vzorku nic nepřidá; 1 přidá 1. 10. (SL −81 $); oslabuje pravidlo z rozboru |
| C — vlastní cíle plánu QQQ dál než cíle scénáře | využil by pohyb za cílem (≥ 1 EM ve 3 dnech) | mění přijatý bod 2 (cíle = cíle scénáře); nové pravidlo bez měření, na 12 dnech hrozí přeučení; R:R do TP2 scénáře nepřidal nic; jen jako nová `rules_version` podle track recordu |

**Rozhodnuto 3. 11.: A** (vlastník). Práh P3 zůstává 2. Plán bude většinou „neobchodovat —
R:R < 2“ a track record počítá i vyřazené spouště (K3 bod 9).

### K9 — čísla 2. 11. podle K4 A (rozhodnuto 3. 11.: A)

AC epiky, AC #1741 i tabulka K3 počítají vstupy z minuty scénáře (9:15 ET). K4 A a oddíl
Odvozeno ale určují vznik plánu v 9:31 ET:

- **úrovně ze snímku minuty vzniku:** flip 698,21 místo 697,65;
- **poměr z poslední uzavřené minuty:** 41,311 (pár 9:25 ET, bar QQQ 9:30 ET chybí);
- **EM 6,01** ze straddlu 695.

Minuta snímku úrovní mění úroveň A jen ve 2 z 12 dní (2. 11. flip o +0,56, 26. 10. put wall
místo flipu). Výsledek P3 nemění v žádném dni.

| varianta | výhody | nevýhody |
|---|---|---|
| **A — AC epiky a golden #1741 přepočítat na K4 A** (doporučeno) | odpovídá přijatému K4 A a Odvozeno; 2. 11.: vstup A 16:35 za 697,99, SL 699,66, TP1 691,99 / TP2 690,59, R:R 4,45 (přísně 2,69), P4 −70 $ (put 687, model IV 31,4 %) | mění čísla AC, která vlastník zná z 2. 11. |
| B — úrovně ze snímku scénáře 9:15 ET, poměr a opce z 9:31 ET | úrovně odpovídají snímku scénáře NQ | úrovně jsou o 16 min starší a Odvozeno se mění; 2. 11.: 16:40 za 697,48, SL 699,66, R:R 3,27 (2,16), P4 −80 $ |

**Rozhodnuto 3. 11.: A** (vlastník). Golden 2. 11. stojí na snímku úrovní NQ 14:31 UTC (flip
28 843,74, call wall 29 025, put wall 28 500, těžiště 28 741,59), ONH/ONL 28 844 / 28 587,
poměru 41,311 (pár 14:25 UTC) a řetězu QQQ 0DTE z 9:23–9:25 ET. Spot QQQ v 9:31 je
695,26 (NQ 28 722) a EM 6,01. P1 pro stín vstupu B blokuje: největší kladná GEX je 700
(+208 M), 0,79 EM od spotu. Kontrola měření K3 (vstupy z 9:15 ET) zůstává ve skriptu
jako AC #1756.

Od K4 C (5. 11.) platí tato čísla dál. 2. 11. ranní řetěz chybí, takže plán by vznikl v 9:15 bez
opce a v 9:31 celý z 9:31 (K4, revize 5. 11., bod 5).

### K10 — stínové varianty v track recordu (rozhodnuto 3. 11.: B)

Stín = táž čistá funkce s jinými parametry pravidel. Hypotetický výsledek se uloží v JSON
výsledku vedle hlavního (K2 A) a do výsledku obchodů se nepočítá.

| varianta | výhody | nevýhody |
|---|---|---|
| A — žádné (jen v1 a hypotetický výsledek vyřazených P3, K3 bod 9) | F3 (#1748) a F5b (#1749) beze změny | co přehrání nerozhodlo, zůstane bez dat |
| **B — dva stíny: vstup B (při K6 B) a test knotem** (doporučeno) | měří právě to, co vzorek nerozhodl: B neměl data P1, knot dal víc spouští (7 proti 4) se stejným výsledkem | o něco víc práce v F3 a v track recordu F5b |
| C — všechny alternativy z tabulky | nejvíc dat | úroveň jen flip a jen ONH/ONL vzorek už vyvrátil (ztráta 1. 10.); víc šumu v track recordu |

**Rozhodnuto 3. 11.: B** (vlastník). Stíny jsou dva:

1. **vstup B** = pravidla v1 (K3 body 4 a 8, P1);
2. **test knotem** nad hlavními pravidly (jen A).

Oba počítá táž čistá funkce jako hlavní plán. Výsledek stínů jde do JSON `result` (K2 A), do
výsledku obchodů ani do track recordu hlavního plánu se nepočítá a živá upozornění (K5 B) nemá.
Ve vzorku by stín knotu dal 7 spouští A (hlavní 6) a týž jediný obchod. 2. 11. vstoupí stejně
jako hlavní plán.

### K11 — čas ocenění cíle pro P3 nejpozději v exitu (rozhodnuto 3. 11.: A)

K3 bod 6 oceňuje SL i TP1 v čase cíle 1 podle cesty scénáře. U scénáře s jedním cílem leží tento
bod v termínu settle, tedy 15 min po exitu, kdy pozice už neexistuje (K3 bod 5).

| varianta | výhody | nevýhody |
|---|---|---|
| **A — čas cíle podle cesty, nejpozději exit** (doporučeno; přehrání počítá takto) | oceňuje jen okamžik, kdy pozice může existovat; F2a (#1741) počítá stejně | mění znění schváleného bodu 6 |
| B — doslova podle bodu 6 (i po exitu) | znění beze změny | oceňuje pozici po exitu |

Ve vzorku jde o 4 scénáře s jedním cílem (30. 9., 2. 10., 28. 10., 30. 10.). R:R se mění nejvýš
o 0,03 a výsledek P3 v žádném z nich.

**Rozhodnuto 3. 11.: A** (vlastník). Znění K3 bodu 6 je upravené.

### K12 — R:R a rozpočet při vzniku plánu (rozhodnuto 3. 11.: A)

P2 a P3 chtějí cenu opce při SL a R:R z modelu už při vzniku plánu (9:31 ET). SL podle K3 bodu 3
je ale 5min close za extrémem od testu úrovně a test v 9:31 ET ještě nenastal. 2. 11. přišel
až v 16:35 SEČ a SL vyšel 699,66. Rozpor se ukázal při implementaci F2a (#1741).

| varianta | výhody | nevýhody |
|---|---|---|
| **A — R:R, P3, P4 a rozpočet až při spoušti** (doporučeno) | nic se neodhaduje; odpovídá Návrhu epiky („R:R při spoušti“) a K3 bodu 6 („nejdřív v čase vstupu“); R:R z kotace přinese živé upozornění K5 B (#1757) | ranní plán ještě neřekne, zda se varianta vejde do rozpočtu |
| B — předběžně při vzniku (vstup na úrovni A, SL = úroveň + 0,05 EM, štítek „předběžně“) | rozpočet vidět ráno | odhad je systematicky optimistický, protože SL leží nejblíž, jak může: 2. 11. R:R 9,20 a P4 −53 $ proti 4,61 a −65 $ při spoušti |

**Rozhodnuto 3. 11.: A** (vlastník v chatu). Při vzniku plán nese opci, kotace, úroveň vstupu A,
cíle a model ceny v TP1/TP2. Varianta má stav `čeká — čeká na vstup A (úroveň)`, nebo
`neobchodovat — důvod`, když vstup nebo opce nejde (rozpočet nenastaven, žádná úroveň do 1 EM,
chybí EM nebo řetěz). Táž čistá funkce s bary po vzniku najde spoušť a ocení ji (P3, P4).
Verdikt varianty je pak `obchodovat`, nebo `neobchodovat — důvod`. Od K4 C (5. 11.) to platí
pro vznik v 9:15 ET i pro přecenění v 9:31 ET. Ani vznik, ani přecenění nepočítají R:R, P3,
P4 ani rozpočet.

## Zvažované a odmítnuté (rozhodnuto 2. 11.)

- Vlastní verdikt QQQ, další instrumenty (SPY…) — až podle track recordu.
- Automatické zadávání příkazů — #924, #1187.
- Bezhlavý prohlížeč pro snímek heatmapy (Q2b).
- Velikost účtu z IBKR (Q3 A/B původního návrhu) — až s #1187.
- Celý plán každé ráno od Clauda místo pravidel (3. 11.): postup by se den ode dne lišil a track
  record by neměřil jednu metodu; ruční plán 2. 11. udělal právě chyby, které P1–P5 opravují
  (vstup B na SL, R:R vstupu A odhadem ~6 proti ~2,5). Vlastník chce plán přizpůsobený dni
  a aktuálním informacím: pravidla počítají z dat dne a kontext mimo data (události, zprávy)
  připíše později Claude komentářem k plánu přes MCP a plánované běhy epiky #1385 (F3 #1407,
  F5 #1405). Komentář čísla plánu nemění.

## Důsledky

- **Nové prvky:** tabulka `option_plans` (před nasazením F2b záloha PG), partice
  `derived/QQQ/option_quotes`, dva klíče v `settings`, druhy alertů `option_plan_created`,
  `option_plan_repriced` (K4 C, „ZMĚNA“ při změně stavu varianty) a `option_plan_result`, alerty
  přechodů stavu (K5 B, #1757), endpointy `/option-plans*`, závislost matplotlib v API (K1 A).
  JSON plánu nese dvě sady cen (9:15 a 9:31 ET), každou se svým časem.
- **Výkon:** IBKR lines 0. Tasty: dva krátké snímky (9:15 a 9:31 ET) po ~520 položkách v rezervě
  ad-hoc a přes den 3 symboly.
  Minutový cyklus beze změny (při K5 B jedna čistá funkce nad dnešními bary, jen ve dnech
  s plánem). RAM podle K1.
- **Relevance dat:** ceny jsou měřené a nesou stáří. Model (BS z naměřené IV) počítá jen
  hypotetické ceny při SL a TP a nese štítek. Chybějící řetěz = mezera.
- **Riziko:** pravidla stojí na jednom dni (2. 11.). Zpětné přehrání (#1756) dalo z 12 plánů
  jediný obchod, právě 2. 11.; ostatní vyřadil P3 nebo zůstaly bez vstupu (po K6 B 6×). Co
  vzorek nerozhodl, měří stíny K10 B. Brána pro jakoukoli úpravu je track record
  (n ≥ 30). Úprava znamená novou `rules_version`, nikdy větší stop.
- Scénář NQ, verdikt dne a jejich golden data zůstávají beze změny.
