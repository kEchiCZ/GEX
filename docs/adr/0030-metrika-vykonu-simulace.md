# ADR-0030: Metrika výkonu simulace — denní ΣR, USD simulace a anualizovaný Sharpe (#794 fáze 0)

**Stav:** přijato (2026-08-21; rozhodnutí uživatele v #794: metrika „obojí od
začátku, cílit rovnou na produkční řešení")

## Kontext

Epic #794 zavádí kontinuální zlepšování setupů s cílem **Sharpe ratio > 2**.
Sharpe se dosud nikde nepočítal — track record měl hit rate, Wilson LB a Ø R
(`compute/setupstats.py`), žádnou equity ani volatilitu výnosů. Tohle ADR
definuje metriku, nad kterou se cíl vyhodnocuje a proti které bude
optimalizační smyčka (fáze 3+) porovnávat kandidáty.

## Rozhodnutí

### 1. Primární metrika smyčky: denní ΣR

- **Trade výnos** = `outcome_r` uzavřeného setupu (target/stop/timeout), jen
  řádky **aktuální mechaniky**. Aktuální verze se odvozuje dynamicky jako
  nejvyšší `mechanics_version` v datech — engine jinou nevyrábí a natvrdo
  zapsaná konstanta ve frontendu už jednou zastarala (v2 vs. v4, týden
  vyřazených statistik).
- **Portfolio** = všechny obchodované instrumenty (dnes ES + NQ) rovným dílem
  1R na setup, bez stropu souběhů — přesně to, co simulace generuje.
- **Den** = obchodní seance [17:00 CT D−1, 17:00 CT D) dle #512
  (`sessionDateIso`); uzavřený setup patří do seance svého `closed_ts`.
- **Denní výnos** = ΣR přes setupy uzavřené v seanci; seance bez uzavření
  = 0 se do řady NEpočítá (dny bez obchodu nejsou výnos 0 se štěstím, ale
  absence pozorování — nula by uměle snižovala volatilitu).

### 2. Sharpe

- **Anualizovaný**: mean(denní ΣR)/std(denní ΣR, ddof=1) × √252. Bezriziková
  sazba se neodečítá — výnosy jsou v R (bezrozměrné násobky rizika), ne
  kapitálové výnosy.
- Okna: **celá aktuální mechanika** + **klouzavých 30 seancí**.
- **Poctivost vzorku (závazné)**: UI vždy ukazuje počet seancí a do
  ~60 seancí explicitní upozornění, že číslo je orientační. Statistické
  potvrzení „SR > 2 vs. SR 0,5" vyžaduje řádově **400+ seancí** (SE
  anualizovaného SR ≈ √252/√N) — do té doby je Sharpe ukazatel trendu,
  ne splněný cíl. Gate #794 se vyhodnocuje nejdřív na 60 seancích jedné
  mechaniky, definitivně na 400+.

### 3. USD simulace (paralelní větev, produkční realismus)

- **Sizing** = kalkulačka #679: riziko $ = účet × % rizika (Settings →
  Trading, jen v prohlížeči); kontrakty = floor(riziko / (stop v bodech ×
  hodnota bodu)) **micro variant** (MES/MNQ — plný kontrakt je s malým účtem
  0×). Trade s 0 kontrakty se přeskočí a reportuje zvlášť (skipped) — floor
  a přeskoky jsou přesně ta realita, kterou R-řada nevidí.
- **Náklady** per kontrakt round-trip, odečtené z P/L každého obchodu:
  komise 2 × 0,62 $ (IBKR micro futures, fixed tier) + slippage 1 tick na
  stranu. Defaulty: **MES 2,49 $** (tick 1,25 $), **MNQ 1,74 $**
  (tick 0,50 $), jiné 2,50 $. Hodnoty jsou konstanty v
  `frontend/src/setups/performance.ts` — až bude skutečná exekuce, nahradí
  je měření (revize tohoto ADR).
- **Equity** = kumulativní P/L $ od startu aktuální mechaniky nad účtem
  z kalkulačky; denní USD řada → USD Sharpe týmž vzorcem.

### 4. Kde se počítá

Ve **frontendu** (`setups/performance.ts`, čisté funkce + testy) nad
`GET /setups/{symbol}` pro všechny symboly watchlistu. Důvody: sizing i účet
žijí jen v prohlížeči (#679 — na server se neposílají), R-řada je
z existujících dat odvoditelná bez nového endpointu a engine se nemění
(běží shadow sběr M7). Až smyčka fáze 3 bude potřebovat Sharpe na serveru
(porovnávání kandidátů), převezme definici z tohoto ADR — vzorec je záměrně
triviální a testy jsou kontrakt.

## Důsledky

- Stránka Statistiky dostává sekci Výkon setupů: equity křivka (R i USD),
  Sharpe celkem + 30 seancí, max drawdown, počty seancí/obchodů a poznámka
  o vzorku.
- Oprava vedlejšího nálezu: `CURRENT_MECHANICS_VERSION = 2` ve frontendu
  (zastaralá vs. engine v4) → dynamické odvození; statistiky Setupů zase
  vidí aktuální setupy.
- Reset track recordu (#794 ad 3) metriku nezasáhne — řada prostě začne
  znovu od první uzavřené seance nové mechaniky.

## Dodatek 2026-09-28: výpočet se stěhuje na server (#1319)

Oddíl 4 („Kde se počítá") přestal platit. Frontend počítal metriku nad
odpovědí `GET /setups/{symbol}`, která má strop 200 řádků pro tabulku —
Sharpe, equity, EV i USD simulace tak byly klouzavé okno posledních 200
setupů na symbol, ne celá historie. Rozhodnutí uživatele (varianta B, #1319):
souhrn z celé historie počítá server, UI jen vykresluje.

- Vzorce tohoto ADR beze změny, jen jinde: čistá funkce
  `engine/compute/setup_summary.py` (denní ΣR per seance, Sharpe celkem/30,
  max DD, EV, USD simulace) za `GET /setups/summary`; testy přeneseny do
  `engine/tests/test_setup_summary.py`. `frontend/src/setups/performance.ts`
  zrušen.
- Konstanty nákladů (MES 2,49 $, MNQ 1,74 $, jiné 2,50 $) a mapování na mikro
  jsou v `compute/setup_summary.py`; mapování mikro zrcadlí kalkulačku
  pozice ve frontendu (`instrument/position.ts`).
- Účet a % rizika z kalkulačky #679 se dál **neukládají** na server — posílají
  se jen jako parametry dotazu (`sim_account_usd`, `sim_risk_pct`) a bez nich
  se USD simulace nepočítá.
- Aktuální mechanika = `SETUP_MECHANICS_VERSION` enginu, ne maximum v datech.

## Dodatek 2026-10-30: náklady z Nastavení podle instrumentu, skluz na stranu a brána nad čistým R (#1402, #1326)

Rozhodnutí vlastníka 30. 10. 2026 v #1385 (poplatky v Nastavení bez ×10 podle instrumentu,
skluz = A, brána = A; kritérium brány #1334 = A z 28. 10.). Oddíl 3 („Náklady“) a konstanty
z dodatku 2026-09-28 dál platí jen pro mechaniku do v5.

- **Poplatky jsou parametry, ne konstanty.** `SetupParams` (ADR-0033) nese dva poplatky za
  kontrakt a obchod (round-trip), reálně a bez ×10 (ADR-0038 dodatek 2026-10-30), podle ceníku
  IBKR (USA, do 1 000 kontraktů měsíčně):
  - `fee_per_contract_usd` pro ES a NQ: komise 0,85 + CME 1,38 + NFA 0,01 = 2,24 $ za stranu,
    **4,48 $** za obchod;
  - `fee_per_micro_contract_usd` pro MES a MNQ (i M2K, MYM): 0,25 + 0,35 + 0,01 = 0,61 $ za
    stranu, **1,22 $** za obchod.

  Komise 2 × 0,62 $ z oddílu 3 je tím nahrazená. Mění se v Nastavení → Risk management jako
  nová verze parametrů s důvodem; záporný poplatek je chyba 422.
- **Který poplatek** (`setup_summary.contract_fee_usd`): mikro kořen bere pole mikra, ostatní
  pole plného kontraktu. Platí pro souhrn setupů (1 kontrakt), `fee_usd` v kontextu setupu
  (účet) a paper (ADR-0040), který novou verzi převezme i za běhu.
- **Náklad mikra od mechaniky v6** (`setup_summary.round_trip_cost_usd`, přepínač
  `NET_R_GATE`): poplatek mikra z parametrů + skluz **1 tick na každou stranu**
  (`compute/paper.TICK_SIZES` × bod mikra), tedy MES 1,22 + 2 × 1,25 = **3,72 $** a MNQ
  1,22 + 2 × 0,50 = **2,22 $**. Kód tím odpovídá textu oddílu 3 (skluz na stranu); konstanty
  v5 (MES 2,49 $, MNQ 1,74 $) nesly 1 tick na celý obchod (#1326). Bez poplatku mikra náklad
  v6 nejde spočítat (`ValueError`), tichý default není.
- **Jedna konvence pro bránu i důkaz** (#1326 varianta A): od v6 počítají s týmž nákladem
  brána šablon (ADR-0038 bod 4, kritérium A z #1334: dolní mez Ø čistého R > 0 při n ≥ 30 za
  60 seancí; vstup `setup_library.gate_results`), n potřebné, ØR čistě a čistý P/L buňky
  v Knihovně i v pushi a USD simulace souhrnu. Na ES se stopem 2,4 b stojí náklad 0,31 R
  na obchod; na syntetickém vzorku golden (`engine/tests/golden/template_gate_net_1402.json`,
  všechny stopy 2,4 b) to dělá z Ø +0,28 R hrubě −0,03 R čistě. V reálném vzorku #1326
  zůstal ES stín v R kladný (+0,28 → +0,10 R) a znaménko měnily dolary.
- **Souhrn** dál ukazuje R hrubě a poplatky zvlášť (hrubý − poplatky = čistý), R se náklady
  nesnižuje.
- **Mechanika v5 beze změny:** brána nad hrubým R, ØR čistě s konstantami v5. Skripty
  měření nad v5 (`scripts/measure_template_gate_1334.py`, `scripts/measure_band_gate_1064.py`)
  jsou na konstanty připnuté (`per_side=False`), aby šly zopakovat i po bumpu.

Souvisí: #794 (epic, rozhodnutí), #679 (sizing), #512/#748 (seance),
#311 (mechanics_version), ADR-0029 (učicí data).
