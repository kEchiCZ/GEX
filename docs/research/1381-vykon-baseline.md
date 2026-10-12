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

**Měření z 24. 9. (#1274) se s tímto přímo srovnat nedá:**
- proběhlo na NQ s mřížkou 101 striků × 1 409 minut (142 tis. buněk). ES měl 12. 10. 80 striků
  a odpoledne 974–1 049 minut se snímkem (78–84 tis. buněk), při doměření v 19:30 1 284 minut;
- proběhlo ve skryté záložce bez rAF a na buildu před měřičem #1409.

Jeho čísla jsou 2–7× vyšší než dnešní. Například Mode na 1m měl medián 104 ms, dnes 42,7.
Minutový update měl 442–461 ms, dnes p50 66,5. Pan měl 74 ms, dnes 24,9, a `drawStatic`
~27 ms, dnes 5,6. Dolní mez to tedy nebyla. Kolik z rozdílu dělá menší mřížka a kolik změny
kódu mezi 24. 9. a 12. 10., tohle měření nerozliší.

## Podmínky měření

| | hodnota |
|---|---|
| den a čas (UTC) | po 12. 10. 2026, RTH: studené načtení 13:34:47, aktivní bloky 14:20:44–14:25:00, pasivní okno 14:27:08–15:35:30, doměření 19:29–19:59 |
| trh | Columbus Day: akcie a futures obchodují běžně, trh dluhopisů USA je zavřený |
| build frontendu (`perfExport().build`) | `f71392093f779e6692fba44f87e597d7c36780e3` (produkce od 12. 10. 09:52 UTC) |
| data | ES a NQ, expirace 20261012 (0DTE), obchodní den 2026-10-12; balík dne 4,7 MB gzip, rozbaleně 28 MB, ve 19:30 7,3 MB gzip |
| prohlížeč | Chrome 155, Windows 10, jediná záložka GEXLens |
| okno | maximalizované, viewport 1822 × 903, DPR 1; plátno heatmapy 1416 × 420 ve výchozím pohledu |
| obnovovací frekvence (brána) | 238 Hz (klidový medián snímku 4,2 ms); do 15:35 změřená na začátku každého bloku, při doměření 19:29–19:59 před každým segmentem (`gateMs`) |
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
- **síť:** zapisuje časy `/api/replay` a `/news/stats` přes vlastní PerformanceObserver, protože
  buffer resource timing prohlížeče (250 záznamů) je po načtení stránky plný:
  - fronta = `requestStart − startTime`;
  - TTFB = `responseStart − requestStart`;
  - stažení = `responseEnd − responseStart`.

  Výjimka: TTFB studeného načtení stránky ve 13:34 je počítaný od `startTime`, tedy i s frontou;
- **gesta:** pouští skriptovaná gesta, takže přeměření E-2a.18 je srovnatelné krok za krokem.

**Scénáře** se měří na 1m a 5m, vždy ve výchozím i Čistém pohledu (#238):

| scénář | jak | metrika |
|---|---|---|
| pan | 3× tažení 300 px tam a zpět, jeden `pointermove` na snímek (60 kroků) | snímky p50/p95; do 15:35 všechny snímky segmentu, při doměření jen snímky gesta (360) |
| wheel zoom | 3× 12 kroků kolečka dovnitř a 12 ven, jeden krok na snímek | snímky p50/p95; do 15:35 všechny snímky segmentu, při doměření jen snímky gesta (72) |
| Mode | všechny módy a zpět na výchozí | span `modeScale` (do vykreslení kontur) |
| Scale | 2× všechny škály a zpět | span `modeScale` |
| Gradient / Blobs | 3× tam a zpět | změna → po vykreslení (rAF + úloha) |
| přepnutí symbolu, studené | po reloadu na NQ první přepnutí na ES (`pb.run` začíná přepnutím na ES) | span `symbolSwitch`, fáze `dayFetch`/`dayDecode`/`dayBuild` (v pasivním okně) |
| přepnutí symbolu, teplé | ES → NQ → ES → NQ → ES, balíky dne už v paměti; při doměření ES → NQ → ES s čekáním na dorovnání dne | span `symbolSwitch`; při doměření i dorovnávací `dayFetch` a `dayDecode` |
| načtení dne, studené | reload stránky | navigace, resource timing `/api/replay`, bufferované LoAF |
| minutový update | pasivně: 1m výchozí 68 min, ostatní kombinace při doměření 5–9 min | span `minuteUpdate`, dlouhé úlohy za minutu |
| živý tick | pasivně | LoAF s invokerem WebSocket, `drawStatic` a `drawDynamic` za minutu |
| hodinový refetch | pasivně (`LIVE_RECONCILE_MS` = 1 h) | `dayFetch`/`dayDecode`/`dayBuild`, span `dayLoad` |

**Brány a pojistky:**
- **Viditelnost:** před každým segmentem musí být klidový medián snímku ≤ 17,5 ms, jinak
  segment čeká. Do 15:35 brána běžela před čekáním na klidné okno, při doměření těsně před
  segmentem a její medián je u segmentu (`gateMs`). Chrome na Windows hlásí celé zakryté
  okno jako `hidden` a rAF zastaví, takže gesto i spany by čekaly. Segment proto nese:
  - **pokrytí**: součet snímků / délka segmentu;
  - počet přechodů do `hidden`.

  Segment s pokrytím < 90 % nebo se skrytím je neplatný (`occluded`). Okno Chrome je
  maximalizované a terminál ho kryje jen zčásti, protože celé zakryté okno se nekreslí.
- **Minutový update:** segment začíná mezi 40. a 58. sekundou minuty. V RTH přicházel update
  ve 33.–36. sekundě jako 2–4 pushe po sobě, před 13:43 UTC i v 1.–15. sekundě. Hranice minuty
  to tedy není. Segment, ve kterém přesto proběhl `minuteUpdate`, `dayLoad` nebo (od doměření)
  `dayDecode`, je označený jako zkreslený. U přepnutí symbolu je dorovnání dne součástí měření,
  zkreslení tam značí jen `minuteUpdate`.
- **Pan a data vlastníka:** tažení v režimu Kurzor přesune anotaci a uloží ji (#589), tažení
  range ho posune (#484). Pan proto začne jen v režimu Kurzor a jen v bodě, kde canvas po najetí
  ukazuje výchozí kurzor, tedy bez anotace, range i osy.
- **Úklid:** po gestu se pohled resetuje dvojklikem (obnoví i auto-follow #501). Reset ale
  smaže uložený zoom X timeframe a pásmo Y symbolu (`clearSavedView`, `App.tsx:631`). Sběrač
  si proto při `start()` uloží klíče `gexlens.chartZoomX` a `gexlens.chartYRange`. Na konci
  `pb.restore()` vrátí symbol, timeframe, pohled, Mode, Scale i styl, zapíše oba klíče zpět
  a znovu načte stránku, protože aplikace čte localStorage jen při startu. Bloky do 15:35
  sběrač ještě bez této zálohy smazal vlastníkovi uložený zoom ES a NQ na 1m, 5m a 15m.

**Statistika:** nejbližší pořadí (nearest-rank), stejně jako `perfSummary` měřiče. U každé
hodnoty je n. Malé n se neextrapoluje.

**Omezení:**
- Skriptovaná gesta jsou syntetické události, takže Event Timing (INP) je nevidí. Snímky,
  dlouhé úlohy a LoAF je vidí.
- React Profiler v produkčním buildu nefunguje. Rozpad nepřiřazeného času je z LoAF, viz
  oddíl Rozpad.
- Jeden notebook a jeden den. Exporty jsou ze dne měření. Columbus Day má obvykle nižší
  objem, proto přeměření E-2a.18 #1431 patří do běžného obchodního dne.
- Velikost dne roste: v 1m bloku 14:20 měl den 974 minut se snímkem, při doměření v 19:30
  1 284 (ES 80 striků). Čísla z různých časů dne proto nejsou přímo srovnatelná, srovnávají
  se jen okna ze stejné doby.
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
JSON.stringify(pb.report())                 # aktivní segmenty uložit před pasivním oknem
pb.stop()
<obsah scripts/perf_baseline_1412.js>       # znovu vložit: čistý log, výchozí volby zůstanou v sessionStorage
pb.start()                                  # pasivní okno od nuly, bez záznamů z gest
# pasivně na ES 1m výchozí; hodinový refetch běží od načtení stránky (LIVE_RECONCILE_MS),
# mezitím 2–3 exporty:
JSON.stringify(gexlensPerf.perfExport(undefined, { records: false }))
JSON.stringify(pb.report())
await pb.restore()                          # vrátí volby i uložený zoom, pak reload stránky
```
```bash
bash scripts/perf_baseline_load.sh "před 1m výchozí"   # docker stats + LHM, před a po každém bloku
```

## Výsledky

### Cíle ot. 2

| cíl | scénář | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý | splněno |
|---|---|---|---|---|---|---|
| p95 snímku ≤ 33 ms | pan | 33,4 (37,4) | 29,2 (37,5) | 12,6 (16,7) | 12,6 (16,7) | **1m ne**: odpoledne 1m výchozí o 1 obnovení displeje (33,4 ms = 8 × 4,17 ms), ve 19:30 oba pohledy zřetelně |
| p95 snímku ≤ 33 ms | wheel zoom | 58,3 (41,5) | 58,4 (54,1) | 25,1 (33,3) | 25,0 (25,1) | **1m ne**; 5m výchozí ve 19:30 na hranici |
| < 100 ms vč. kontur | Mode, kontury Off / Major | 56,6 / 165,3 | 137,6 / 260,5 | 65,6 / 44,8 | 102,6 / 67,3 | **ne** kromě 5m výchozího; 1m výchozí jen bez kontur, 5m Čistý jen s konturami Major (Off 102,6 při n = 9) |
| < 100 ms vč. kontur | Scale, kontury Off / Major | 61,7 / 155,3 | 153,4 / 245,4 | 34,8 / 49,1 | 53,6 / 106,4 | **ne** kromě 5m výchozího; 1m výchozí a 5m Čistý jen bez kontur |
| 1 úloha < 250 ms | minutový update | 3,7 updatu za min, každý p95 128 / max 150; součet za min p95 374 (19:54: 3 za min, p95 173, součet p95 435) | 4 za min, každý p95 278 / max 287; součet p95 918 | 4 za min, každý p95 81; součet p95 231 | 3,9 za min, každý p95 90; součet p95 473 | **jedna úloha ne** nikde; v 1m Čistém nesplní ani jednotlivý update |
| < 250 ms | živý tick | za 68 min nad 250 ms jen 2 snímky, oba při refetchi; `drawDynamic` p50 0,1 | nad 250 ms jen 2 snímky, oba v minutovém updatu | žádný snímek nad 250 ms | žádný snímek nad 250 ms | ano |
| medián ≤ 16,7 ms (cíl) | pan / zoom na 5m | | | 12,4 / 20,8 (12,5 / 20,8) | 8,4 / 16,9 (12,5 / 20,8) | pan ano, zoom ne; 16,9 na 5m Čistém odpoledne je hranice (4 obnovení displeje 238 Hz) |

Tabulka ukazuje p95 snímku, u Mode a Scale p95 spanu `modeScale` (n = 8–9, tedy max).
- U panu a zoomu je v závorce doměření v 19:30. Bere jen snímky gesta a den byl větší
  (1 284 minut proti 974).
- Minutový update na 1m výchozím je z pasivního okna 14:27–15:35 (68 min). Ostatní kombinace
  jsou z doměření 19:32–19:59, okna mají 5–9 min. Viz oddíl Doměření.

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

**Teplé přepnutí ES ↔ NQ:** span `symbolSwitch`, n = 4, p50 / max. Span končí vykreslením
z cache. Následné dorovnání dne (14–15,5 s) odpolední bloky neměřily, je v oddílu Doměření.
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
- **Tick:** cíl splněný. Za 68 min byly nad 250 ms jen 2 snímky, oba při refetchi (histogram
  snímků), takže žádný tick nemohl trvat 250 ms. To, že LoAF nemá žádný snímek s invokerem
  WebSocket, samo o sobě důkaz není: zprávu WebSocketu může zpracovat až pozdější úloha
  s jiným invokerem. `drawStatic` běží 51,5× za minutu, víc než ticků (`drawDynamic` 40,5),
  ale stojí jen ~0,25 s za minutu.

### Doměření 19:29–19:59 UTC

Doměření doplňuje tři mezery odpoledních bloků:
- snímky jen z gesta;
- přepnutí symbolu včetně dorovnání dne;
- minutový update v Čistém pohledu a na 5m.

Podmínky:
- sběrač `acd626d`, ES, okno 1822 × 903;
- brána 4,2 ms před každým segmentem, pokrytí 100 %, bez skrytí;
- den měl 1 284 minut se snímkem a 80 striků, odpoledne 974–1 049 minut.

**Pan a zoom** (p50 / p95 / max). Řádky „gesto“ berou jen snímky gesta (pan n = 360,
zoom n = 72), řádky „segment“ všechny snímky segmentu jako odpoledne.

| | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý |
|---|---|---|---|---|
| pan: gesto | 29,1 / 37,4 / 41,7 | 29,1 / 37,5 / 50,0 | 12,5 / 16,7 / 20,9 | 12,5 / 16,7 / 20,8 |
| pan: segment | 25,1 / 37,4 / 41,7 (n 384) | 29,1 / 37,5 / 50,0 (n 382) | 12,5 / 16,7 / 20,9 (n 380) | 12,5 / 16,7 / 20,8 (n 380) |
| zoom: gesto | 33,3 / 41,5 / 41,7 | 41,6 / 54,1 / 54,3 | 20,8 / 33,3 / 45,8 | 20,8 / 25,1 / 29,2 |
| zoom: segment | 33,2 / 41,5 / 74,9 (n 96) | 37,6 / 54,1 / 75,0 (n 96) | 20,8 / 29,2 / 45,8 (n 92) | 20,8 / 29,1 / 33,4 (n 94) |
| zoom: LoAF ≥ 50 ms | 1 (68,6, reset) | 1 (71,4, reset) | 0 | 0 |

- **Snímky mimo gesto** (najetí, klid a reset dvojklikem) snižují p50 (pan 1m výchozí 25,1
  proti 29,1) a přidávají max (reset ~75 ms). p95 se mění nejvýš o 4 ms.
- **Cíle se nemění.** Ve větším dni nesplní pan na 1m cíl zřetelně (p95 37,4–37,5) a zoom na
  1m také ne (41,5 a 54,1). Na 5m mají pan i zoom p95 ≤ 33,3; zoom 5m výchozí je s 33,3
  na hranici (8 obnovení displeje).
- **Odpolední zoom 1m** (p95 58,3, 6 dlouhých snímků) počítal i snímky resetu. Kolik
  z dlouhých snímků patřilo gestu, z kompaktních dat zpětně oddělit nejde. Ve 19:30 je jediný
  dlouhý snímek zoomu reset.

**Přepnutí ES ↔ NQ s dorovnáním dne** (ms). Balíky obou dnů už byly v paměti.

| | `symbolSwitch` (z cache) | dorovnání celkem | `dayFetch` | z toho TTFB | `dayDecode` |
|---|---|---|---|---|---|
| 1m výchozí, ES → NQ | 293,6 | 15 492 | 13 198 | 12 286 | 2 245 |
| 1m výchozí, NQ → ES | 243,1 | 13 998 | 11 781 | 11 136 | 2 165 |
| 5m výchozí, ES → NQ | 154,1 | 15 399 | 12 848 | 11 920 | 2 438 |
| 5m výchozí, NQ → ES | 115,7 | 13 953 | 11 885 | 11 270 | 1 978 |

- Z cache se graf vykreslí za 116–294 ms. Pak `useDayData` stáhne celý den znovu
  (`replay/useDayData.ts:396–431`) a dorovnání skončí za 14,0–15,5 s.
- Na konci je `dayDecode` 2,0–2,4 s jako jedna úloha na hlavním vlákně. Graf mezitím
  nereaguje a minutový update ani gesto neproběhnou.
- `fetchReplayInputs` dekóduje ještě před kontrolou `cancelled` (`replay/loader.ts:479–482`).
  Rychlé přepnutí tam a zpět proto dekóduje i fetch, který už nikdo nepotřebuje.
- TTFB `/api/replay` vzrostl s velikostí dne na 11,1–12,3 s (odpoledne 7,1 s), balík má
  6,7–7,4 MB gzip.

**Minutový update pasivně** (mimo segmenty; p50 / p95 / max v ms):

| okno (UTC) | kombinace | min | updatů za min | `minuteUpdate` | dlouhé úlohy za min: počet p50, součet p50 / p95 | `dayBuild` p50 | `renderGrid` p50 | `drawStatic` p95 / max | snímky > 250 ms |
|---|---|---|---|---|---|---|---|---|---|
| 14:27–15:35 | 1m výchozí | 68,4 | 3,7 | 66,5 / 128 / 149,5 | 3; 246 / 374 | 18,0 | 9,7 | 9,7 / 44,1 | 2 (refetch) |
| 19:31:55–19:41:00 | 1m Čistý | 9,1 | 4,0 | 135,4 / 277,8 / 286,7 | 5; 666 / 918 | 25,4 | 11,0 | 9,0 / 52,3 | 2 (update) |
| 19:42:20–19:47:20 | 5m výchozí | 5,0 | 4,0 | 59,3 / 80,9 / 105,2 | 2; 157 / 231 | 27,2 | 2,6 | 3,4 / 14,4 | 0 |
| 19:47:48–19:53:40 | 5m Čistý | 5,9 | 3,9 | 69,0 / 89,6 / 113,6 | 3; 187 / 473 | 26,1 | 2,4 | 2,2 / 11,0 | 0 |
| 19:53:42–19:59:30 | 1m výchozí (kontrola) | 5,8 | 3,0 | 98,6 / 173 / 173 | 4; 400 / 435 | 27,6 | 11,0 | 10,8 / 43,6 | 0 |

- **Čistý pohled zhoršuje i minutový update.** Ve stejné hodině měl update na 1m:
  - v Čistém pohledu p50 135 ms a p95 278 ms;
  - ve výchozím p50 99 ms a p95 173 ms.

  První update každé minuty trvá v Čistém 188–287 ms (ve výchozím 147–173), takže v Čistém
  nesplní 250 ms ani jednotlivý update. 56 z 58 dlouhých snímků je ve Scheduleru Reactu.
  `drawStatic` v updatu má v Čistém až 52 ms. Je to stejný vzorec jako u Mode a Scale.
- **Update roste s velikostí dne.** Na 1m výchozím vzrostl p50 z 66,5 ms (14:27–15:35,
  974–1 049 minut) na 98,6 ms (19:54–19:59, 1 284 minut) a `dayBuild` z 18,0 na 27,6 ms.
- **Updatů je 3–4 za minutu**, všechny ve 33.–36. sekundě. Každý přestaví den i grid. Cíl
  „jedna úloha za minutu < 250 ms“ proto nesplní žádná kombinace. Součet za minutu (p95):
  - 1m výchozí 374–435 ms;
  - 1m Čistý 918 ms;
  - 5m výchozí 231 ms;
  - 5m Čistý 473 ms (jen jedna ze 6 minut, ostatní p50 187).
- **Tick:** v 1m Čistém byly nad 250 ms jen 2 snímky, oba v minutovém updatu (19:32:34
  a 19:33:34). Na 5m žádný. Cíl ticku platí.

### Načtení dne a přepnutí symbolu (vstup ADR-0047)

| událost | celkem | fáze |
|---|---|---|
| studené načtení stránky (reload 13:34:47, NQ 15m) | data dne 12,2 s po navigaci | DCL 235 ms, `load` 1 172 ms; `/api/replay/NQ` začíná až v 4 817 ms, TTFB 7 166 ms, stažení 166 ms, 4 677 kB gzip → 28 065 kB |
| studené přepnutí NQ → ES (13:42:23, první v záložce) | `symbolSwitch` 12 388 ms | `dayFetch` 9 948 (TTFB + stažení + `json()` 28 MB), `dayDecode` 2 310 jako jedna dlouhá úloha, `dayBuild` 50 |
| hodinový refetch ES (14:34:48, `LIVE_RECONCILE_MS`) | `dayLoad` 8 143 ms | `dayFetch` 6 537, `dayDecode` 1 492 jako jedna dlouhá úloha |
| hodinový refetch ES (15:34:48) | `dayLoad` 9 151 ms | fronta 5 ms, TTFB 7 125 ms (78 %), stažení 138 ms (5 936 kB gzip → 30 863 kB), `json()` ~141 ms, `dayDecode` 1 656 ms jako jedna dlouhá úloha |
| teplé přepnutí ES ↔ NQ | vykreslení z cache 98–379 ms, dorovnání dne 14,0–15,5 s | render Reactu z balíku v paměti; pak `useDayData` stáhne celý den znovu: `dayFetch` 11,8–13,2 s (TTFB 11,1–12,3 s ve 19:30), `dayDecode` 2,0–2,4 s jako jedna úloha (oddíl Doměření) |

- **`dayDecode` je jedna úloha.** Je to `decodeBundle` (`replay/loader.ts:481`), spuštěný
  hned po `json()` (LoAF invoker `Response.json.then`). Hlavní vlákno stojí 1,5–2,4 s,
  graf nereaguje a v tu dobu nejde ani tick, ani gesto.
- **Hodinový refetch** zablokuje hlavní vlákno jednou za hodinu na 1,5–1,7 s. Opakuje se
  při každém refetchi, takže nejde jen o studený start.
- **Většinu času tvoří server.** TTFB `/api/replay` je 7,1 s, tedy 78 % načtení. Stažení
  a `json()` 28–31 MB stojí jen ~0,3 s.
- `/api/replay` startuje až 4,8 s po navigaci. Na čem čeká, měření nerozložilo; je to vstup
  pro E-1.5 #1391 (zapsáno v komentáři tam).
- **Vstup pro ADR-0047:**
  - načtení dne trvá 8–12 s, ve větším dni v 19:30 TTFB sám 11–12 s;
  - přepnutí na symbol, který ještě není v paměti, trvá 12 s;
  - teplé přepnutí se z cache vykreslí do 0,4 s, ale za 14–15 s ho dorovná plný refetch
    s 2–2,4 s blokovaným hlavním vláknem.

  Předběžná hranice < 1 s tak platí jen pro vykreslení z cache. Bez změny serveru (TTFB)
  a bez dekódování mimo hlavní vlákno (2b.1) ji načtení dne nesplní.

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
  - LoAF má 5–6 snímků ≥ 50 ms a v nich 214–263 ms ve Scheduleru Reactu, tedy 43–44 ms na
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
    (19 na 9 přepnutí Mode proti 10 bez kontur). Span končí až po vykreslení kontur a na 1m je
    delší o 70 ms u Mode (p50 42,7 → 113,1) a o 42 ms u Scale (43,8 → 85,9). V tom čase je
    výpočet kontur ve workeru, doručení výsledku, druhý render Reactu a druhé kolo
    `drawStatic`. Na tyto části měřič span nerozloží. Celé je to rozsah E-2a.7 #1420.
  - Invoker LoAF je tu `FrameRequestCallback` bez zdroje. Sběrač mění select v pokračování po
    `await frame()` a synchronní render Reactu po diskrétní události `change` běží uvnitř jeho
    rAF. Při kliknutí myší je to úloha `event-listener` se stejnou délkou.
- **Minutový update a tick:** pasivní okno má 200 dlouhých úloh, p50 93 ms. Kromě dvou
  refetchů (`Response.json.then` → `decodeBundle`) jsou všechny ve Scheduleru Reactu. Většinou
  patří minutovému updatu (252 spanů), ale mohou mezi nimi být i rendery po ticku: `onmessage`
  WebSocketu jen nastaví stav (`api/ws.ts:71–94`) a render běží ve Scheduleru. Měřené operace
  (`dayBuild`, `buildModeGrid`, `renderGrid`, `drawStatic`) dají ~42 ms z 66,5 ms spanu.
- **Načtení dne:** `dayDecode` je jedna úloha 1,5–2,4 s, viz oddíl Načtení dne.

**Závěr metody A (LoAF na produkci):** LoAF rozložil dlouhé snímky na tyto části:
- render Reactu po gestu, po minutovém updatu a v Čistém pohledu;
- kreslení (`drawStatic`, druhé kolo po konturách);
- dekódování dne.

Render Reactu dál nerozložil. Profiling build se podle rozhodnutí z 12. 10. nedělá. Jak render
rozložit bez něj, předkládá Stav v #1412.

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
   - Zoom na 1m má p95 58 ms proti cíli 33 v obou pohledech. Při doměření, jen ze snímků
     gesta, měl 41,5 ve výchozím a 54,1 v Čistém.
   - Render Reactu stojí 43–44 ms na dlouhý snímek a krok kolečka hlásí zoom i rodiči.
   - Pan na 1m výchozím byl odpoledne s p95 33,4 ms na hranici. Ve větším dni v 19:30 má
     37,4–37,5 v obou pohledech.
2. **E-2a.7 #1420 Mode a Scale < 100 ms.**
   - S konturami Major má 1m výchozí p95 155–165 ms. Proti stavu bez kontur přibude výpočet
     kontur ve workeru, druhý render a druhé kolo `drawStatic` (+42–70 ms v p50, nerozloženo).
     To je rozsah E-2a.7.
   - Čistý pohled na 1m nesplní cíl ani bez kontur (138–153 ms), kvůli +50–100 ms renderu
     Reactu. Stejný přírůstek má v Čistém i minutový update. To je mimo dnešní rozsah E-2a.7
     a rozhodne o tom vlastník (Stav #1412).
3. **E-2a.8 #1421 minutový update jako jedna úloha.**
   - Update chodí 3–4× za minutu a každý přestaví den i grid.
   - Na 1m výchozím má každý update p95 128–173 ms a součet za minutu p95 374–435 ms.
   - V 1m Čistém má každý update p95 278 ms, takže tam cíl nesplní ani jednotlivý update.
   - Cíl „jedna úloha < 250 ms“ dnes nesplní žádná kombinace. AC #1421 měří jen 1m (výchozí).
     Zda má zahrnout i Čistý pohled, rozhodne vlastník (Stav #1412).
4. **#1536 `/news/stats` jen pro aktivní symbol** (rozhodnuto C). Snímky neovlivňuje, ale
   stahuje 3,8 MB za minutu s TTFB 2,2–2,7 s. Je malý a nezávislý. Zdržení ostatních pollů
   je jen podezření (viz Pozorování).
5. **E-2a.3 #1413 memo spodních panelů:** Čistý pohled bez panelů má při panu stejný p50
   (24,9 ms), očekávaný zisk je tedy malý. Rozhodnout až po E-2a.6.
6. **E-2a.5 #1417 ořez a LOD overlay:** `drawStatic` dělá 5,6–14,6 ms snímku na 1m. Po E-2a.6,
   pokud pan a zoom pořád nesplní cíl.
7. **E-2a.9 #1422 Blobs a jediný polling paper účtu:** přepnutí Gradient ↔ Blobs trvá 33 / 52 ms
   na 1m a cíl splňuje. `/paper/account` pollují dva hooky po 10 s (`App.tsx:675`,
   `InstrumentHeader.tsx:171`), tedy 12 požadavků za minutu. Je to síťová hygiena spolu s #1536.
8. **E-2a.4 #1416 tick bez `drawStatic`:** cíl tick < 250 ms je splněný a `drawStatic` na tick
   stojí ~0,25 s za minutu. Nejnižší priorita.

Načtení dne (TTFB 7–12 s, `dayDecode` 1,5–2,4 s na hlavním vlákně) do E-2a.3–E-2a.9 nepatří. Jde
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
- **Doměření.** Pasivní okna na 5m se zkrátila na 5–6 min, aby se do konce RTH vešlo
  kontrolní okno 1m výchozí ve stejné hodině. Bez něj by vliv Čistého pohledu nešel oddělit od
  velikosti dne. Na konci `pb.restore()` vrátil volby i oba klíče uloženého pohledu
  (`same` i `restoredView` = true).

**Zkouška sběrače nanečisto** (11:39–11:41 UTC, před RTH, ES 1m výchozí, kontury Off). Ověřila
jen sběrač, čísla nejsou baseline:
- souběžně běžely dva cargo buildy jiného projektu (207–213 % CPU), v RTH už ne;
- studené přepnutí NQ → ES trvalo 20,5 s (TTFB 17,6 s), v RTH 12,4 s.

**`dayDecode`** má 9 vzorků, všechny jako jedna dlouhá úloha:
- 1 132 ms při nasazení #1409;
- 2,0 s při zkoušce nanečisto;
- 2 310, 1 492 a 1 656 ms v RTH odpoledne;
- 2 245, 2 165, 2 438 a 1 978 ms při doměření (dorovnání po teplém přepnutí).

**Nález #1536:**
- Hlavní pohled stahuje každou minutu celé `/news/stats` (3,8 MB, gzip 481 kB, 10 623 řádků),
  ale potřebuje z něj 138 řádků.
- V RTH měl TTFB 2,2–2,7 s v prohlížeči.
- Podezření, neověřené: zdržuje ostatní polly. `/paper/account` měl při zkoušce nanečisto
  (ne v RTH) TTFB 1,9–3,0 s v prohlížeči a 0,31 s z hostitele. Jestli je příčinou fronta
  spojení HTTP/1.1 v prohlížeči, nebo API zaneprázdněné odpovědí `/news/stats`, měření
  nerozlišilo.
- Snímky to neovlivňuje: dlouhý snímek s parsováním `/news/stats` LoAF nezachytil.
