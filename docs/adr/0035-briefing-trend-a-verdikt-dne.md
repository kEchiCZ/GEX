# ADR-0035: Trend napříč timeframy a verdikt dne v Briefingu

**Stav:** navrženo (2026-09-09, issues #1089, #1090, #1091 — zadal uživatel; pravidla
a váhy níže jsou volba implementace, čeká na potvrzení, PR nesou `needs-decision`).
Implementováno: §1–2 v PR #1092 (#1089), §3–4 v PR #1093 (#1090); vyhodnocení §3 = #1091 (engine doplní po settle výsledek US open → settle v bodech i EM, `GET /briefing/verdicts/stats`, karta ve Stats; brána n ≥ 30, pak návrh vah); zmrazení verdiktu od US openu = #1585; verdikt počítá a zapisuje jen engine = #1595 (dodatek 2026-10-17).
**Kontext:** Briefing (#674) říká TYP dne (gamma režim, volatilita), ale ne **v jakém
trendu trh je**, a nenabízí odpověď na „čekat spíš long, nebo short den". SPEC v2.0
pojem trendu podkladu nezná — jediné směrové čtení je indikátor tendence (#350),
který kombinuje opční positioning, ne cenovou strukturu.

## Rozhodnutí

### 1. Svíčky vyšších timeframů skládá API, ne engine
Bary podkladu drží engine ~2 roky (`derived/{sym}/bars`, viz ADR-0028), takže
denní i týdenní svíčky vznikají ve čtecí vrstvě (`GET /candles/{sym}?tf=`) bez
nového sběru — jediný zdroj dat zůstává IBKR (R6). Denní svíčka = Globex seance
(ADR-0023: [17:00 CT D−1, 17:00 CT D)), intradenní koše 15/60/240 min se zarovnávají
na otevření seance, týden z denních podle ISO týdne. Rozdělaná svíčka nese `partial`.

### 2. Trend per timeframe = struktura + EMA, čtení shora dolů
Per timeframe **W, D, 4h, 1h, 15m** se hodnotí nezávisle:
- **struktura trhu** z fraktálových pivotů (šířka 2 pro W, 3 jinak): poslední dva
  swing highs i lows rostou = HH/HL (rostoucí), klesají = LH/LL (klesající),
  jinak bez trendu; rozdělaná svíčka se do pivotů nepočítá;
- **EMA20/EMA50** z close: rostoucí = cena nad EMA20, sklon EMA20 za 5 svíček > 0
  a EMA20 nad EMA50 (je-li k dispozici); klesající zrcadlově; jinak bez trendu.

Verdikt TF: **silný** = struktura i EMA souhlasí; **slabý** = směr říká jen jedna;
struktura proti EMA = bez trendu (korekce uvnitř trendu). Pod 20 svíček se nic
nedosazuje — řádek říká „málo dat" (zásada ADR-0028).

**Vyšší TF (W, D) určuje směr, nižší (4h, 1h, 15m) načasování.** Při rozporu
týdne a dne rozhoduje den (týden je kontext, den obchodní rámec) a čtení to přizná
jako korekci. Očekávaný směr z trendu = směr vyššího TF; vyšší bez trendu = bez
převahy, i když nižší TF směr mají.

### 3. Verdikt dne je průhledné hlasování s pevnými vahami (varianta a)
Zvažované varianty: (a) pevné váhy, každý hlas s důvodem — průhledné, jde
okamžitě vysvětlit i vyvrátit; (b) váhy z track recordu — poctivější, ale bez
dat zatím není z čeho; (c) žádný verdikt, jen složky — nesplní zadání.
**Zvoleno (a) se závazkem přejít na (b)**, jakmile vyhodnocení (#1091) dodá n ≥ 30.

| složka | hlas |
|---|---|
| trend vyšších TF (W, D) | ±2 podle směru; bez trendu 0 |
| trend nižších TF (většina 4h, 1h, 15m) | ±1 |
| gamma režim | negativní gamma: +1 ve směru trendu (momentum); pozitivní: skóre se přitáhne k nule o 1 (tlumení) |
| tendence (#350, poslední pásmo) | Long/Short ±1, Strong ±2 |
| sentiment (potvrzený stav) | RiskOn +1, RiskOff −1 |
| overnight vs. včerejší close | nad +1, pod −1 |
| ΔOI přes noc | převaha call/put ±1, jen když \|Δcall − Δput\| ≥ 10 % většího z totálů |
| High-impact zpráva před US openem | verdikt „počkat na tisk" (převaha se ukáže až po něm) |

Skóre ≥ +3 = spíše long den, ≤ −3 = spíše short den, jinak bez převahy. Verdikt
se **ukládá** (`briefing_verdicts`: seance, symbol, skóre, hlasy, verze pravidel),
aby šel vyhodnotit proti průběhu seance (#1091). Bez uložení by šlo o názor bez
zpětné vazby — to projekt nedělá (srov. ADR-0033, ADR-0034). *Od US openu je verdikt
zmrazený: API zápis i založení odmítne (409), frontend po openu neposílá (#1585, vlastník
16. 10.); zápis z enginu v open − N (varianta D) je otázka triáže.* → Rozhodnuto 17. 10.:
zapisuje jen engine, POST odstraněn (dodatek 2026-10-17, #1595).

### 4. Úrovně obratu a zprávy dne
Jeden seznam úrovní seřazený podle vzdálenosti od ceny: gamma (flip, zdi,
těžiště), referenční (PDH/PDL/PDC, ONH/ONL), ±EM a denní EMA20; konfluence dvou
úrovní do 0,1 % ceny se zvýrazní. Zprávy dne s časem v Europe/Prague; očekávaná
reakce = směr z konvence řady (`gexlens_news.conventions`, #462) + medián |ret|
naměřených reakcí téže kategorie (ADR-0031); pod 10 měření „bez měřené reakce".

## Důsledky
- Nový endpoint `/candles` (SPEC kap. 6); frontend `instrument/trend.ts`
  a `instrument/daysummary.ts` jsou čisté funkce s testy (hlasování verdiktu z `daysummary.ts`
  od #1595 zaniklo — verdikt počítá jen engine, viz dodatek 2026-10-17).
- Karta Trend a karta Shrnutí dne v Briefingu; plán do deníku nese tytéž řádky.
- Verdikt je heuristika. Manuál to říká výslovně a odkazuje na vyhodnocení (#1091);
  do té doby se váhy nemění bez ADR dodatku.

## Dodatek 2026-10-17: verdikt dne počítá a zapisuje jen engine (#1595)

**Stav:** přijato (vlastník 17. 10. 2026, #1595; #1385 Rozhodnuto).

Do #1595 verdikt §3 počítal a přes `POST /briefing/verdicts` ukládal frontend, a to jen při
otevřeném Briefingu — řádek seance vznikl, jen když ho někdo před openem otevřel. Engine měl
pro automatický scénář (#1173) vlastní port pravidel (`compute/dayverdict`) s jinými vstupy:
PDC, cena, expirace i počty svíček se lišily. PDC frontendu (poslední Globex bar 16:59 ET) se
12.–16. 10. lišilo od RTH close až o 6,25 b. (ES) a 62,75 b. (NQ) a ve 2 z 10 seancí dalo
opačný hlas overnight; v pondělí PDC nemělo vůbec (nedělní partice, #1596).

1. **Kdo zapisuje:** jen engine — `DayVerdictWriter` (`verdict_writer.py`) v minutovém cyklu
   každého symbolu s pipeline (`GET /status` → `symbols`). `POST /briefing/verdicts` je
   odstraněn (405); API verdikt jen čte (`GET /briefing/verdicts`, `/stats`). Briefing ukazuje
   uložený řádek a nic nepočítá; bez řádku ukáže stav (mezeru), žádný náhradní výpočet.
   Symbol bez pipeline (ad-hoc pohled, symbol nad limitem instrumentů) verdikt nemá.
2. **Kadence (a1):** v obchodní seanci od jejího začátku (17:00 CT předchozího dne) do US openu
   9:30 ET — do open − 15 min každých 5 min (minuty :00, :05, …), v [open − 15 min, open)
   každou minutu. Upsert jednoho řádku (`session_date`, `symbol`): `created_at` = první zápis,
   `updated_at` = poslední; **platí poslední stav před openem**.
3. **Stráž:** `settle.verdict_writable(session, now)` = `is_trading_session(session)` ∧
   now < `us_open_ts(session)` — jediná stráž zápisu verdiktu. Ověřuje ji úložiště
   (`BriefingVerdictRepository.upsert_verdict`) skutečným časem zápisu, ne minutou cyklu
   (cyklus 9:29 může doběhnout po openu). Od openu se řádek nemění; `outcome_*` doplní
   `BriefingVerdictCollector` po settle + 15 min. Víkend a svátek NYSE: žádný řádek.
4. **Neúplný výpočet** (selhalo čtení API, runtime nemá levels nebo tendenci — třeba první
   minuta po restartu) uloží řádek jen tehdy, když žádný není; úplný řádek nepřepíše. Bez
   platné ceny (≤ 0, NaN) se minuta nezapíše. Čtení vstupů z API má strop 20 s (běžně ~2,4 s
   na symbol); po něm se minuta nezapíše — visící API nesmí zdržet minutový cyklus sběru dat.
5. **Vstupy (b)** — definice, kterou tabulka §3 nedávala:
   - PDC („včerejší close") = RTH close předchozí obchodní seance: close posledního 1m baru
     před settle 16:00 ET, při zkrácené seanci 13:00 ET (#1241 bod 5);
   - cena = živý spot enginu (IBKR last, fallback tasty);
   - gamma a ΔOI z aktivní expirace enginu (`runtime.expiry`), ne z volby v hlavičce Briefingu;
   - svíčky trendu **W 80 / D 120 / 240 120 / 60 120 / 15 120** — jedna tabulka pro engine
     (`scenario_auto.CANDLE_LIMITS`) i kartu Trend (`frontend/src/api/briefing.ts`); délka okna
     mění start EMA (průměr prvních svíček), proto patří k definici pravidel.
6. **Verze pravidel:** `VERDICT_RULES_VERSION` zůstává **3**, původ řádku rozlišuje datum
   seance. Řádky do seance 16. 10. 2026 včetně psal frontend se svými vstupy (PDC = poslední
   Globex bar, cena = close 1m baru, expirace z hlavičky, svíčky W 80 / 240 90, jen při
   otevřeném Briefingu); od seance 19. 10. 2026 engine. Verze 4 přijde s bumpem v6 (#1403,
   ADR-0057 bod 8). Paritu pravidel s dosavadní TS implementací drží golden
   `engine/tests/golden/dayverdict_1595.json`; texty důvodů zůstávají (plná cena, „83 %",
   ΔOI zaokrouhlené jako JS `Math.round`).
7. **Automatický scénář (d1)** převezme verdikt zapisovače z téže minuty (API se čte jednou).
   Zůstává jeden na seanci, ale vznikne v **první minutě svého okna s úplným verdiktem**
   (`GEXLENS_SCENARIO_AUTO_MINUTES_BEFORE_OPEN`, výchozí 15) — dřív jeden pokus v první minutě
   okna i při výpadku API. Při okně delším než 15 min vzniká před open − 15 min jen na
   pětiminutových značkách (zapisovač tam počítá každých 5 min). Hodnota 0 vypne scénář, ne
   zápis verdiktu. Směr scénáře je verdikt
   minuty jeho vzniku; uložený verdikt (poslední před openem) se od něj může lišit.
8. **Bez zpětného dopočtu:** seance 29. 9.–16. 10. se nedopočítávají.
