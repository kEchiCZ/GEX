# ADR-0038: Risk framework malého účtu — účet v jednotkách plného kontraktu, 1 % na setup, brzdy a brána šablon

- **Stav:** přijato (uživatel 15. 9. 2026, #1185 varianta A, riziko 1 %)
- **Datum:** 2026-09-15
- **Souvisí:** #1185, #679 (kalkulačka v prohlížeči), #794 (walk-forward), #1060 (brána polohy), #453, ADR-0033 (parameter store), ADR-0004 (setupy)

## Kontext

Track record setupů (1 128 uzavřených, všechny mechaniky) obsahuje 71 setupů
se ztrátou přes 1 000 $ na plný kontrakt (Σ −114 717 $ proti celkovým +13 896 $).
Všechny mají stop ≥ 32 b ES / ≥ 73 b NQ; ve v5 jsou setupy se stopem nad
stropem zároveň nejhorší (n = 29, −0,34 R, 3 % cílů) proti setupům se stopem
v rozpočtu (n = 339, +0,06 R). Uživatel bude obchodovat účet ~5 000 $ na
**MES/MNQ**, aplikace počítá v ES/NQ.

## Rozhodnutí

1. **Jednotky.** Aplikace zůstává v plných kontraktech; účet se vede jako
   **50 000 $** (10× reálný účet na mikro). 1 kontrakt v aplikaci = 1 mikro
   reálně, body 1:1, dolary ×10. `ACCOUNT_START_USD` ve frontendu 5 000 → 50 000.
2. **Sizing** (`compute/risk.py`): `contracts = ⌊účet × risk_pct / (stop b ×
   hodnota bodu)⌋`; riziko **1 %** (500 $), tvrdý strop **2 %**. Stop 10 b ES /
   25 b NQ = 1 kontrakt. Stop nad rozpočtem = `unaffordable`.
3. **Brzdy** v R obchodovatelných setupů napříč symboly: −3 R seance, −6 R
   týden (od pondělní seance), 2 stopy téže šablony za seanci. Alert
   `risk_brake` jednou per brzda a seance.
4. **Brána šablon:** obchodovatelná jen šablona s kladnou dolní mezí
   jednostranného 95% intervalu Ø R (t≈1,645) při n ≥ 30 za 60 seancí, ze
   setupů se stopem v rozpočtu (starší řádky dopočtené z entry/stop).
5. **Setup vzniká vždy** (varianta A, ne C): nese `tradeable`/`trade_block`
   v kontextu; stín je šedý, bez pushe, mimo bilanci účtu. Detektory se
   nemění — poučení zůstává „stop nezvětšovat, zpřesnit vstup".
6. Parametry jsou součástí verzované `setup_params` (ADR-0033), editace
   v Settings → Risk management s povinným důvodem.

## Důsledky

- Ztráta jednoho setupu > 1 000 $ v aplikaci (100 $ reálně) je nemožná
  z definice; simulace nad v5: max DD 19 % (1 %) vs. 39 % (2 %).
- Žádný slib zisku: edge v5 ≈ +0,03 R, brána zpočátku označí většinu setupů
  za stín. Přepnutí na 2 % až po ≥ 50 živých obchodech s edge ≥ +0,2 R;
  živě až po ~60 seancích kladné bilance obchodovatelných setupů.
- 10× zhodnocení není otázka risk managementu, ale edge (#794, #1060, #453).

## Dodatek 2026-09-29: okna brzd a vstup bez setupů po settle (#1322, #1324)

- **Okna brzd** platí podle kódu hranice (`compute/risk.brake_state`), stejnou konvencí
  polouzavřených intervalů jako obchodní den. Den je Globex seance (`session_bounds`), tedy do
  otevření Globexu v 17:00 CT, ne do settle. Týden běží od otevření pondělní seance (neděle 17:00 CT,
  `week_start`) do konce dnešní seance. Do #1322 se do týdne omylem sčítalo celé 84denní okno brány
  šablon (`_load_realized` čte širší z obou oken), takže ztráty z minulých týdnů by týdenní brzdu
  držely měsíce. Rozhodnutí bod 3 se nemění. Strop stopů šablony ji zastaví po **2.** stopu za
  seanci (text „třetí stop“ v docstringu byl chybný).
- **Vstup brzd i brány šablon** nevidí setupy vzniklé po settle vlastní expirace
  (`compute/setups.born_after_settle`, #1324). Takový setup nemohl existovat: pipeline běží nad
  vypršelým řetězem až do rollu o půlnoci UTC a timeout by setup zavřel příští minutou. Od #1324 takový
  setup nevznikne, takže mezi settle a půlnocí UTC nevznikají žádné setupy (na otevřeném Globexu 1–3 h
  denně). Roll expirace v settle, který okno odstraní, řeší #1331. Hranice je settle expirace podle
  ADR-0039 bod 2 (`expiry_settle_ts`), táž jako timeout setupu. Historické řádky zůstávají v DB
  a vyřadí je predikát při čtení. `realized_day_r` a `realized_week_r` v kontextu starších setupů se
  nepřepisují, protože jsou záznamem stavu v době vzniku (`realized_week_r` před #1322 je tedy
  84denní součet, ne týden).

## Dodatek 2026-09-30: brána per šablona × symbol (#1325, varianta C)

- **Klíč brány je šablona × symbol.** `affordable_results` bere jen řádky vlastního symbolu (tickeru
  instance, ADR-0041 bod 3). Řádky před pravidly bez `affordable` dopočítá hodnotou bodu vlastního
  symbolu (`runtime.multiplier`, ES 50 $, NQ 20 $), tedy touž hodnotou, se kterou počítá sizing kandidáta.
  Kritérium bodu 4 se nemění: dolní mez Ø R > 0 při n ≥ `template_gate_min_samples` (30) za
  `template_gate_days` (60) seancí, ze setupů se stopem v rozpočtu a bez setupů po settle (#1324).
  Brzdy (bod 3) včetně stropu stopů šablony zůstávají napříč symboly, protože chrání účet, ne edge.
- **Pinovaný kontrakt má vlastní buňku.** Klíčem je ticker, ne kořen produktu, stejně jako u partic,
  setupů, statistik a kalibrace confidence (ADR-0041 bod 3 a Důsledky). `ESZ6` proto nepřebírá vzorek
  `ES` a začíná na n = 0. Dokud nenasbírá `template_gate_min_samples` vlastních uzavřených setupů
  šablony, je jeho brána `insufficient` a všechny jeho setupy jsou stín. Pinovaný kontrakt obvykle
  žije krátce (roll týden, ADR-0039), takže v praxi zůstane celý stínový. Klíč podle kořene
  (`symbol_root`) by vzorek i hodnotu bodu sdílel, ale šel by proti ADR-0041 bod 3.
- **Proč.** Brána do #1325 stála na slovníku `point_values`, o kterém komentář tvrdil, že ho instance
  `SetupEngine` sdílejí. Nesdílely: každá instance dostala vlastní prázdný slovník a plnila jen svůj
  symbol. Brána proto brala řádky s `affordable` z obou symbolů, ale dopočtené řádky před pravidly jen
  z vlastního. ES a NQ tak tutéž šablonu ve stejnou chvíli hodnotily nad jiným vzorkem: NQ pass při
  LB +0,004 (n = 226), o 5 minut později ES block při LB −0,027 (n = 234). Verdikt NQ kmital,
  25. 9. se za 75 minut třikrát přepnul. Bránou prošla jen NQ trend_continuation, a to jen díky této
  chybě. K 30. 9. 2026 06:47 UTC je obchodovatelných setupů v5 osm: 1339, 1343, 1384, 1397, 1412,
  1414, 1416 a 1418. Dva z nich (1397, 1414) vznikly po settle, takže podle #1324 nemohly existovat.
  Zbylých šest skončilo stopem (−6 R, −2 090 $ včetně poplatků). Replay brány po #1325 v okamžiku
  vzniku dává u všech osmi block (n = 123–143, LB −0,095 až −0,052). Do nasazení #1325 mohou další
  přibýt. Rozbor v #1323 (29. 9.) pracoval s prvními čtyřmi (−1 085 $). Oprava samotného sdílení
  (varianta B) by dnes zavřela bránu všem šablonám. Klíč po symbolech odpovídá datům: ES a NQ se
  u téže šablony chovají jinak (medián stopu v bráně 2,4 b proti 16,8 b; ES nepřežije skluz 1 tick,
  NQ ano).
- **Sdílený `point_values` zrušen** jako balast. Brána cizí symbol nečte a hodnotu bodu vlastního
  zná instance z `runtime.multiplier`. Souhrn setupů (#1319) má vlastní tabulku hodnot bodu
  (`compute/paper.POINT_VALUES`), změna se ho netýká.
- **`risk_rules_version` = 2.** Změnil se význam `template_gate`, `template_gate_n` a
  `template_gate_lb` a s nimi i `tradeable` v kontextu setupu. Řádky s verzí 1 nesou verdikt staré
  logiky a nepřepisují se, protože jsou záznamem stavu v době vzniku. Verze je jediný způsob, jak je
  v analýze oddělit.
- **Důsledek: dnes block na obou symbolech.** K 30. 9. 2026 06:48 UTC (po vyřazení setupů po settle)
  je jediná šablona s dostatečným vzorkem, trend_continuation, v bloku na ES (n = 189, LB −0,107)
  i na NQ (n = 144, LB −0,079). Ostatní šablony mají n < 30 (insufficient). Bez ručního zásahu tedy
  nevzniknou obchodovatelné setupy ani jejich pushe (stín do pushe nejde) a bilance účtu stojí.
  Přebití brány pro vybranou kombinaci šablona × symbol navrhuje #1323, které zatím čeká na
  rozhodnutí. Do té doby jde bránu jen vypnout celou (Settings → Risk management,
  `template_gate_enabled`). Obchodovatelné jsou pak všechny šablony na všech symbolech se stopem
  v rozpočtu, chrání jen sizing a brzdy (varianta E z #1325 s jejími nevýhodami). Kritérium se zpětně
  neladí: LB > 0 se přehodnotí walk-forwardem po ≥ 20 seancích od 30. 9. (~28. 10., připomínka #1334),
  s náklady ADR-0030 (komise a skluz 1 tick).

## Dodatek 2026-10-01: stádia buněk, Zkouška a Knihovna setupů (#1323, fáze 1)

- **Rozhodnutí uživatele 1. 10. 2026** (otevřené otázky #1323): ovládání v Setupy → Knihovna (1B);
  přebití brány jen jako **Zkouška s rozpočtem** (2B: výchozí 10 setupů nebo −3 R, meze 1–20 setupů
  a 0,5–6 R, obnovitelná s důvodem); po vyčerpání zpět na Auto bez zápisu verze (3A); zaškrtávátko
  `template_gate_enabled` z UI pryč, pole zůstává (4B); verdikt brány v Knihovně se počítá teď týmiž
  funkcemi jako engine (5B); klíč buňky je ticker instance (ADR-0041), brzdy mají přednost před
  rozhodnutím uživatele a rozsah kouče (#1244) se řeší zvlášť (8). Původní „obchodovat" bez stropu
  z prvního zadání #1323 se nezavádí.
- **Stádium buňky ticker × šablona.** Auto (výchozí, buňka bez záznamu: sizing → brzdy → brána),
  Stín (setup vzniká a měří se, nikdy není obchodovatelný, `trade_block = "user"`) a Zkouška (přebije
  verdikt brány, dokud nevyčerpá rozpočet). Stádia jsou pole `SetupParams` (`shadow_cells`,
  `trial_cells` se začátkem a rozpočtem per buňka; výchozí rozpočet `trial_budget_setups` = 10
  a `trial_budget_r` = 3), takže každá změna je nová verze `setup_params` s povinným důvodem
  (ADR-0033, dodatek 2026-10-01). Klíč `NQ:trend_continuation` neplatí pro pinovaný `NQZ6`, ten má
  vlastní buňku. Vyřazení celé šablony zůstává `disabled_templates` (přestane se měřit).
- **Pořadí bloků:** sizing (`stop_over_budget`, `stop_over_cap`) → brzdy (`daily_brake`,
  `weekly_brake`, `template_stops`) → `user` (Stín) → brána (`gate`). Aktivní Zkouška přebije jen
  verdikt brány `block` nebo `insufficient` a nastaví `gate_overridden = true`. Sizing ani brzdy
  nepřebije žádné stádium. Brána dál bere i stínové řádky (na `tradeable` se nefiltruje), takže
  stádium vzorek nezkresluje.
- **Čerpání zkoušky** (`compute/risk.trial_usage`, čistá funkce): setupy buňky s `gate_overridden`
  vzniklé od začátku zkoušky, uzavřené z `realized_since` a otevřené z paměti instance; ztráta je Σ R
  uzavřených. Vyčerpáno = počet ≥ N nebo Σ R ≤ −X. Setup obchodovatelný z vlastní brány (pass) ani
  setup zastavený sizingem nebo brzdou rozpočet nečerpá. Na šablonu × symbol je otevřený nejvýš jeden
  setup, rozpočet se tak může přečerpat nejvýš o něj. Selže-li čtení výsledků, Zkouška bránu
  nepřebije — výpadek DB nesmí riziko zvýšit.
- **Kdy zkouška platí** (`TrialCell.in_force`): od `started_at` a jen na mechanice, na které
  začala. Cyklus enginu má čas zaokrouhlený na minutu, API razí začátek přesně: setup z cyklu,
  ve kterém se nová verze aplikovala, by zkouška pustila, ale čerpání (setupy od `started_at`)
  by ho nikdy nezapočetlo — strop by neplatil (review 1. 10. 2026). Invariant: `gate_overridden`
  ⇒ `created_ts` ≥ `started_at`; zkouška platí od příští minuty. Čerpání se čte jen z aktuální
  mechaniky (#311), takže zvednutí `SETUP_MECHANICS_VERSION` by vyčerpané zkoušce vrátilo plný
  rozpočet bez rozhodnutí uživatele; zkouška proto nese `mechanics_version` a na jiné mechanice
  končí (Auto, bez zápisu). Pro novou mechaniku ji uživatel vědomě obnoví — riziko systém jen
  snižuje.
- **Asymetrická autonomie** (stupeň 1, #794, ADR-0033 bod 5): riziko zvyšuje jen člověk s důvodem
  (nová verze parametrů). Systém ho smí jen snížit — konec zkoušky a brzdy se odvozují z výsledků
  a do `setup_params` nic nezapisují. Konec zkoušky ohlásí alert `setup_stage` (`event =
  trial_spent`) jednou na zkoušku, na přechodu do vyčerpání (vznik setupu, který doplní počet, nebo
  uzavření, které dosáhne ztráty), takže odejde jednou i po restartu enginu bez stavu v paměti.
- **Zápis stádia jen přes `POST /setups/stage`** (jedna buňka, povinný důvod) — **odchylka od
  zadání #1323** („žádný nový endpoint“, zápis přes `saveSetupParams`), předložená uživateli
  k potvrzení v PR; varianta A (zápis přes `/setups/params` se začátkem od serveru) zůstává
  možná. Začátek a mechaniku zkoušky nastaví server („teď“, `SETUP_MECHANICS_VERSION`);
  opakované zahájení je obnovení s rozpočtem od nuly. `POST /setups/params` stádia nemění
  (chybějící klíč převezme platnou verzi, jiná hodnota vrátí 422): klient by jinak mohl zvolit
  budoucí začátek (zkouška bez stropu) a starý snímek parametrů (Settings otevřené před změnou
  stádia) by stádia tiše vrátil; návrhy walk-forwardu stádia nenesou. Náhradní
  důvod „risk: změna ze Settings" v UI je zrušený, prázdný důvod odmítne UI i API.
- **Kontext setupu, `risk_rules_version` = 3.** Nové klíče `user_stage` (efektivní stádium při
  vzniku; vyčerpaná zkouška = `auto`), `gate_overridden`, u buňky se zkouškou `trial_started_at`,
  `trial_budget_setups`, `trial_budget_r`, `trial_setups` (čerpání včetně tohoto setupu, pokud ho
  zkouška pustila) a `trial_sum_r`, a důkaz buňky nad vzorkem brány: `template_gate_avg_r`,
  `template_gate_avg_net_r` (po nákladech ADR-0030) a `template_gate_n_needed`. `tradeable` může
  být u v3 true i při bráně `block`. Řádky v1 a v2 tyto klíče nemají a čtou se jako Auto bez přebití.
- **Knihovna je druhý volající, ne druhá implementace.** `GET /setups/summary` vrací `cells`
  (`compute/setup_library.library_cells`): verdikt brány spočítaný teď (`affordable_rows`
  + `template_gate`, okno `gate_window_start`), ØR hrubě a čistě, vzorek potřebný pro průkaz edge
  +0,2 R (`risk.samples_needed`: ((1,645 + 0,84) · σ / 0,2)², jednostranně 95 %, síla 80 %) a odhad
  v seancích při dnešním tempu; řadí se podle průkaznosti n / n potřebné, ne podle ØR. n potřebné
  nikdy neklesne pod minimum brány (30) a při σ = 0 pod ním se neodhaduje: stop je vždy přesně
  −1 R a capovaný cíl přesně +3 R, takže dva stejné výsledky dávaly „2 / 2 · vzorek stačí“ na
  prvním řádku. Vzorek brány je oknem `template_gate_days` useknutý: při tempu t za seanci ho
  okno pojme nejvýš t × 60 (`window_capacity`). Když je to méně než n potřebné, odhad v seancích
  se nedává a Knihovna píše „v okně nedosáhne“ — vzorec z #1323 by sliboval termín, který se
  pořád posouvá (1. 10.: ES T7 ~427 < 456, NQ T7 ~337 < 462; souvisí s přehodnocením
  kritéria #1334). Buňka nese i `net_usd`, Σ čistého P/L vzorku v reálných mikro dolarech při
  skutečném sizingu (kontrakty z kontextu setupu). Buňky s nastaveným stádiem jsou v Knihovně
  vždy, i mimo watchlist. Stav brzd účtu v hlavičce Knihovny (`brakes`, `library_brakes`)
  počítá `brake_state` nad týmž čtením `realized_since` jako engine, napříč symboly — pole
  navíc proti zadání #1323, předložené k potvrzení spolu s endpointem. Souhrn #1319 má skupinu
  `trial` (obchodovatelné díky zkoušce, podmnožina `tradeable`) a důvod stínu `user`.
- **Push** nového setupu chodí dál jen pro `tradeable` (stín uživatele push nedostane); zpráva nese
  šablonu (`T7 trend_continuation`), stádium (AUTO / STÍN / ZKOUŠKA k/N) a řádek důkazu (ØR
  hrubě a čistě, n / n potřebné, brána), který při dolní mezi ≤ 0 nebo bez ní začíná štítkem
  „edge neprokázán“ — vždy u setupu, který pustila Zkouška.
- **Globální `template_gate_enabled`** zůstává v parametrech jen jako nouzová cesta přes API; v UI
  by byl druhou cestou k „obchodovat vše bez rozpočtu". Věta předchozího dodatku o vypnutí brány
  zaškrtávátkem v Settings proto už neplatí.
- **Důsledky.** Každá změna stádia zvedá `params_version`, takže řezy track recordu „od verze N"
  zahrnou i verze, které měnily jen stádium (Historie ve fázi 2 je rozliší). Brzdy jsou společné
  napříč symboly: ztráty zkoušky mohou denní nebo týdenní brzdou zastavit i jiné obchodovatelné
  setupy — dialog změny stádia to říká. Zkouška znamená reálné peníze na neprokázaném edge
  (k 1. 10. 2026 má každá buňka bránu block nebo insufficient, rozbor v #1323); mitigace jsou
  rozpočet, štítek „edge neprokázán" (dialog i push), brzdy, které se nepřebíjejí, a čisté R
  i $ v Knihovně (ØR čistě vedle hrubého, `net_usd` v tooltipu).
  Fáze 2–4 (detail setupu, úpravy prahů s OOS ověřením, varianty, diskreční Fibonacci) jsou
  samostatná zadání.
