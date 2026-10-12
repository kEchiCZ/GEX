# Baseline výkonu grafu (E-2a.2, #1412)

Změřený výkon heatmapy na notebooku vlastníka v RTH s produkčními daty, za běhu produkce.
Čísla jsou vstupem tří rozhodnutí:
- pořadí oprav E-2a.3–E-2a.9 (komentář v #1274);
- hranice načtení dne a přepnutí symbolu v ADR-0047 (E-1.5 #1391);
- přeměření po opravách E-2a.18 #1431, které použije tentýž sběrač a tytéž scénáře.

**Cíle (ot. 2 v #1385, varianta B):**
- p95 snímku ≤ 33 ms při panu a zoomu na 1m;
- přepnutí Mode a Scale < 100 ms včetně kontur;
- minutový update jako jedna úloha < 250 ms;
- živý tick < 250 ms;
- cíl, ne tvrdá hranice: medián snímku ≤ 16,7 ms na 5m.

Předchozí měření z 24. 9. (#1274) bylo ze skryté záložky bez paintu, jeho čísla jsou proto jen
dolní mez.

## Podmínky měření

| | hodnota |
|---|---|
| den a čas (UTC) | po 12. 10. 2026, RTH: studené načtení 13:34:47, aktivní bloky 14:20:44–14:25:00, pasivní okno od 14:27:08 |
| build frontendu (`perfExport().build`) | `f71392093f779e6692fba44f87e597d7c36780e3` (produkce od 12. 10. 09:52 UTC) |
| data | ES a NQ, expirace 20261012 (0DTE), obchodní den 2026-10-12; balík dne 4,7 MB, rozbaleně 28 MB |
| prohlížeč | Chrome 155, Windows 10, jediná záložka GEXLens |
| okno | maximalizované, viewport 1822 × 903, DPR 1; plátno heatmapy 1416 × 420 ve výchozím pohledu |
| obnovovací frekvence (brána) | 238 Hz (klidový snímek 4,2 ms) před každým segmentem |
| CPU | i5-10300H, 16 GB; takty jader fixně 2 396 MHz (turbo vypnuté v plánu napájení) |
| teplota a příkon CPU | package 53–67 °C, 5–12 W, zátěž CPU 12–52 %, takty 2 396 MHz (22 snímků LibreHardwareMonitoru po 2 a 5 min, 13:42–15:33) |
| kontejnery (`docker stats`) | `news-engine` v dávkách 64–99 % CPU, mezi nimi 2–6 %; `engine` 3–92 %; `api` 1–106 % (3 špičky nad 60 %); `postgres` 0–13 %; RAM systému 77–83 % |
| další zátěž | žádné buildy ani jiné kontejnery; terminál přes část okna Chrome (plátno se kreslí celé) |

## Metoda

**Měřič #1409** (`frontend/src/perf/meter.ts`) je v produkčním buildu. Ve výchozím stavu je
vypnutý a ovládá se jen z konzole (`window.gexlensPerf`). Měří:
- snímky z rAF;
- dlouhé úlohy ≥ 50 ms;
- operace `drawStatic`, `drawDynamic`, `buildModeGrid`, `renderGrid`, `minuteFlush`, `dayFetch`,
  `dayDecode` a `dayBuild`;
- spany do vykreslení: `dayLoad`, `symbolSwitch`, `modeScale` (včetně kontur) a `minuteUpdate`.

Buffer drží 30 s.

**Sběrač** `scripts/perf_baseline_1412.js` se vkládá do konzole a aplikaci nemění:
- **skládání záznamů:** každých 5 s vybere nové záznamy měřiče, takže p50 a p95 jdou přes
  libovolně dlouhé okno;
- **rozpad snímků:** snímky ≥ 50 ms rozkládá přes Long Animation Frames (LoAF) na tři části:
  - úlohy před vykreslením;
  - rAF;
  - styl, layout a paint.

  Pro každou část zapíše invoker skriptu (událost, zprávu Scheduleru Reactu, WebSocket, časovač)
  a místo v minifikovaném bundlu (`soubor:znak`). Produkce nemá sourcemapy, funkce se proto
  dohledá v `dist/` téhož buildu;
- **síť:** zapisuje časy `/api/replay` a `/news/stats` (fronta, TTFB, stažení). Buffer resource
  timing prohlížeče (250 záznamů) je po načtení stránky plný;
- **gesta:** pouští skriptovaná gesta, takže přeměření E-2a.18 je srovnatelné krok za krokem.

**Scénáře** se měří na 1m a 5m, vždy ve výchozím i Čistém pohledu (#238):

| scénář | jak | metrika |
|---|---|---|
| pan | 3× tažení 300 px tam a zpět, jeden `pointermove` na snímek (60 kroků) | snímky p50/p95 |
| wheel zoom | 3× 12 kroků kolečka dovnitř a 12 ven, jeden krok na snímek | snímky p50/p95 |
| Mode | všechny módy a zpět na výchozí | span `modeScale` (do vykreslení kontur) |
| Scale | 2× všechny škály a zpět | span `modeScale` |
| Gradient / Blobs | 3× tam a zpět | změna → po vykreslení (rAF + úloha) |
| přepnutí symbolu, studené | po reloadu na NQ první přepnutí na ES (`pb.run` začíná přepnutím na ES) | span `symbolSwitch`, fáze `dayFetch`/`dayDecode`/`dayBuild` (v pasivním okně) |
| přepnutí symbolu, teplé | ES → NQ → ES → NQ → ES, balíky dne už v paměti | span `symbolSwitch` |
| načtení dne, studené | reload stránky | navigace, resource timing `/api/replay`, bufferované LoAF |
| minutový update | pasivně, ≥ 60 min | span `minuteUpdate`, dlouhé úlohy za minutu |
| živý tick | pasivně | LoAF s invokerem WebSocket, `drawStatic` a `drawDynamic` za minutu |
| hodinový refetch | pasivně (`LIVE_RECONCILE_MS` = 1 h) | `dayFetch`/`dayDecode`/`dayBuild`, span `dayLoad` |

**Brány a pojistky:**
- **Viditelnost:** před každým segmentem musí být klidový medián snímku ≤ 17,5 ms, jinak
  segment čeká. Chrome na Windows hlásí celé zakryté okno jako `hidden` a rAF zastaví, takže
  gesto i spany by čekaly. Segment proto nese:
  - **pokrytí**: součet snímků / délka segmentu;
  - počet přechodů do `hidden`.

  Segment s pokrytím < 90 % nebo se skrytím je neplatný (`occluded`). Okno Chrome je
  maximalizované a terminál ho kryje jen zčásti, protože celé zakryté okno se nekreslí.
- **Minutový update:** segment začíná mezi 40. a 58. sekundou minuty. V RTH přicházel update
  ve 33.–36. sekundě jako 2–4 pushe po sobě, před 13:43 UTC i v 1.–15. sekundě. Hranice minuty
  to tedy není. Segment, ve kterém přesto proběhl `minuteUpdate` nebo `dayLoad`, je označený
  jako zkreslený.
- **Pan a data vlastníka:** tažení v režimu Kurzor přesune anotaci a uloží ji (#589), tažení
  range ho posune (#484). Pan proto začne jen v režimu Kurzor a jen v bodě, kde canvas po najetí
  ukazuje výchozí kurzor, tedy bez anotace, range i osy.
- **Úklid:** po gestu se pohled resetuje dvojklikem (obnoví i auto-follow #501). Na konci
  `pb.restore()` vrátí symbol, timeframe, pohled, Mode, Scale i styl.

**Statistika:** nejbližší pořadí (nearest-rank), stejně jako `perfSummary` měřiče. U každé
hodnoty je n. Malé n se neextrapoluje.

**Omezení:**
- Skriptovaná gesta jsou syntetické události, takže Event Timing (INP) je nevidí. Snímky,
  dlouhé úlohy a LoAF je vidí.
- React Profiler v produkčním buildu nefunguje. Rozpad nepřiřazeného času je z LoAF, viz
  oddíl Rozpad.
- Jeden notebook a jeden den. Exporty jsou ze dne měření.
- Při 240 Hz posílá skriptovaný pan 240 `pointermove` za sekundu. Myš dává totéž, protože
  Chrome zarovnává události na snímek. Na 60Hz displeji by gesto stejné délky mělo 4× méně
  kroků.

**Postup:**
```text
# konzole produkce (:8080), maximalizované okno Chrome, jediná záložka GEXLens
# hned po načtení stránky (studené načtení dne, první přepnutí na ES je studené):
<obsah scripts/perf_baseline_1412.js>
await pb.coldLoad()
pb.start()
await pb.run('1m', false); await pb.run('1m', true)
await pb.run('5m', false); await pb.run('5m', true)
# pasivně na ES 1m výchozí; hodinový refetch běží od načtení stránky (LIVE_RECONCILE_MS),
# mezitím 2–3 exporty:
JSON.stringify(gexlensPerf.perfExport(undefined, { records: false }))
JSON.stringify(pb.report())
await pb.restore(); pb.stop()
```
```bash
bash scripts/perf_baseline_load.sh "před 1m výchozí"   # docker stats + LHM, před a po každém bloku
```

## Výsledky

### Cíle ot. 2

| cíl | scénář | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý | splněno |
|---|---|---|---|---|---|---|
| p95 snímku ≤ 33 ms | pan | 33,4 | 29,2 | 12,6 | 12,6 | 1m výchozí ne, o 1 obnovení displeje (33,4 ms = 8 × 4,17 ms) |
| p95 snímku ≤ 33 ms | wheel zoom | 58,3 | 58,4 | 25,1 | 25,0 | **1m ne** |
| < 100 ms vč. kontur | Mode, kontury Off / Major | 56,6 / 165,3 | 137,6 / 260,5 | 65,6 / 44,8 | 102,6 / 67,3 | **ne** kromě 5m výchozího; 1m výchozí jen bez kontur |
| < 100 ms vč. kontur | Scale, kontury Off / Major | 61,7 / 155,3 | 153,4 / 245,4 | 34,8 / 49,1 | 53,6 / 106,4 | **ne** kromě 5m výchozího; 1m výchozí a 5m Čistý jen bez kontur |
| 1 úloha < 250 ms | minutový update | 3,7 úlohy za min, každá p95 128 / max 150; součet za min p95 374 | neměřeno pasivně | neměřeno pasivně | neměřeno pasivně | každá úloha ano, **jedna úloha ne** |
| < 250 ms | živý tick | žádný dlouhý snímek s invokerem WebSocket za 68 min; `drawDynamic` p50 0,1 | neměřeno pasivně | neměřeno pasivně | neměřeno pasivně | ano |
| medián ≤ 16,7 ms (cíl) | pan / zoom na 5m | | | 12,4 / 20,8 | 8,4 / 16,9 | pan ano, zoom ne |

Tabulka ukazuje p95 snímku, u Mode a Scale p95 spanu `modeScale` (n = 8–9, tedy max). Pasivní
okno běželo jen na ES 1m ve výchozím pohledu. Minutový update v Čistém pohledu a na 5m proto
není změřený, doměří ho E-2a.8 #1421 tímtéž sběračem.

### Aktivní scénáře

ES, 12. 10. 14:20:44–14:25:00 UTC. Všechny segmenty mají pokrytí ≥ 99,9 % a jsou bez skrytí.
Zkreslený minutovým updatem je jen symbol v 1m Čistém. Hodnoty v ms.

**Pan a zoom:** snímek p50 / p95 / max. Pan: n = 377–379 snímků, zoom: n = 89–93.

| | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý |
|---|---|---|---|---|
| pan: snímek | 24,9 / 33,4 / 58,3 | 24,9 / 29,2 / 54,2 | 12,4 / 12,6 / 20,7 | 8,4 / 12,6 / 20,8 |
| pan: `drawStatic` p50 / p95 | 5,6 / 8,1 | 2,3 / 3,4 | 1,9 / 3,0 | 1,7 / 2,6 |
| zoom: snímek | 37,5 / 58,3 / 83,2 | 45,9 / 58,4 / 62,5 | 20,8 / 25,1 / 29,3 | 16,9 / 25,0 / 25,1 |
| zoom: `drawStatic` p50 / p95 | 7,5 / 11,6 | 14,6 / 18,7 | 3,8 / 6,0 | 3,3 / 4,6 |
| zoom: LoAF ≥ 50 ms | 6 snímků, Scheduler Reactu 263 ms | 5 snímků, Scheduler Reactu 214 ms | 0 | 0 |

**Mode a Scale:** span `modeScale` do vykreslení kontur, p50 / p95. Mode n = 9, Scale n = 8,
takže p95 = max.

| | kontury | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý |
|---|---|---|---|---|---|
| Mode | Off | 42,7 / 56,6 | 91,7 / 137,6 | 24,4 / 65,6 | 34,1 / 102,6 |
| Mode | Major | 113,1 / 165,3 | 163,5 / 260,5 | 41,0 / 44,8 | 49,1 / 67,3 |
| Scale | Off | 43,8 / 61,7 | 125,4 / 153,4 | 28,2 / 34,8 | 45,5 / 53,6 |
| Scale | Major | 85,9 / 155,3 | 215,5 / 245,4 | 40,0 / 49,1 | 62,9 / 106,4 |
| Gradient ↔ Blobs (změna → po vykreslení, n = 6) | Off | 32,7 / 52,3 | 28,5 / 41,7 | 9,5 / 13,8 | 9,8 / 12,8 |

**Teplé přepnutí ES ↔ NQ:** span `symbolSwitch`, n = 4, p50 / max.
- 1m výchozí: 200,7 / 259,3;
- 1m Čistý: 258,0 / 378,8 (zkreslené minutovým updatem 251 ms);
- 5m výchozí: 97,5 / 113,4;
- 5m Čistý: 105,9 / 121,5.

LoAF tu ukazuje hlavně Scheduler Reactu (`MessagePort.onmessage`): v 1m výchozím 818 ms za čtyři
přepnutí, tedy ~200 ms na přepnutí.

**Velikost plátna a zakryté okno** (jen pro srovnání, do cílů se nepočítá):
- V okně 911 × 911 (plátno 505 × 189) měl 1m Čistý pan 20,8 / 25,0 a Mode Off 92,6 / 172. Mode
  a Scale tedy s plochou plátna téměř neklesají a jejich čas není v kreslení pixelů.
- 1m výchozí z 13:42 v zakrytém okně měl pan s pokrytím ~6 % a v cílech není. Ostatní segmenty
  tohoto bloku měřené po odkrytí odpovídají blokům výše.

### Minutový update, tick a hodinový refetch

ES 1m výchozí, 14:27:08–15:35:30 UTC. Okno trvalo 68,4 min s pokrytím 100 % a bez skrytí.

| metrika | hodnota |
|---|---|
| snímky celkem | 958 655; ≤ 8,4 ms 99,35 %, > 16,7 ms 0,32 % (3 039) |
| pomalé snímky ≥ 20 ms | n 2 142, p50 24,9, p95 95,8, max 1 675 ms |
| dlouhé úlohy | n 200, p50 93, p95 120, max 1 656 ms (refetch) |
| dlouhé úlohy za minutu | počet p50 3, p95 5; nejdelší p50 108, p95 136 ms; součet p50 246, p95 374 ms |
| `minuteUpdate` | n 252 (3,7 za minutu), p50 66,5, p95 128, max 149,5 ms |
| operace na update | `dayBuild` p50 18,0 / p95 24,3; `buildModeGrid` 8,9 / 12,6; `renderGrid` 9,7 / 12,0; `minuteFlush` 0,1 |
| tick | `drawDynamic` 40,5 za min, p50 0,1 ms; `drawStatic` 51,5 za min, p50 4,9, p95 9,7 ms; LoAF s invokerem WebSocket 0 |
| hodinový refetch | 14:34:48 `dayLoad` 8 143 ms; 15:34:48 `dayLoad` 9 151 ms (rozpad v oddílu Načtení dne) |

- **Update chodí po kanálech.** `minuteFlush` (252) i `dayBuild` (254) proběhnou 3,7× za
  minutu. Každý kanál engine tedy dorazí do jiného okna debounce (`APPEND_DEBOUNCE_MS` = 400 ms,
  `replay/useDayData.ts:34`) a každý přestaví den i grid. V RTH přicházely pushe ve 33.–36.
  sekundě, tedy rozprostřené přes ~3 s.
- **Tick:** cíl splněný. `drawStatic` běží 51,5× za minutu, víc než ticků (`drawDynamic` 40,5),
  ale stojí jen ~0,25 s za minutu.

### Načtení dne a přepnutí symbolu (vstup ADR-0047)

| událost | celkem | fáze |
|---|---|---|
| studené načtení stránky (reload 13:34:47, NQ 15m) | data dne 12,2 s po navigaci | DCL 235 ms, `load` 1 172 ms; `/api/replay/NQ` začíná až v 4 817 ms, TTFB 7 166 ms, stažení 166 ms, 4 677 kB gzip → 28 065 kB |
| studené přepnutí NQ → ES (13:42:23, první v záložce) | `symbolSwitch` 12 388 ms | `dayFetch` 9 948 (TTFB + stažení + `json()` 28 MB), `dayDecode` 2 310 jako jedna dlouhá úloha, `dayBuild` 50 |
| hodinový refetch ES (14:34:48, `LIVE_RECONCILE_MS`) | `dayLoad` 8 143 ms | `dayFetch` 6 537, `dayDecode` 1 492 jako jedna dlouhá úloha |
| hodinový refetch ES (15:34:48) | `dayLoad` 9 151 ms | fronta 5 ms, TTFB 7 125 ms (78 %), stažení 138 ms (5 936 kB gzip → 30 863 kB), `json()` ~141 ms, `dayDecode` 1 656 ms jako jedna dlouhá úloha |
| teplé přepnutí ES ↔ NQ | 98–379 ms | viz Aktivní scénáře; balík dne je v paměti, zbytek je render Reactu |

- **`dayDecode` je jedna úloha.** Je to `decodeBundle` (`replay/loader.ts:481`), spuštěný
  hned po `json()` (LoAF invoker `Response.json.then`). Hlavní vlákno stojí 1,5–2,3 s,
  graf nereaguje a v tu dobu nejde ani tick, ani gesto.
- **Hodinový refetch** zablokuje hlavní vlákno jednou za hodinu na 1,5–1,7 s. Opakuje se
  při každém refetchi, takže nejde jen o studený start.
- **Většinu času tvoří server.** TTFB `/api/replay` je 7,1 s, tedy 78 % načtení. Stažení
  a `json()` 28–31 MB stojí jen ~0,3 s.
- `/api/replay` startuje až 4,8 s po navigaci. Na čem čeká, rozpadne E-1.5 #1391.
- **Vstup pro ADR-0047:** načtení dne trvá 8–12 s a přepnutí na symbol, který ještě není
  v paměti, 12 s. Předběžná hranice < 1 s tak bez změny serveru (TTFB) a bez dekódování mimo
  hlavní vlákno (2b.1) nejde.

### Rozpad dlouhých snímků (LoAF)

LoAF vidí jen snímky ≥ 50 ms, kratší rozpadá přes operace měřiče. Uvnitř Reactu LoAF končí
u Scheduleru (`MessagePort.onmessage` → `performWorkUntilDeadline`, `index-BFwQwrQT.js:11757`)
a dál nerozkládá.

- **Pan 1m:**
  - snímek má p50 24,9 ms, z toho `drawStatic` 5,6 ms; zbylých ~19 ms na krok je mimo kreslení;
  - každý `pointermove` mění stav pohledu (`setGestureView` → `setView`, `Heatmap.tsx:1458`)
    a vyvolá render;
  - na 5m je snímek poloviční (12,4 ms), náklad tedy roste s počtem minut v datech;
  - Čistý pohled bez spodních panelů má stejný p50 a p95 nižší o 4 ms, takže panely nejsou
    hlavní část.
- **Zoom 1m:**
  - LoAF má 5–6 snímků ≥ 50 ms a v nich 214–263 ms ve Scheduleru Reactu, tedy ~45 ms na
    dlouhý snímek;
  - krok kolečka mění `zoomX` i `zoomY` a hned je hlásí rodiči k persistenci (`onUserZoomX`,
    `onUserYRange`, `Heatmap.tsx:1462–1465`), takže se při každém kroku renderuje i rodič.
- **Mode a Scale:** přepnutí je jedna synchronní úloha v pořadí `buildModeGrid` → neměřená
  práce Reactu → `drawStatic` → `renderGrid`. Mezera od konce `buildModeGrid` po začátek
  `drawStatic` (ms, 6–8 přepnutí na buňku):

  | | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý |
  |---|---|---|---|---|
  | Mode | 9–23 | 28–124 | 6–14 | 9–85 |
  | Scale | 9–18 | 85–115 | 5–9 | 20–34 |

  - **Čistý pohled** přidává na 1m 50–100 ms renderu Reactu mimo měřené operace. Proto Mode
    i Scale nesplní cíl ani bez kontur. Která komponenta to je, LoAF neřekne.
  - **Kontury Major** ve výchozím pohledu mezeru nezvětší, ale `drawStatic` proběhne dvakrát
    (19 na 9 přepnutí Mode proti 10 bez kontur). Span končí až po druhém kole po konturách:
    +70 ms na 1m. To je přesně E-2a.7 #1420.
  - Invoker LoAF je tu `FrameRequestCallback` bez zdroje. Sběrač mění select v pokračování po
    `await frame()` a synchronní render Reactu po diskrétní události `change` běží uvnitř jeho
    rAF. Při kliknutí myší je to úloha `event-listener` se stejnou délkou.
- **Minutový update:** 200 dlouhých úloh, p50 93 ms. Všechny jsou ve Scheduleru Reactu, kromě
  dvou refetchů (`Response.json.then` → `decodeBundle`). Měřené operace (`dayBuild`,
  `buildModeGrid`, `renderGrid`, `drawStatic`) dají ~42 ms z 66,5 ms spanu.
- **Načtení dne:** `dayDecode` je jedna úloha 1,5–2,3 s, viz oddíl Načtení dne.

**Závěr metody A (LoAF na produkci):** LoAF rozložil dlouhé snímky na tyto části:
- render Reactu po gestu, po minutovém updatu a v Čistém pohledu;
- kreslení (`drawStatic`, druhé kolo po konturách);
- dekódování dne.

Render Reactu dál nerozložil. Na komponenty ho rozloží jen React Profiler v profiling buildu,
viz Stav v #1412.

### Exporty měřiče

`perfExport(undefined, { records: false })`, okno 30 s, ES 1m výchozí, build `f713920`:

| čas (UTC) | snímky n / p50 / p95 / max | dlouhé úlohy za minutu (10 min před exportem) | počítadla od 14:27:08 |
|---|---|---|---|
| 14:39:03 | 6 880 / 4,2 / 4,3 / 104,2 (229 fps) | 3, 3, 3, 2, 2, 6, 4, 4, 4, 4; nejdelší 96–127, ve 14:34 1 492 (refetch) | `minuteUpdate` 47, `drawStatic` 689, `dayFetch` 1 |
| 15:01:25 | 7 027 / 4,2 / 4,3 / 45,8 (234 fps) | 2, 3, 2, 2, 3, 2, 2, 3, 2, 3; nejdelší 96–126 | `minuteUpdate` 130, `drawStatic` 1 873, `dayFetch` 1 |
| 15:35:30 | 7 055 / 4,2 / 4,3 / 50,0 (235 fps) | 2, 4, 2, 2, 3, 2, 3, 2, 4, 5; nejdelší 105–127, v 15:34 1 656 (refetch) | `minuteUpdate` 252, `drawStatic` 3 522, `dayFetch` 2 |

V klidu bez gesta drží graf 229–235 fps a medián snímku 4,2 ms. Výkon tedy ztrácí jen
v událostech: gesto, přepnutí, minutový update a refetch.

## Pořadí oprav E-2a.3–E-2a.9

Nejdřív jdou opravy nesplněných cílů, seřazené podle odchylky od cíle a hodnoty pro čtení grafu
v RTH. Ostatní úkoly žádný cíl neplní a počkají na přeměření. Zapsáno také v #1274.

1. **E-2a.6 #1419 pan a zoom přes rAF, persistence až na konci gesta.**
   - Zoom na 1m má p95 58 ms proti cíli 33 v obou pohledech.
   - Render Reactu stojí ~45 ms na dlouhý snímek, protože krok kolečka hlásí zoom rodiči.
   - Pan na 1m výchozím je s p95 33,4 ms na hranici.
2. **E-2a.7 #1420 Mode a Scale < 100 ms.**
   - S konturami Major má 1m výchozí p95 155–165 ms. Hrdlem je druhé kolo `drawStatic` po
     konturách, přesně rozsah E-2a.7.
   - Čistý pohled na 1m nesplní cíl ani bez kontur (138–153 ms), kvůli +50–100 ms renderu
     Reactu. To je mimo dnešní rozsah E-2a.7 a rozhodne o tom vlastník (Stav #1412).
3. **E-2a.8 #1421 minutový update jako jedna úloha.**
   - Každý update má p95 128 ms, ale chodí 3,7× za minutu.
   - Součet za minutu má p95 374 ms a každý update přestaví den i grid.
   - Cíl „jedna úloha < 250 ms“ dnes splněný není.
4. **#1536 `/news/stats` jen pro aktivní symbol** (rozhodnuto C). Snímky neovlivňuje, ale
   3,8 MB za minutu s TTFB 2,2–2,7 s zdržuje ve frontě HTTP/1.1 ostatní polly (`/paper/account`
   1,9–3,0 s). Je malý a nezávislý.
5. **E-2a.3 #1413 memo spodních panelů:** Čistý pohled bez panelů má při panu stejný p50
   (24,9 ms), očekávaný zisk je tedy malý. Rozhodnout až po E-2a.6.
6. **E-2a.5 #1417 ořez a LOD overlay:** `drawStatic` dělá 5,6–14,6 ms snímku na 1m. Po E-2a.6,
   pokud pan a zoom pořád nesplní cíl.
7. **E-2a.9 #1422 Blobs a jediný polling paper účtu:** přepnutí Gradient ↔ Blobs trvá 33 / 52 ms
   na 1m a cíl splňuje. `/paper/account` pollují dva hooky po 10 s (`App.tsx:675`,
   `InstrumentHeader.tsx:171`), tedy 12 požadavků za minutu. Je to síťová hygiena spolu s #1536.
8. **E-2a.4 #1416 tick bez `drawStatic`:** cíl tick < 250 ms je splněný a `drawStatic` na tick
   stojí ~0,25 s za minutu. Nejnižší priorita.

Načtení dne (TTFB 7 s, `dayDecode` 1,5–2,3 s na hlavním vlákně) do E-2a.3–E-2a.9 nepatří. Jde
do ADR-0047 (E-1.5 #1391) a do 2b.1 (dekódování ve Web Workeru) po přeměření E-2a.18.

## Pozorování

**Průběh měření 12. 10.:**
- **Zakryté okno.** První blok (13:42, 1m výchozí) běžel v okně, které měl terminál úplně
  zakryté. Pan trval 155 s s pokrytím ~6 % a do výsledků nejde. Sběrač od té doby hlídá
  viditelnost a pokrytí u každého segmentu.
- **Klidné okno segmentů.** Okno 15.–40. sekunda minuty předpokládalo update na hranici minuty.
  V RTH ale update chodil ve 33.–36. sekundě, proto se okno posunulo na 40.–58. s.
- **Malé okno.** Blok 1m Čistý v okně 911 × 911 je jen srovnání velikosti plátna (oddíl
  Aktivní scénáře).

**Zkouška sběrače nanečisto** (11:39–11:41 UTC, před RTH, ES 1m výchozí, kontury Off). Ověřila
jen sběrač, čísla nejsou baseline:
- souběžně běžely dva cargo buildy jiného projektu (207–213 % CPU), v RTH už ne;
- studené přepnutí NQ → ES trvalo 20,5 s (TTFB 17,6 s), v RTH 12,4 s.

**`dayDecode`** má 5 vzorků, všechny jako jedna dlouhá úloha:
- 1 132 ms při nasazení #1409;
- 2,0 s při zkoušce nanečisto;
- 2 310, 1 492 a 1 656 ms v RTH.

**Nález #1536:**
- Hlavní pohled stahuje každou minutu celé `/news/stats` (3,8 MB, gzip 481 kB, 10 623 řádků),
  ale potřebuje z něj 138 řádků.
- V RTH měl TTFB 2,2–2,7 s v prohlížeči. Ostatní minutové polly čekají ve frontě HTTP/1.1.
  Například `/paper/account` měl při zkoušce TTFB 1,9–3,0 s v prohlížeči a 0,31 s z hostitele.
- Snímky to neovlivňuje: dlouhý snímek s parsováním `/news/stats` LoAF nezachytil.
