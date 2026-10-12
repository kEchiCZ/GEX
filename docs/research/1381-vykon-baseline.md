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
| den a čas (UTC) | — |
| build frontendu (`perfExport().build`) | — |
| data | — (symbol, expirace, obchodní den) |
| prohlížeč | — |
| okno | — (viewport, DPR, velikost canvasu) |
| obnovovací frekvence (brána) | — |
| CPU | i5-10300H, 16 GB; takty jader fixně 2 396 MHz (turbo vypnuté v plánu napájení) |
| teplota a příkon CPU | — (před a po, LibreHardwareMonitor) |
| kontejnery (`docker stats`) | — |
| další zátěž | — (další okna a záložky GEXLens, jiné buildy) |

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

  Pro každou část zapíše invoker skriptu (událost, zprávu Scheduleru Reactu, WebSocket, časovač);
- **gesta:** pouští skriptovaná gesta, takže přeměření E-2a.18 je srovnatelné krok za krokem.

**Scénáře** se měří na 1m a 5m, vždy ve výchozím i Čistém pohledu (#238):

| scénář | jak | metrika |
|---|---|---|
| pan | 3× tažení 300 px tam a zpět, jeden `pointermove` na snímek (60 kroků) | snímky p50/p95 |
| wheel zoom | 3× 12 kroků kolečka dovnitř a 12 ven, jeden krok na snímek | snímky p50/p95 |
| Mode | všechny módy a zpět na výchozí | span `modeScale` (do vykreslení kontur) |
| Scale | 2× všechny škály a zpět | span `modeScale` |
| Gradient / Blobs | 3× tam a zpět | změna → po vykreslení (rAF + úloha) |
| přepnutí symbolu | ES → NQ → ES → NQ → ES (první NQ studené) | span `symbolSwitch`, fáze `dayFetch`/`dayDecode`/`dayBuild` |
| načtení dne, studené | reload stránky | navigace, resource timing `/api/replay`, bufferované LoAF |
| minutový update | pasivně, ≥ 60 min | span `minuteUpdate`, dlouhé úlohy za minutu |
| živý tick | pasivně | LoAF s invokerem WebSocket, `drawStatic` a `drawDynamic` za minutu |
| hodinový refetch | pasivně (`LIVE_RECONCILE_MS` = 1 h) | `dayFetch`/`dayDecode`/`dayBuild`, span `dayLoad` |

**Brány a pojistky:**
- **Viditelnost:** scénář běží jen ve viditelné záložce. Klidový medián snímku musí být
  ≤ 17,5 ms, jinak je okno zakryté nebo škrcené a scénář se neměří.
- **Hranice minuty:** segment začíná mezi 15. a 40. sekundou minuty, mimo minutový flush.
  Segment, ve kterém přesto proběhl `minuteUpdate` nebo `dayLoad`, je označený jako zkreslený.
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

**Postup:**
```text
# konzole produkce (:8080), viditelné okno Chrome, jediná záložka GEXLens
<obsah scripts/perf_baseline_1412.js>
pb.start()
await pb.run('1m', false); await pb.run('1m', true)
await pb.run('5m', false); await pb.run('5m', true)
# pasivně ≥ 60 min od načtení záložky (hodinový refetch), mezitím 2–3 exporty:
JSON.stringify(gexlensPerf.perfExport(undefined, { records: false }))
JSON.stringify(pb.report())
await pb.restore(); pb.stop()
# studené načtení dne: reload, znovu vložit sběrač, pak
await pb.coldLoad()
```
```bash
bash scripts/perf_baseline_load.sh "před 1m výchozí"   # docker stats + LHM, před a po každém bloku
```

## Výsledky

### Cíle ot. 2

| cíl | scénář | 1m výchozí | 1m Čistý | 5m výchozí | 5m Čistý | splněno |
|---|---|---|---|---|---|---|
| p95 snímku ≤ 33 ms | pan | — | — | — | — | — |
| p95 snímku ≤ 33 ms | wheel zoom | — | — | — | — | — |
| < 100 ms vč. kontur | Mode | — | — | — | — | — |
| < 100 ms vč. kontur | Scale | — | — | — | — | — |
| 1 úloha < 250 ms | minutový update | — | — | — | — | — |
| < 250 ms | živý tick | — | — | — | — | — |
| medián ≤ 16,7 ms (cíl) | pan, zoom na 5m | | | — | — | — |

### Aktivní scénáře

—

### Minutový update, tick a hodinový refetch

—

### Načtení dne a přepnutí symbolu (vstup ADR-0047)

—

### Rozpad dlouhých snímků (LoAF)

—

### Exporty měřiče

—

## Pořadí oprav E-2a.3–E-2a.9

—

## Pozorování

- Při nasazení #1409 (12. 10. 09:52 UTC) měl `dayDecode` jeden vzorek 1 132 ms jako jednu
  dlouhou úlohu. Je to n = 1, proto se z něj nevyvozuje nic.
