# ADR-0035: Trend napříč timeframy a verdikt dne v Briefingu

**Stav:** navrženo (2026-09-09, issues #1089, #1090, #1091 — zadal uživatel; pravidla
a váhy níže jsou volba implementace, čeká na potvrzení, PR nesou `needs-decision`).
Implementováno: §1–2 v PR #1092 (#1089), §3–4 v PR pro #1090; vyhodnocení §3 čeká na #1091.
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
zpětné vazby — to projekt nedělá (srov. ADR-0033, ADR-0034).

### 4. Úrovně obratu a zprávy dne
Jeden seznam úrovní seřazený podle vzdálenosti od ceny: gamma (flip, zdi,
těžiště), referenční (PDH/PDL/PDC, ONH/ONL), ±EM a denní EMA20; konfluence dvou
úrovní do 0,1 % ceny se zvýrazní. Zprávy dne s časem v Europe/Prague; očekávaná
reakce = směr z konvence řady (`gexlens_news.conventions`, #462) + medián |ret|
naměřených reakcí téže kategorie (ADR-0031); pod 10 měření „bez měřené reakce".

## Důsledky
- Nový endpoint `/candles` (SPEC kap. 6); frontend `instrument/trend.ts`
  a `instrument/daysummary.ts` jsou čisté funkce s testy.
- Karta Trend a karta Shrnutí dne v Briefingu; plán do deníku nese tytéž řádky.
- Verdikt je heuristika. Manuál to říká výslovně a odkazuje na vyhodnocení (#1091);
  do té doby se váhy nemění bez ADR dodatku.
