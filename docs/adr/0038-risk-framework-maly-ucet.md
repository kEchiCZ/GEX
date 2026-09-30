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
