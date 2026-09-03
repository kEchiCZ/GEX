# ADR-0032: Klasifikace agresora per trade z dxFeed TimeAndSale místo IBKR tick-by-tick hot zóny

**Stav:** accepted · **Datum:** 2026-09-03 · **Rozhodl:** uživatel (issue #1006, #615) · **Mění:** SPEC kap. 0 R2, kap. 3.4, 4.5, 5.1

## Kontext

SPEC R2 požadoval CumΔ s plnou klasifikací agresora: tick-by-tick pro „hot zónu"
ATM ±15 strikes (IBKR `reqTickByTickData`, Lee–Ready klasifikace) a midpoint test
pro zbytek řetězce. ADR-0001 změřil limit účtu **5 souběžných tick-by-tick
streamů**, což hot zónu degradovalo na ~ATM ±1.

Při plánování fáze 3 #615 (3. 9. 2026) se ukázalo, že **hot zóna nikdy nebyla
v provozu**: `HotZoneCollector` (#8, PR #38, 16. 7. 2026) vznikl nad mockem,
integrační vrstva enginu (PR #61, týž den) napojila jen minutovou midpoint větev
a produkční adaptér pro `reqTickByTickData` nikdy nevznikl. `CumDeltaTracker.add_trade`
neměl volajícího, `ticks/` se nikdy nezapisovalo, přepínač `hot_zone_width` v Settings
nic neřídil. Nebylo to rozhodnutí — mezera bez issue, která přežila obě triáže
(#582, #629), protože termín „hot zóna" se mezitím používal pro pásmo ATM ±15
snapshotů, které funguje (#547, #609). Podrobnosti a důkazy v #1006.

Mezitím fáze 0–2 epicu #610 přinesly dxFeed `TimeAndSale` přes tastytrade DXLink
(ADR-0025, ADR-0027): každý outright trade nese `aggressorSide` **od burzy**
(CME MDP 3.0 tag 5797), bez limitu počtu streamů; měřeno 500 080 printů
(23. 8.–1. 9.), strana určená u 99,95 %.

Dvě fakta o CME datech, ověřená 3. 9. (#615):

1. **Nohy spreadů nejsou v trade streamu.** CME pro legy spread tradů nevysílá
   Trade Summary (MDEntryType=2), jen Electronic Volume update. Příznak
   `spreadLeg` je proto vždy false (potvrzeno podporou tastytrade) a žádný
   vendor — ani IBKR — leg označit nemůže, protože jako trade neexistuje.
2. IBKR `tickByTickAllLast` nese `price, size, exchange, specialConditions,
   pastLimit, unreported` — žádný příznak spreadu; čte tentýž CME feed.

## Rozhodnutí

1. **R2 se naplní z dxFeed `TimeAndSale`:** zóna ATM ±15 strikes × C/P (přepočet
   při pohybu spotu o ≥ 1 strike, přesun kontraktu mezi zónami jen na hranici
   snapshotu — ADR-0025 pravidlo 3) dostává znaménko z `aggressorSide`
   (BUY → +1, SELL → −1); trade bez určené strany → midpoint fallback pro danou
   minutu. Kontrakty mimo ±15 a celý řetěz při výpadku tastytrade větve →
   midpoint test (SPEC 4.5). Realizace = #615 fáze 3.
2. **IBKR tick-by-tick zóna se nezapojuje a ruší se:** `ibkr/hotzone.py`,
   `MockHotZoneClient`, `TICKS_SCHEMA`/`write_ticks`/`data/ticks/`,
   `GEXLENS_HOT_ZONE_WIDTH`, `GEXLENS_TICK_BY_TICK_MAX_STREAMS`, runtime setting
   `hot_zone_width` a jeho pole v Settings UI. Typy `ClassifiedTrade`/`TradeSide`
   zůstávají v `compute/cumdelta.py` jako vstup trade větve.
3. **Znění R2 ve SPEC:** „strana agresora od burzy (dxFeed TimeAndSale) pro
   ATM ±15 strikes, midpoint test pro zbytek řetězce a jako fallback bez
   tastytrade". Kap. 3.4 přepsána, 4.5 a 5.1 upraveny.
4. **Spread legy se nerozlišují** (doplněk ADR-0027, 3. 9.): CumΔ z trade větve
   je čistě outright agrese, což je pro účel CumΔ (směrový tok) žádoucí.
   Strukturovaný objem (spready, bloky) se měří zvlášť jako rozdíl snapshot
   objemu a Σ tisků — #1007.

## Důsledky

- Do dokončení #615 fáze 3 je CumΔ **100 % midpoint** — manuál a `/status`
  to říkají výslovně, místo aby tvrdily existenci tick-by-tick zóny.
- Přepnutí na trade větev mění hodnoty CumΔ (#615 „největší riziko epicu"):
  `mechanics_version` +1, paralelní běh ≥ 5 seancí s vyčíslením rozdílu,
  kalibrace prahů (#394/#434) až po něm. Default zůstává midpoint do rozhodnutí
  uživatele.
- Provoz bez tastytrade větve je degradovaný na midpoint pro celý řetěz —
  vědomě; tastytrade je od ADR-0025 trvalá větev, ne volitelný doplněk.
- ADR-0001 bod 3 (limit 5 streamů) zůstává platným měřením bez použití.

## Zamítnuté alternativy

- **Zapojit IBKR tick-by-tick pro ATM ±1** (původní plán, dodatečně napsat
  adaptér): pokryje ~2 % zóny, stranu odhaduje Lee–Ready z bid/ask, druhý zdroj
  klasifikace s vlastní degradací a UI stavem, tentýž CME feed bez spread
  příznaku. Nepřidá nic, co dxFeed nedá lépe; smysl jen jako pojistka pro provoz
  bez tastytrade, který není podporovaný scénář.
- **Heuristika spread legů** (shodný čas a velikost napříč striky): odhad bez
  možnosti ověření; zamítnuto uživatelem 3. 9. — a po zjištění, že legy v trade
  streamu vůbec nejsou, bezpředmětné.
