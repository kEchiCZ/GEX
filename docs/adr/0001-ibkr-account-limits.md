# ADR-0001: Limity IBKR účtu a chování FOP dat

**Stav:** accepted · **Datum měření:** 2026-07-16 · **Prostředí:** TWS live účet, port 7496, subskripce *CME Real-Time – North America* (USD 1.55/měs.), měřeno na ES FOP řetězci E3D (0DTE), front future ESU6, spot ≈ 7601–7605.

Řeší SPEC kap. 10 (otevřené ověřovací body 1–4). Měřicí postup: inkrementální subskripce přes `ib_async` do prvního chybového kódu.

## Naměřené hodnoty

| # | Bod | Výsledek |
|---|---|---|
| 1 | tradingClass ES weeklies | `reqSecDefOptParams` vrátil 24 chainů. Vzor: **E{týden}{A–E}** = denní weeklies po–čt + pátek dle týdne (např. E3D = čtvrtek 3. týdne), **EW{týden}** = páteční weekly, **EW** = EOM, **ES** = kvartální. Příklad nejbližších: E3D→20260716, EW3→20260717, E3A→20260720, E3B→20260721, E4C→20260722. |
| 2 | OI přes generic tick 588 na FOP | **Tick 588 nechodí vůbec** (intraday ani v ranním okně — ověřeno 2026-07-16 17:00 UTC a průběžně do 2026-07-17 10:20 UTC); historical `OPTION_OPEN_INTEREST` pro FOP končí timeoutem. **ŘEŠENÍ (2026-07-17 10:25 UTC, issue #65): generic tick 101 (call/put OI) na FOP funguje** — živá sonda na EW3 P7500 vrátila `putOpenInterest=5645` do ~20 s (`callOpenInterest` druhé strany je validní 0.0 → hodnota se musí číst podle strany kontraktu). `IbOIFetcher` proto používá tick 101 pro OPT i FOP. Engine dál zkouší denní archiv à 30 min a při selhání posílá alert `oi_missing`; frontend do té doby staví heatmapu z volume. |
| 3 | Souběžné tick-by-tick streamy | **5** — šestý `reqTickByTickData` vrátil error 10190 („Max number of tick-by-tick requests has been reached"). |
| 4 | Market data lines | **≥ 150** — 150 souběžných `reqMktData` bez error 101; skutečný strop nedosažen (líný horní odhad postačuje pro návrh). |

Vedlejší potvrzení: live top-of-book i modelové Greeks (`tickOptionComputation`) na FOP fungují s levnou subskripcí *CME Real-Time – North America*; L2 subskripce (*CME Real-Time (NP,L2)*) není potřeba — aplikace hloubku trhu nepoužívá.

## Rozhodnutí (promítnuto do defaultní konfigurace)

- `GEXLENS_TICK_BY_TICK_MAX_STREAMS=5` — HotZoneCollector (issue #8) musí hot zónu degradovat z cílové šířky ATM±15 na počet pokrytelný 5 streamy (efektivně ~ATM±1 × C/P) a stav reportovat do UI (SPEC 3.4 s degradací počítá). Zbytek hot zóny klasifikuje CumΔ přes 1min midpoint test (SPEC 4.5, větev „zbytek řetězce"). Uživatel může limit zvýšit dokoupením IBKR Quote Booster packů — pak stačí zvednout env proměnnou.
- `GEXLENS_BATCH_SIZE=80` — bezpečně pod ověřenými ≥150 lines; ponechává rezervu pro trvalé streamy hot zóny a podkladu.
- OIArchiver (issue #9) použije primárně ranní `reqMktData` snapshot s generic tickem **101** (funguje pro OPT i FOP; 588 na FOP nedodává — viz bod 2).

## Důsledky

- Plná tick-by-tick klasifikace agresora dle R2 je na tomto účtu dostupná jen pro velmi úzké ATM pásmo; přesnost CumΔ mimo něj je dána midpoint testem po minutách. Není to porušení SPEC (3.4/4.5 degradaci definují), ale je to známé omezení přesnosti.
- Při změně subskripcí/účtu přeměřit (skript je jednorázový, postup popsán výše) a aktualizovat tento ADR + `.env.example`.
