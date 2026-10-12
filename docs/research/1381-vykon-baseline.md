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
| obnovovací frekvence (brána) | ~240 Hz (klidový snímek 4,2 ms při zkoušce 12. 10.) |
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
| přepnutí symbolu, studené | po reloadu na NQ první přepnutí na ES (`pb.run` začíná přepnutím na ES) | span `symbolSwitch`, fáze `dayFetch`/`dayDecode`/`dayBuild` (v pasivním okně) |
| přepnutí symbolu, teplé | ES → NQ → ES → NQ → ES, balíky dne už v paměti | span `symbolSwitch` |
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
- Při 240 Hz posílá skriptovaný pan 240 `pointermove` za sekundu. Myš dává totéž, protože
  Chrome zarovnává události na snímek. Na 60Hz displeji by gesto stejné délky mělo 4× méně
  kroků.

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

**Zkouška sběrače nanečisto** (12. 10., 11:39–11:41 UTC, před RTH, ES 1m, výchozí pohled,
kontury Off). Ověřila jen sběrač, čísla nejsou baseline:
- přepnutí NQ → ES studené: 20,5 s;
  - `dayFetch` 18,3 s: `/api/replay/ES`, TTFB 17,6 s, 4,5 MB, rozbaleně 23 MB;
  - `dayDecode` 2,0 s jako jedna dlouhá úloha;
- přepnutí ES ↔ NQ teplé: 268–315 ms (n = 4);
- pan: snímek p50 29 ms, p95 42 ms (n = 381), 1 `drawStatic` na krok;
- zoom: snímek p95 83 ms. LoAF ukazuje 1,2 s skriptu ve zprávách Scheduleru Reactu
  (`MessagePort.onmessage`) za 4,5 s;
- Mode: `modeScale` p50 53 ms, p95 80 ms (n = 9).

Souběžně běžely dva cargo buildy jiného projektu v Dockeru (207–213 % CPU). Během RTH měření
nesmí běžet.

**Nález #1536:** hlavní pohled stahuje každou minutu celé `/news/stats` (3,8 MB, 10 623 řádků,
TTFB 1,6–2,2 s), ale potřebuje z něj 138 řádků. Ostatní minutové polly čekají v prohlížeči
ve frontě HTTP/1.1. Například `/paper/account` má TTFB 1,9–3,0 s v prohlížeči a 0,31 s
z hostitele.

- Při nasazení #1409 (12. 10. 09:52 UTC) měl `dayDecode` jeden vzorek 1 132 ms jako jednu
  dlouhou úlohu. Je to n = 1, proto se z něj nevyvozuje nic.
