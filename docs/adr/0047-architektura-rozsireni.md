# ADR-0047: Architektura rozšíření — evoluce repa, PWA bez service workeru, launcher, bod návratu, aditivní schéma, clean-room a výkonové cíle grafu

- **Stav:** navrženo (E-1.5 #1391). Body 1–6 zapisují rozhodnutí vlastníka ze 7. a 12. 10. 2026
  v epice #1385. Bod 7 (načtení dne a přepnutí symbolu) čeká na rozhodnutí vlastníka.
- **Datum:** 2026-10-12
- **Souvisí:** #1385 (epika), #1381 (syrové zadání), #1274 a #1386 (Fáze 2a), #1412 (baseline
  `docs/research/1381-vykon-baseline.md`), #1409 (měřič), #1393 (pin image, E-1.6), #1396 (bod
  návratu, E-0.9), #490 (WebGL), SPEC 7.2 a kap. 8, ADR-0001, ADR-0029

## Kontext

Epika #1385 rozšiřuje GEXLens o svižný graf, MCP pro Clauda, obchodníka po branách, úrovně přes
všechny expirace, kontext sentimentu a CZ+EN. Vlastník chce aplikaci „hezkou a rychlou“, „když
kliknu, tak je hotovo“, na notebooku, který má (16 GB, WSL 6 GB), a možnost vrátit se k dnešní
verzi, kdyby rozšíření nevyšlo.

Tvrdá data:
- **Graf na 1m (#1274, 24. 9.):** pan 74 ms (p95 154 ms), Mode 104 ms, minutový update
  0,65–0,8 s. Rasterizace trvala 0,03 ms, čas byl v překreslování a re-renderu Reactu.
  Měřeno ve skryté záložce a na buildu před měřičem, čísla jsou 2–7× vyšší než baseline
  (`docs/research/1381-vykon-baseline.md`, úvod).
- **Baseline v RTH (#1412, 12. 10., build `f713920`):** pan a zoom na 1m p95 29–58 ms; Mode/Scale
  na 1m 57–62 ms bez kontur, 155–165 ms s konturami Major, v Čistém pohledu 138–153 ms bez
  kontur; minutový update 3–4× za minutu, každý p95 128–278 ms; v klidu 229–235 fps.
- **Načtení dne (#1412):** `/api/replay` má TTFB 7–12 s (78 % času `dayLoad`), balík 4,7–7,4 MB
  gzip; `dayDecode` je jedna úloha 1,5–2,4 s na hlavním vlákně; studené načtení stránky začne
  `/api/replay` až 4,8 s po navigaci a data dne má za 12,2 s; studené přepnutí symbolu
  12,4 s; hodinový refetch 8,1–9,2 s. Teplé přepnutí ES ↔ NQ vykreslí z cache za 98–379 ms
  (379 ms v 1m Čistém, zkreslené minutovým updatem; doměření 116–294 ms), plné dorovnání
  (refetch + decode) skončí za 14–15,5 s.
- SPEC 7.2 žádá „60 fps pan/zoom“ bez metody měření; SPEC kap. 8 „změna módu/škály < 100 ms“
  a „live tick < 250 ms end-to-end“.

## Rozhodnutí

1. **Evoluce stávajícího repa s bodem návratu (A3, vlastník 7. 10.).** Žádný nový projekt: narazil
   by na R1, migroval by věčná data (R4), ztratil by testy a golden datasety a jako druhá
   produkce by soupeřil o 100 market data lines a jednu IBKR relaci (ADR-0001). Bod návratu před
   první změnou enginu, API nebo replaye ve Fázi 2a:
   - git tag `stav-pred-1381-<datum>` a GitHub release s digesty obou image (E-0.9 #1396);
   - záloha PG (`scripts/backup-postgres.ps1`) a kopie `data/` s hashem;
   - návrat jedním příkazem přes pin tagu image v compose (E-1.6 #1393).
   Výjimka 12. 10. (vlastník): nasazení jen frontendu E-2a.1a šlo bez brány, se zálohou běžícího
   image (`gex-frontend:pre-1409`).
2. **Web + PWA bez service workeru + launcher (B1, vlastník 7. 10.).** Aplikace zůstává webová.
   `scripts/start-prod.ps1` spustí Docker Desktop, počká na `/api/status` (toleruje zavřený trh
   a údržbu VHDX) a otevře okno aplikace (E-2a.14). PWA manifest dává vlastní okno a ikonu
   (E-2a.15). **Service worker nevzniká** a test to hlídá: cache SW by po nasazení servírovala
   starý build a obcházela pin image z bodu 1, a data musí být vždy čerstvá (stáří viditelné,
   SPEC 3.7). Desktopový obal (Tauri, Electron) ani nativní běh bez Dockeru nevzniká.
3. **Vlastní renderer s vestavěným měřením (C1, vlastník 7. a 12. 10.).** Graf se opravuje
   cíleně podle měření, ne výměnou technologie:
   - měřič `frontend/src/perf/meter.ts` (#1409) je v produkčním buildu, ve výchozím stavu
     vypnutý a **bez UI** (overlay, Diagnostika ani „Seklo to“ nevznikají, #1411). Ovládá se
     z konzole (`window.gexlensPerf`) a z Playwrightu;
   - render Reactu se rozkládá spany měřiče v opravných PR, ne profiling buildem (12. 10.);
   - cizí knihovna grafu ani WebGL (#490) bez změřeného hrdla v rasterizaci nevzniká.
4. **Aditivní změny schématu a uloženého stavu.** Kód před epikou musí po návratu přes bod 1 běžet
   nad stejnou databází a stejným prohlížečem:
   - PG: jen nové tabulky a sloupce (nullable nebo s defaultem), žádné `DROP`, `RENAME` ani změna
     typu bez vlastního ADR; před migrací záloha PG;
   - `localStorage`: nové klíče vedle starých, starý klíč se nemaže.
5. **Clean-room.** Funkce se staví z veřejných popisů, dokumentace, vlastního kódu a vlastního
   měření. Dekompilace, rozbalování instalátoru, odposlech komunikace cizí aplikace a obcházení
   licence nebo předplatného se nedělají. Jméno referenční aplikace a srovnávací matice
   nepatří do kódu, docs, commitů, PR, Playwright snapshotů ani do issues; snímky jejího UI
   zůstávají jen v #1381, #1342 a #1359. Syrové zadání se přepisuje do očištěné epiky
   (skill `epika`).
6. **Výkonové cíle grafu (ot. 2, varianta B, vlastník 7. 10.).** Měří se na notebooku vlastníka,
   ve viditelném okně, v RTH, nad produkčními daty, ve výchozím i Čistém pohledu (protokol
   E-2a.18 #1431; pasivní okna 4 × 15 min v jednom úseku RTH, rozhodnutí 12. 10.):

   | | Cíl | Čím se měří (měřič #1409) |
   |---|---|---|
   | tvrdě | p95 snímku ≤ 33 ms při pan i zoom na 1m | `frame` ze snímků gesta |
   | tvrdě | přepnutí Mode/Scale < 100 ms včetně kontur | span `modeScale` (končí posledním `drawStatic` po konturách) |
   | tvrdě | minutový update jako jedna úloha < 250 ms | span `minuteUpdate`, 1 span za minutu, `longtask` |
   | tvrdě | živý tick < 250 ms (klientská část) | `frame` > 250 ms mimo spany `minuteUpdate` a `dayLoad` = 0 |
   | cíl | medián snímku ≤ 16,7 ms při pan a zoom na 5m | `frame` ze snímků gesta |

   Měřič vidí jen hlavní vlákno prohlížeče; cestu engine → WS → klient neměří, takže tick
   se ověřuje v klientské části. Dekódování dne při refetchi pokrývá bod 7.
   fps se v CI neměří (stroj CI není notebook). CI hlídá deterministická počítadla v Playwright
   trace na syntetickém dni (E-2a.13 #1426): `drawStatic` na tick a po Mode/Scale, re-render
   panelů při panu a úlohy za minutový update.
   Nesplněný cíl po přeměření E-2a.18 otevře jen odpovídající řádek Fáze 2b (#1386).
7. **Načtení dne a přepnutí symbolu (navrženo, k rozhodnutí vlastníka).** Předběžná hranice
   „< 1 s“ z #1391 platí podle baseline jen pro vykreslení z cache. Návrh (varianta A):

   | | Cíl | Dnes (#1412) | Čím se měří |
   |---|---|---|---|
   | tvrdě | teplé přepnutí na symbol v paměti: vykreslení < 300 ms | 98–379 ms (doměření 116–294) | span `symbolSwitch` |
   | tvrdě | načtení a dorovnání dne bez úlohy > 250 ms na hlavním vlákně | `dayDecode` 1,5–2,4 s | `dayDecode`, `longtask` |
   | cíl | studené načtení stránky do dat dne < 3 s | 12,2 s (přepnutí 12,4 s, refetch 8,1–9,2 s) | od navigace: Navigation a Resource Timing (`pb.coldLoad()` sběrače `scripts/perf_baseline_1412.js`) |

   - Splnění vyžaduje dekódování mimo hlavní vlákno (2b.1 v #1386), kratší TTFB `/api/replay`
     (dnes 78 % `dayLoad`; #1547) a rozpad 4,8 s před startem `/api/replay` při studeném
     načtení (#1548).
   - Rychlé přepnutí tam a zpět nesmí dekódovat fetch, který už nikdo nečte
     (`replay/loader.ts:477–482`, kontrola zrušení před `decodeBundle`; #1548).
   - Varianty, které vlastník zvažuje: **A** (tabulka výše); **B** jedna hranice < 1 s pro
     studené i teplé načtení (vyžaduje přestavbu formátu a serveru replaye, mimo Fázi 2a);
     **C** bez hranice, jen měřit (12 s TTFB by zůstalo bez cíle).
8. **Vztah ke SPEC.** ADR má přednost před textem SPEC, na který se odkazuje:
   - SPEC 7.2 „60 fps pan/zoom“ nahrazuje bod 6 (16,7 ms zůstává cílem na 5m, tvrdá hranice
     na 1m je 33 ms);
   - SPEC kap. 8 Latence: „změna módu/škály < 100 ms“ platí včetně kontur; „live tick < 250 ms
     end-to-end“ beze změny (měřič ověřuje klientskou část, bod 6); přibývá načtení dne podle bodu 7.

## Důsledky

- Každá oprava grafu ve Fázi 2a uvádí čísla měřiče před a po v komentáři #1274; cíl bez čísla
  se nepovažuje za splněný.
- Fáze 2b vznikne jen pro cíle, které přeměření E-2a.18 neprokáže; bez nesplněného cíle se
  nezakládá.
- PWA bez service workeru: offline režim aplikace nemá; bez API ukáže viditelný stav
  „nedostupné“ (#1355), ne starý obsah.
- Aditivní schéma znamená, že staré sloupce a klíče zůstávají, i když je nový kód nečte.
  Úklid jde jen samostatným ADR po uzavření epiky.
- Návrat přes bod 1 vrací kód a image, ne data: řádky zapsané po bodu návratu zůstávají
  (data uživatele se nemažou, AGENTS.md; věčný archiv R4, ADR-0029).

## Zvažované varianty (7. 10., detail v historii #1385)

- **A1/A2 nový projekt nebo fork:** zamítnuto (R1, migrace věčných dat, druhá produkce o 100 lines).
- **B2 desktopový obal (Tauri/Electron), B3 nativní běh:** zamítnuto; rychlost dává oprava
  vykreslování a dat, ne obal. Obal by přidal build a aktualizace bez změřeného přínosu.
- **C2 cizí knihovna grafu, C3 WebGL hned:** zamítnuto bez změřeného hrdla; rasterizace měla
  24. 9. 0,03 ms.
