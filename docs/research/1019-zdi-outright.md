# Zdi podle podílu outright objemu — kalibrace (#1019)

> E-0.7 Fáze 0 (#1387) epiky #1385 · #1007 krok 4 · seance 21. 9.–7. 10. 2026 (13 obchodních
> seancí ES i NQ, po opravě front kontraktu #1189 a zápisu `printvol` bez ohledu na α #1178) ·
> skript `scripts/measure_wall_outright_1019.py` (jen čtení partic, pevné semínko) · brána pro
> % outright v MCP (E-3.9 #1439) a v ranním snapshotu úrovní (E-5.7).

## Verdikt

- **Nepotvrzeno.** GEX zdi (tytéž čte wall_bounce), 223 rozhodnutých doteků s měřeným podílem:
  rozdíl podílu „drží" outright ≥ 40 % − pod 40 %, sdružený přes RTH a noc (Mantel–Haenszel),
  **+5,5 p. b., bootstrap po seancích [−4,0; +14,3]** — dolní mez pod nulou, síto neprošlo.
  Kontrola „přebytek drží nad nulovou pravděpodobností" (vliv polohy doteku odstraněn):
  +4,6 p. b. [−4,5; +12,9].
- **Vrstvy:** RTH 74,8 % (77/103) × 68,1 % (47/69), +6,6 p. b. [−6,8; +20,5]; noc 90,0 % (9/10)
  × 90,2 % (37/41). Nulová pravděpodobnost „drží" (z polohy close doteku) je v obou skupinách
  stejná (~77 %), skupiny tedy startují ze stejného místa.
- **Dominance zdi rozdíl nevysvětluje ani nenese:** v RTH s dominancí nad mediánem −0,3 p. b.,
  pod mediánem +12,0 p. b. [−6,5; +29,7] — obojí neprůkazné.
- Popisně (mnoho srovnání, nic samostatně): call zdi +10,8 p. b. [−2,2; +23,6], put zdi
  −16,3 p. b. [−34,0; +3,1] — opačné směry, ES −1,2 a NQ +0,4 p. b.
- **OI zdi** (`oiwalls`) jsou od ceny většinou daleko: 15 rozhodnutých doteků za 13 seancí — na
  verdikt nestačí.
- **Vedlejší pozorování (netestováno):** GEX zdi 0DTE v tomto vzorku „drží" (±9 bp, 30 min)
  v 76 %, tedy stejně, jak by vyšlo z náhodné procházky ze stejné polohy (77 %). Podíl outright
  na tom nic nemění.
- **Důsledek podle zadání #1019:** záporný verdikt → podíl outright zůstává jen informativní
  popisek (profil dvěma tóny, popisky zdí a žebříku — #1014, #1015); žádný práh, tečkování ani
  váha v detektorech; pás C a heatmapa D se nezakládají. Varianty k rozhodnutí vlastníka v #1019.

## Oprava metody proti prvnímu běhu (8. 10.)

První běh (stažený komentář v #1019) měl dvě chyby, které našel nezávislý ověřovatel:
1. **Práh „drží" ležel na úrovni doteku** (dotek i „drží" 4,5 bp od zdi): víc než polovina doteků
   „držela" už na dalším baru a podíl „drží" (~85 %) byl z velké části mechanický.
2. **Sloučené RTH a noc**, ačkoli zadání chtělo vrstvy zvlášť: skupina outright ≥ 40 % byla
   z 86 % RTH, skupina pod 40 % z 55 % — a noc „drží" víc sama od sebe. Proto celkově +4,2 p. b.
   a v RTH zdánlivě průkazných +11,4 p. b.

Oprava (odchylka od parametrů zvolených před prvním během, uvedená výslovně):
- bariéry symetricky kolem zdi **±9 bp** (2× tolerance doteku), „drží" = close pod zdí o 9 bp;
- **nulová pravděpodobnost** „drží" pro každý dotek z polohy close doteku mezi bariérami
  (náhodná procházka bez driftu) a „přebytek drží" jako kontrola;
- **stratifikace RTH / noc** jako primární analýza (Mantel–Haenszel, bootstrap celých seancí);
- vynechán dotek mimo pokrytí `printvol` (12) a po settle 0DTE (15; setupy po settle nevznikají,
  #1324).

## Jak číst a co data neříkají

- Dotek = bar se přiblíží ke zdi na 4,5 bp ze správné strany (ES při ~7 650 ≈ 3,4 b, `wall_zone`
  šablony je 3 b; NQ ≈ 11 b). wall_bounce navíc vynechává zdi s dominancí pod 0,15 — tady jsou
  všechny zdi.
- Podíl outright = tisky / objem na striku a straně zdi od začátku seance do doteku (dnešní tok,
  point-in-time), ne složení OI, ze kterého zeď vzniká — proxy z #1007.
- Pokrytí `printvol` u nové 0DTE expirace začíná v 6 z 13 seancí až 00:01 UTC místo 22:00
  (roll pipeline o půlnoci UTC — #1331, nasazeno 7. 10.); 28. 9. až 15:07 UTC, 29. 9. končí 18:09.
  Doteky mimo pokrytí jsou vynechané, podíl v prvních hodinách je počítaný z kratšího okna.
- Track record wall_bounce, EM respect (#872) a gamma cliff (#576), které #1019 jmenuje jako další
  zdroje, nejsou události na striku zdi (EM, cliff) nebo mají malé n (wall_bounce v5 s printvol
  ~80 setupů) — srovnání by bylo slabší než doteky.
- Doteky jedné seance nejsou nezávislé — proto bootstrap celých seancí; Newcombe ve vrstvách bere
  doteky jednotlivě (spíš optimistický) a je jen popisný.

## Výstup skriptu

Parametry: dotek 4.5 bp, bariéry průraz / drží ±9.0 bp kolem zdi, znovu-ozbrojení 15.0 bp, horizont 30 min, min. objem na striku 100, práh 40,0 %, MIN_SAMPLE 30, bootstrap 10000× po seancích, semínko 1019.

### Seance

| symbol | seance | barů | printvol od – do (UTC) | NULL printed | doteků |
|---|---|---|---|---|---|
| ES | 2026-09-21 | 1380 | 09-20 22:01 – 09-21 21:04 | 0,0 % | 11 |
| NQ | 2026-09-21 | 1380 | 09-20 22:01 – 09-21 21:02 | 0,0 % | 18 |
| ES | 2026-09-22 | 1380 | 09-21 22:03 – 09-22 21:01 | 0,0 % | 5 |
| NQ | 2026-09-22 | 1380 | 09-21 22:05 – 09-22 21:06 | 0,0 % | 24 |
| ES | 2026-09-23 | 1380 | 09-22 22:05 – 09-23 21:05 | 0,0 % | 9 |
| NQ | 2026-09-23 | 1380 | 09-22 22:06 – 09-23 21:05 | 0,0 % | 19 |
| ES | 2026-09-24 | 1380 | 09-23 22:01 – 09-24 21:32 | 0,0 % | 11 |
| NQ | 2026-09-24 | 1380 | 09-23 22:01 – 09-24 21:27 | 0,0 % | 17 |
| ES | 2026-09-25 | 1380 | 09-25 00:01 – 09-25 21:28 | 0,0 % | 10 |
| NQ | 2026-09-25 | 1380 | 09-25 00:01 – 09-25 21:28 | 0,0 % | 18 |
| ES | 2026-09-28 | 1380 | 09-28 15:07 – 09-28 21:28 | 0,0 % | 2 |
| NQ | 2026-09-28 | 1380 | 09-28 15:07 – 09-28 21:33 | 0,0 % | 5 |
| ES | 2026-09-29 | 1380 | 09-29 00:01 – 09-29 18:09 | 0,0 % | 12 |
| NQ | 2026-09-29 | 1380 | 09-29 00:01 – 09-29 18:09 | 0,0 % | 24 |
| ES | 2026-09-30 | 1380 | 09-29 22:04 – 09-30 21:33 | 0,0 % | 22 |
| NQ | 2026-09-30 | 1380 | 09-29 22:04 – 09-30 21:33 | 0,0 % | 25 |
| ES | 2026-10-01 | 1380 | 10-01 00:02 – 10-01 21:28 | 0,0 % | 16 |
| NQ | 2026-10-01 | 1380 | 10-01 00:02 – 10-01 21:32 | 0,0 % | 16 |
| ES | 2026-10-02 | 1380 | 10-02 00:02 – 10-02 21:28 | 0,0 % | 3 |
| NQ | 2026-10-02 | 1380 | 10-02 00:02 – 10-02 21:33 | 0,0 % | 21 |
| ES | 2026-10-05 | 1380 | 10-04 22:01 – 10-05 21:33 | 0,1 % | 6 |
| NQ | 2026-10-05 | 1380 | 10-04 22:01 – 10-05 21:32 | 0,2 % | 14 |
| ES | 2026-10-06 | 1380 | 10-06 00:01 – 10-06 21:08 | 0,1 % | 4 |
| NQ | 2026-10-06 | 1380 | 10-06 00:01 – 10-06 21:07 | 0,2 % | 10 |
| ES | 2026-10-07 | 1380 | 10-06 22:03 – 10-07 20:00 | 0,1 % | 8 |
| NQ | 2026-10-07 | 1380 | 10-06 22:05 – 10-07 20:00 | 0,0 % | 9 |

### GEX zdi (levels)

| výsledek | doteků | z toho nízký objem na striku |
|---|---|---|
| drží | 189 | 19 |
| průraz | 59 | 6 |
| nerozhodnuto | 68 | 8 |

Vynecháno: mimo pokrytí printvol 12, po settle 0DTE 15.
Podíl outright na zdi při doteku: medián 40,1 %, kvartily 22,1 % – 50,2 %, n = 223.

#### Primárně: práh 40 %, stratifikováno RTH / noc — síta NEsplněna

| vrstva | drží — outright ≥ 40 % | nulová p | drží — outright < 40 % | nulová p | rozdíl high − low [Newcombe 95 %] |
|---|---|---|---|---|---|
| RTH | 77/103 = 74,8 % [65,6 %; 82,2 %] | 77,2 % | 47/69 = 68,1 % [56,4 %; 77,9 %] | 76,7 % | +6,6 p. b. [-6,8 p. b.; +20,5 p. b.] |
| noc | 9/10 = 90,0 % [59,6 %; 98,2 %] | 78,7 % | 37/41 = 90,2 % [77,5 %; 96,1 %] | 75,9 % | -0,2 p. b. [-31,2 p. b.; +15,0 p. b.] |
| celkem (nestratifikováno) | 86/113 = 76,1 % [67,5 %; 83,0 %] | 77,3 % | 84/110 = 76,4 % [67,6 %; 83,3 %] | 76,4 % | -0,3 p. b. [-11,4 p. b.; +10,9 p. b.] |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 113 / 110 | ✔ |
| drží: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po seancích 95 % — dolní mez > 0 | +5,5 p. b. [-4,0 p. b.; +14,3 p. b.] | ✘ |
| přebytek drží nad nulovou p: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po seancích 95 % (kontrola) | +4,6 p. b. [-4,5 p. b.; +12,9 p. b.] | ✘ |

#### Rozpad (popisný)

|  | drží — outright ≥ 40 % | drží — outright < 40 % | rozdíl [Newcombe 95 %] |
|---|---|---|---|
| ES | 40/53 = 75,5 % [62,4 %; 85,1 %] | 23/30 = 76,7 % [59,1 %; 88,2 %] | -1,2 p. b. [-18,6 p. b.; +18,8 p. b.] |
| NQ | 46/60 = 76,7 % [64,6 %; 85,6 %] | 61/80 = 76,2 % [65,9 %; 84,2 %] | +0,4 p. b. [-14,1 p. b.; +14,1 p. b.] |
| call zeď | 59/67 = 88,1 % [78,2 %; 93,8 %] | 51/66 = 77,3 % [65,8 %; 85,7 %] | +10,8 p. b. [-2,2 p. b.; +23,6 p. b.] |
| put zeď | 27/46 = 58,7 % [44,3 %; 71,7 %] | 33/44 = 75,0 % [60,6 %; 85,4 %] | -16,3 p. b. [-34,0 p. b.; +3,1 p. b.] |
| RTH, dominance ≥ medián 0,275 | 40/55 = 72,7 % [59,8 %; 82,7 %] | 19/26 = 73,1 % [53,9 %; 86,3 %] | -0,3 p. b. [-18,9 p. b.; +21,3 p. b.] |
| RTH, dominance < medián | 37/48 = 77,1 % [63,5 %; 86,7 %] | 28/43 = 65,1 % [50,2 %; 77,6 %] | +12,0 p. b. [-6,5 p. b.; +29,7 p. b.] |

### OI zdi (oiwalls)

| výsledek | doteků | z toho nízký objem na striku |
|---|---|---|
| drží | 14 | 2 |
| průraz | 3 | 0 |
| nerozhodnuto | 6 | 3 |

Vynecháno: mimo pokrytí printvol 1, po settle 0DTE 1.
Podíl outright na zdi při doteku: medián 45,9 %, kvartily 24,6 % – 51,3 %, n = 15.

#### Primárně: práh 40 %, stratifikováno RTH / noc — síta NEsplněna

| vrstva | drží — outright ≥ 40 % | nulová p | drží — outright < 40 % | nulová p | rozdíl high − low [Newcombe 95 %] |
|---|---|---|---|---|---|
| RTH | 5/7 = 71,4 % [35,9 %; 91,8 %] | 82,4 % | 2/2 = 100,0 % [34,2 %; 100,0 %] | 72,9 % | -28,6 p. b. [-64,1 p. b.; +40,3 p. b.] |
| noc | 1/1 = 100,0 % [20,7 %; 100,0 %] | 74,4 % | 4/5 = 80,0 % [37,6 %; 96,4 %] | 71,8 % | +20,0 p. b. [-61,0 p. b.; +62,4 p. b.] |
| celkem (nestratifikováno) | 6/8 = 75,0 % [40,9 %; 92,9 %] | 81,4 % | 6/7 = 85,7 % [48,7 %; 97,4 %] | 72,1 % | -10,7 p. b. [-46,7 p. b.; +30,4 p. b.] |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 8 / 7 | ✘ |
| drží: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po seancích 95 % — dolní mez > 0 | -11,6 p. b. [-50,0 p. b.; +14,9 p. b.] | ✘ |
| přebytek drží nad nulovou p: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po seancích 95 % (kontrola) | -18,7 p. b. [-54,1 p. b.; +2,9 p. b.] | ✘ |

#### Rozpad (popisný)

|  | drží — outright ≥ 40 % | drží — outright < 40 % | rozdíl [Newcombe 95 %] |
|---|---|---|---|
| ES | 1/2 = 50,0 % [9,5 %; 90,5 %] | 1/1 = 100,0 % [20,7 %; 100,0 %] | -50,0 p. b. [-90,5 p. b.; +39,1 p. b.] |
| NQ | 5/6 = 83,3 % [43,6 %; 97,0 %] | 5/6 = 83,3 % [43,6 %; 97,0 %] | +0,0 p. b. [-42,0 p. b.; +42,0 p. b.] |
| call zeď | 5/6 = 83,3 % [43,6 %; 97,0 %] | 4/5 = 80,0 % [37,6 %; 96,4 %] | +3,3 p. b. [-39,6 p. b.; +47,9 p. b.] |
| put zeď | 1/2 = 50,0 % [9,5 %; 90,5 %] | 2/2 = 100,0 % [34,2 %; 100,0 %] | -50,0 p. b. [-90,5 p. b.; +27,3 p. b.] |
| RTH, dominance ≥ medián 0,529 | 4/4 = 100,0 % [51,0 %; 100,0 %] | — | — |
| RTH, dominance < medián | 1/3 = 33,3 % [6,1 %; 79,2 %] | 2/2 = 100,0 % [34,2 %; 100,0 %] | -66,7 p. b. [-93,9 p. b.; +13,5 p. b.] |
