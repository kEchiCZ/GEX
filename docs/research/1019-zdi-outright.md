# Zdi podle podílu outright objemu — kalibrace (#1019)

> E-0.7 Fáze 0 (#1387) epiky #1385 · #1007 krok 4 · seance 21. 9.–7. 10. 2026 (13 obchodních
> seancí ES i NQ, po opravě front kontraktu #1189 a zápisu `printvol` bez ohledu na α #1178) ·
> skript `scripts/measure_wall_outright_1019.py` (jen čtení partic, pevné semínko) · brána pro
> % outright v MCP (E-3.9 #1439) a v ranním snapshotu úrovní (E-5.7).

## Verdikt

- **Nepotvrzeno.** GEX zdi (tytéž čte wall_bounce), 343 doteků, 296 rozhodnutých s měřeným
  podílem: zdi s outright ≥ 40 % drží v 87,9 % (131/149), zdi pod 40 % v 83,7 % (123/147).
  Rozdíl +4,2 p. b., Newcombe 95 % [−3,8; +12,3], bootstrap po seancích [−4,3; +11,9] —
  dolní mez pod nulou, síta neprošla. Dělení mediánem (40,1 %) dává totéž.
- **Zavádějící faktor:** skupina s vyšším outright má i vyšší dominanci zdi (medián 0,319 × 0,271).
  Těch +4 p. b. tak může nést dominance (ta už dnes filtruje wall_bounce, `min_wall_dominance`).
- **OI zdi** (`oiwalls`) jsou od ceny většinou daleko: 25 doteků za 13 seancí, 19 s měřeným
  podílem — na verdikt nestačí.
- Rozpad (jen popisně, mnoho srovnání): v RTH 86,7 % × 75,3 %, call zdi 93,5 % × 84,4 %, put zdi
  78,9 % × 82,5 %, noc 95,2 % × 93,9 %. Nic z toho není samostatný nález.
- **Důsledek podle zadání #1019:** záporný verdikt → podíl outright zůstává jen informativní
  popisek (profil dvěma tóny, popisky zdí a žebříku — #1014, #1015); žádný práh, tečkování ani
  váha v detektorech, pás C a heatmapa D se nezakládají. Varianty k rozhodnutí vlastníka v #1019.

## Jak číst a co data neříkají

- Událost = dotek zdi ze správné strany (bar se přiblíží na 4,5 bp; ES ≈ 3 body = `wall_zone`
  šablony), výsledek do 30 min nad úrovní zdi v okamžiku doteku: průraz = close za zdí o ≥ 4,5 bp,
  drží = close zpět od zdi o ≥ 4,5 bp. Parametry byly zvolené předem, před prvním během.
- **Absolutní podíl „drží" je nadsazený:** dotek stačí high/low, close doteku tak obvykle leží
  ještě před zdí a k prahu „drží" má blíž než k prahu průrazu. Pro srovnání skupin to nevadí
  (platí pro obě stejně), ale ~85 % není „zdi drží v 85 %". Strop u vysokého podílu zároveň
  snižuje sílu testu: interval rozdílu je ±8 p. b.; efekt +4 p. b. by potřeboval zhruba
  čtyřnásobek dat (~50 seancí).
- Podíl outright = tisky / objem na striku a straně zdi od začátku seance do doteku (dnešní tok,
  point-in-time), ne složení OI, ze kterého zeď vzniká. Proxy z #1007 — přímé složení OI nejde
  změřit.
- Doteky jedné seance nejsou nezávislé — proto bootstrap, který losuje celé seance; Newcombe je
  bere jednotlivě (spíš optimistický), a přesto neprošel.

## Výstup skriptu

Parametry: dotek 4.5 bp, průraz / drží 4.5 bp, znovu-ozbrojení 15.0 bp, horizont 30 min, min. objem na striku 100, práh 40,0 %, MIN_SAMPLE 30, bootstrap 10000× po seancích, semínko 1019.

### Seance

| symbol | seance | stav |
|---|---|---|
| ES | 2026-09-21 | barů 1380, doteků 11, printvol NULL 0,0 % |
| NQ | 2026-09-21 | barů 1380, doteků 19, printvol NULL 0,0 % |
| ES | 2026-09-22 | barů 1380, doteků 6, printvol NULL 0,0 % |
| NQ | 2026-09-22 | barů 1380, doteků 24, printvol NULL 0,0 % |
| ES | 2026-09-23 | barů 1380, doteků 9, printvol NULL 0,0 % |
| NQ | 2026-09-23 | barů 1380, doteků 19, printvol NULL 0,0 % |
| ES | 2026-09-24 | barů 1380, doteků 11, printvol NULL 0,0 % |
| NQ | 2026-09-24 | barů 1380, doteků 19, printvol NULL 0,0 % |
| ES | 2026-09-25 | barů 1380, doteků 12, printvol NULL 0,0 % |
| NQ | 2026-09-25 | barů 1380, doteků 18, printvol NULL 0,0 % |
| ES | 2026-09-28 | barů 1380, doteků 3, printvol NULL 0,0 % |
| NQ | 2026-09-28 | barů 1380, doteků 12, printvol NULL 0,0 % |
| ES | 2026-09-29 | barů 1380, doteků 12, printvol NULL 0,0 % |
| NQ | 2026-09-29 | barů 1380, doteků 31, printvol NULL 0,0 % |
| ES | 2026-09-30 | barů 1380, doteků 23, printvol NULL 0,0 % |
| NQ | 2026-09-30 | barů 1380, doteků 27, printvol NULL 0,0 % |
| ES | 2026-10-01 | barů 1380, doteků 16, printvol NULL 0,0 % |
| NQ | 2026-10-01 | barů 1380, doteků 18, printvol NULL 0,0 % |
| ES | 2026-10-02 | barů 1380, doteků 4, printvol NULL 0,0 % |
| NQ | 2026-10-02 | barů 1380, doteků 22, printvol NULL 0,0 % |
| ES | 2026-10-05 | barů 1380, doteků 7, printvol NULL 0,1 % |
| NQ | 2026-10-05 | barů 1380, doteků 14, printvol NULL 0,2 % |
| ES | 2026-10-06 | barů 1380, doteků 4, printvol NULL 0,1 % |
| NQ | 2026-10-06 | barů 1380, doteků 10, printvol NULL 0,2 % |
| ES | 2026-10-07 | barů 1380, doteků 8, printvol NULL 0,1 % |
| NQ | 2026-10-07 | barů 1380, doteků 9, printvol NULL 0,0 % |

### GEX zdi (levels)

| výsledek | doteků | z toho nízký objem na striku |
|---|---|---|
| drží | 284 | 30 |
| průraz | 53 | 11 |
| nerozhodnuto | 6 | 2 |

Podíl outright na zdi při doteku: medián 40,1 %, kvartily 22,1 % – 50,3 %, n = 296.

#### Dělení: předregistrovaný práh 40,0 % — síta NEsplněna

| skupina | n rozhodnutých | drží [Wilson 95 %] | medián dominance |
|---|---|---|---|
| outright ≥ 40,0 % | 149 | 131/149 = 87,9 % [81,7 %; 92,2 %] | 0,319 |
| outright < 40,0 % | 147 | 123/147 = 83,7 % [76,9 %; 88,8 %] | 0,271 |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 149 / 147 | ✔ |
| rozdíl „drží“ high − low, Newcombe 95 % — dolní mez > 0 | 4,2 % [-3,8 %; 12,3 %] | ✘ |
| týž rozdíl, bootstrap po seancích — dolní mez > 0 | [-4,3 %; 11,9 %] | ✘ |

#### Dělení: medián (kontrola) 40,1 % — síta NEsplněna

| skupina | n rozhodnutých | drží [Wilson 95 %] | medián dominance |
|---|---|---|---|
| outright ≥ 40,1 % | 148 | 130/148 = 87,8 % [81,6 %; 92,2 %] | 0,318 |
| outright < 40,1 % | 148 | 124/148 = 83,8 % [77,0 %; 88,9 %] | 0,271 |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 148 / 148 | ✔ |
| rozdíl „drží“ high − low, Newcombe 95 % — dolní mez > 0 | 4,1 % [-4,0 %; 12,1 %] | ✘ |
| týž rozdíl, bootstrap po seancích — dolní mez > 0 | [-4,4 %; 11,5 %] | ✘ |

#### Rozpad (popisný, práh 40 %)

|  | drží — outright ≥ 40 % | drží — outright < 40 % | drží — všechny doteky |
|---|---|---|---|
| ES | 63/71 = 88,7 % [79,3 %; 94,2 %] | 39/48 = 81,2 % [68,1 %; 89,8 %] | 104/121 = 86,0 % [78,6 %; 91,0 %] |
| NQ | 68/78 = 87,2 % [78,0 %; 92,9 %] | 84/99 = 84,8 % [76,5 %; 90,6 %] | 180/216 = 83,3 % [77,8 %; 87,7 %] |
| RTH | 111/128 = 86,7 % [79,8 %; 91,5 %] | 61/81 = 75,3 % [64,9 %; 83,4 %] | 176/217 = 81,1 % [75,4 %; 85,8 %] |
| noc | 20/21 = 95,2 % [77,3 %; 99,2 %] | 62/66 = 93,9 % [85,4 %; 97,6 %] | 108/120 = 90,0 % [83,3 %; 94,2 %] |
| call zeď | 86/92 = 93,5 % [86,5 %; 97,0 %] | 76/90 = 84,4 % [75,6 %; 90,5 %] | 174/199 = 87,4 % [82,1 %; 91,3 %] |
| put zeď | 45/57 = 78,9 % [66,7 %; 87,5 %] | 47/57 = 82,5 % [70,6 %; 90,2 %] | 110/138 = 79,7 % [72,2 %; 85,6 %] |

### OI zdi (oiwalls)

| výsledek | doteků | z toho nízký objem na striku |
|---|---|---|
| drží | 22 | 5 |
| průraz | 3 | 1 |
| nerozhodnuto | 0 | 0 |

Podíl outright na zdi při doteku: medián 36,5 %, kvartily 24,8 % – 49,3 %, n = 19.

#### Dělení: předregistrovaný práh 40,0 % — síta NEsplněna

| skupina | n rozhodnutých | drží [Wilson 95 %] | medián dominance |
|---|---|---|---|
| outright ≥ 40,0 % | 9 | 8/9 = 88,9 % [56,5 %; 98,0 %] | 0,690 |
| outright < 40,0 % | 10 | 9/10 = 90,0 % [59,6 %; 98,2 %] | 0,492 |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 9 / 10 | ✘ |
| rozdíl „drží“ high − low, Newcombe 95 % — dolní mez > 0 | -1,1 % [-34,5 %; 30,6 %] | ✘ |
| týž rozdíl, bootstrap po seancích — dolní mez > 0 | [-35,2 %; 20,0 %] | ✘ |

#### Dělení: medián (kontrola) 36,5 % — síta NEsplněna

| skupina | n rozhodnutých | drží [Wilson 95 %] | medián dominance |
|---|---|---|---|
| outright ≥ 36,5 % | 10 | 9/10 = 90,0 % [59,6 %; 98,2 %] | 0,651 |
| outright < 36,5 % | 9 | 8/9 = 88,9 % [56,5 %; 98,0 %] | 0,455 |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 10 / 9 | ✘ |
| rozdíl „drží“ high − low, Newcombe 95 % — dolní mez > 0 | 1,1 % [-30,6 %; 34,5 %] | ✘ |
| týž rozdíl, bootstrap po seancích — dolní mez > 0 | [-24,2 %; 20,0 %] | ✘ |

#### Rozpad (popisný, práh 40 %)

|  | drží — outright ≥ 40 % | drží — outright < 40 % | drží — všechny doteky |
|---|---|---|---|
| ES | 2/2 = 100,0 % [34,2 %; 100,0 %] | 1/1 = 100,0 % [20,7 %; 100,0 %] | 3/3 = 100,0 % [43,8 %; 100,0 %] |
| NQ | 6/7 = 85,7 % [48,7 %; 97,4 %] | 8/9 = 88,9 % [56,5 %; 98,0 %] | 19/22 = 86,4 % [66,7 %; 95,3 %] |
| RTH | 7/8 = 87,5 % [52,9 %; 97,8 %] | 2/3 = 66,7 % [20,8 %; 93,9 %] | 9/11 = 81,8 % [52,3 %; 94,9 %] |
| noc | 1/1 = 100,0 % [20,7 %; 100,0 %] | 7/7 = 100,0 % [64,6 %; 100,0 %] | 13/14 = 92,9 % [68,5 %; 98,7 %] |
| call zeď | 6/7 = 85,7 % [48,7 %; 97,4 %] | 7/8 = 87,5 % [52,9 %; 97,8 %] | 17/20 = 85,0 % [64,0 %; 94,8 %] |
| put zeď | 2/2 = 100,0 % [34,2 %; 100,0 %] | 2/2 = 100,0 % [34,2 %; 100,0 %] | 5/5 = 100,0 % [56,6 %; 100,0 %] |
