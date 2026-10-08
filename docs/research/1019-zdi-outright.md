# Zdi podle podílu outright objemu — kalibrace (#1019)

> E-0.7 Fáze 0 (#1387) epiky #1385 · #1007 krok 4 · seance 21. 9.–7. 10. 2026 (13 obchodních
> seancí ES i NQ, po opravě front kontraktu #1189 a zápisu `printvol` bez ohledu na α #1178) ·
> skript `scripts/measure_wall_outright_1019.py` (jen čtení partic, pevné semínko) · brána pro
> % outright v MCP (E-3.9 #1439) a v ranním snapshotu úrovní (E-5.7).

## Verdikt

- **Neprůkazné — 13 seancí nestačí.** U GEX zdí (tytéž čte wall_bounce) drží zdi s outright
  ≥ 40 % **bodově o +5 až +10 p. b. lépe** při všech zkoušených šířkách bariéry, ale průkazné
  (dolní mez nad nulou) je to jen u ±4,5 bp — a tam bariéra „drží" leží na úrovni doteku, takže
  měří i pouhé odmítnutí knotem. Kontrola, která odstraní vliv polohy doteku („přebytek drží nad
  nulovou pravděpodobností"), neprojde při žádné šířce.
- **Primární číslo** (±9 bp, sdruženo přes RTH a noc, Mantel–Haenszel, bootstrap po dnech):
  **+5,4 p. b. [−6,5; +15,4]**, přebytek +5,1 [−6,6; +14,5]; 217 rozhodnutých doteků s měřeným
  podílem (109 / 108).
- **Citlivost na šířku bariéry** (šířka 9 bp byla zvolena až po prvním běhu — viz níže):

  | bariéra | drží: rozdíl [95 %] | přebytek: rozdíl [95 %] |
  |---|---|---|
  | ±4,5 bp | **+10,1 [+2,1; +18,3]** | +4,3 [−2,4; +10,7] |
  | ±6,75 bp | +5,6 [−4,0; +14,2] | +2,3 [−4,9; +9,0] |
  | ±9 bp | +5,4 [−6,5; +15,4] | +5,1 [−6,6; +14,5] |
  | ±13,5 bp | +5,6 [−5,9; +14,7] | +6,3 [−5,5; +15,3] |
  | ±9 bp bez odmítnutí knotem | +9,8 [−4,4; +21,1] | +6,2 [−6,9; +17,0] |

- **Vrstvy (±9 bp):** RTH 73,7 % (73/99) × 67,2 % (45/67); noc 90,0 % (9/10) × 90,2 % (37/41) —
  noc má na rozdíl málo doteků v horní skupině.
- **Dominance zdi nelze rozhodnout:** v RTH nad mediánem dominance −2,5 p. b., pod mediánem
  +13,7 p. b. [−5,2; +31,7] — intervaly ±20 p. b.
- Popisně (mnoho srovnání, nic samostatně): call zdi +10,2 p. b. [−3,1; +23,2], put zdi
  −16,0 p. b. [−34,1; +3,9] — opačné směry; ES −1,4, NQ −0,1 p. b.
- **OI zdi** (`oiwalls`) jsou od ceny většinou daleko: 17 rozhodnutých doteků za 13 seancí
  (15 s měřeným podílem) — na verdikt nestačí.
- **Vedlejší pozorování (netestováno):** GEX zdi 0DTE v tomto vzorku „drží" (±9 bp, 30 min)
  v ~76 %, tedy ne lépe, než by vyšlo z náhodné procházky ze stejné polohy (~77 %; nulová p
  horizont 30 min nezohledňuje, skutečná nulová p rozhodnutých doteků je spíš vyšší).
- **Důsledek:** do rozhodování nic nepřidávat — podíl outright zůstává informativní popisek
  (profil dvěma tóny, popisky zdí a žebříku — #1014, #1015); žádný práh, tečkování, váha
  v detektorech, MCP (E-3.9) ani ranní snapshot (E-5.7). Varianty a rozhodnutí vlastníka v #1019.

## Oprava metody proti prvnímu běhu (8. 10.)

První běh (stažené komentáře v #1019 a #1007) měl chyby, které našel nezávislý ověřovatel:
1. **Práh „drží" ležel na úrovni doteku** (dotek i „drží" 4,5 bp od zdi): víc než polovina doteků
   „držela" už na dalším baru, podíl „drží" (~85 %) byl z velké části mechanický.
2. **Sloučené RTH a noc**, ačkoli zadání chtělo vrstvy zvlášť: skupina outright ≥ 40 % byla
   z 86 % RTH, skupina pod 40 % z 55 % — a noc „drží" víc sama od sebe.
3. Chyběla brána pokrytí `printvol` a 15 doteků bylo po settle 0DTE.

Oprava — **odchylky od parametrů zvolených před prvním během, uvedené výslovně**:
- bariéry symetricky kolem zdi, primárně **±9 bp; tuto šířku jsem zvolil až po prvním běhu**,
  proto tabulka citlivosti výše (s původními 4,5 bp by síto prošlo);
- nulová pravděpodobnost „drží" pro každý dotek z polohy close doteku mezi bariérami (náhodná
  procházka bez driftu) a „přebytek drží" jako kontrola;
- stratifikace RTH / noc jako primární analýza (Mantel–Haenszel), bootstrap po celých
  obchodních dnech (ES a NQ téhož dne jsou korelované);
- vynechán dotek mimo pokrytí `printvol` (12) a po settle 0DTE (15; setupy po settle nevznikají,
  #1324), horizont výsledku končí v settle;
- dělení mediánem (v plánu jako kontrola) vypuštěno — medián podílu je 40,0 %, dělení je
  s prahem 40 % prakticky totožné.

## Jak číst a co data neříkají

- Dotek = bar se přiblíží ke zdi na 4,5 bp ze správné strany (ES při ~7 770 ≈ 3,5 b, `wall_zone`
  šablony je 3 b; NQ při ~30 800 ≈ 14 b). wall_bounce navíc vynechává zdi s dominancí pod 0,15 —
  tady jsou všechny zdi.
- Podíl outright = tisky / objem na striku a straně zdi od začátku seance do doteku (dnešní tok,
  point-in-time), ne složení OI, ze kterého zeď vzniká — proxy z #1007.
- Pokrytí `printvol` u nové 0DTE expirace začíná v 5 z 13 seancí až 00:01 UTC místo 22:00 (roll
  pipeline o půlnoci UTC — #1331, nasazeno 7. 10.); 28. 9. až 15:07 UTC, 29. 9. končí 18:09.
  Doteky mimo pokrytí jsou vynechané, podíl v prvních hodinách je počítaný z kratšího okna.
- Track record wall_bounce, EM respect (#872) a gamma cliff (#576), které #1019 jmenuje jako další
  zdroje, nejsou události na striku zdi (EM, cliff) nebo mají malé n (wall_bounce v5 s printvol
  ~80 setupů) — srovnání by bylo slabší než doteky.
- Doteky jednoho dne nejsou nezávislé — proto bootstrap celých dnů; Newcombe ve vrstvách bere
  doteky jednotlivě (spíš optimistický) a je jen popisný.
- K rozhodnutí by bylo potřeba zhruba 3–4× víc dat (interval ±10 p. b. proti efektu ~5 p. b.).

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
| drží | 183 | 19 |
| průraz | 59 | 6 |
| nerozhodnuto | 74 | 8 |

Vynecháno: mimo pokrytí printvol 12, po settle 0DTE 15.
Podíl outright na zdi při doteku: medián 40,0 %, kvartily 22,0 % – 50,3 %, n = 217.

#### Primárně: práh 40 %, stratifikováno RTH / noc — síta NEsplněna

| vrstva | drží — outright ≥ 40 % | nulová p | drží — outright < 40 % | nulová p | rozdíl high − low [Newcombe 95 %] |
|---|---|---|---|---|---|
| RTH | 73/99 = 73,7 % [64,3 %; 81,4 %] | 76,7 % | 45/67 = 67,2 % [55,3 %; 77,2 %] | 76,9 % | +6,6 p. b. [-7,2 p. b.; +20,7 p. b.] |
| noc | 9/10 = 90,0 % [59,6 %; 98,2 %] | 78,7 % | 37/41 = 90,2 % [77,5 %; 96,1 %] | 75,9 % | -0,2 p. b. [-31,2 p. b.; +15,0 p. b.] |
| celkem (nestratifikováno) | 82/109 = 75,2 % [66,4 %; 82,4 %] | 76,9 % | 82/108 = 75,9 % [67,1 %; 83,0 %] | 76,5 % | -0,7 p. b. [-12,0 p. b.; +10,7 p. b.] |

| síto | hodnota |  |
|---|---|---|
| n ≥ 30 v obou skupinách | 109 / 108 | ✔ |
| drží: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po dnech 95 % — dolní mez > 0 | +5,4 p. b. [-6,5 p. b.; +15,4 p. b.] | ✘ |
| přebytek drží nad nulovou p: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po dnech 95 % (kontrola) | +5,1 p. b. [-6,6 p. b.; +14,5 p. b.] | ✘ |

#### Citlivost na šířku bariéry (✔ = dolní mez > 0)

| bariéra | n high / low | drží: rozdíl MH [95 %] | přebytek: rozdíl MH [95 %] |
|---|---|---|---|
| ±4,5 bp | 142 / 135 | +10,1 p. b. [+2,1 p. b.; +18,3 p. b.] ✔ | +4,3 p. b. [-2,4 p. b.; +10,7 p. b.] |
| ±6,75 bp | 135 / 125 | +5,6 p. b. [-4,0 p. b.; +14,2 p. b.] | +2,3 p. b. [-4,9 p. b.; +9,0 p. b.] |
| ±9 bp | 109 / 108 | +5,4 p. b. [-6,5 p. b.; +15,4 p. b.] | +5,1 p. b. [-6,6 p. b.; +14,5 p. b.] |
| ±13,5 bp | 63 / 81 | +5,6 p. b. [-5,9 p. b.; +14,7 p. b.] | +6,3 p. b. [-5,5 p. b.; +15,3 p. b.] |
| ±9 bp bez odmítnutí knotem | 105 / 94 | +9,8 p. b. [-4,4 p. b.; +21,1 p. b.] | +6,2 p. b. [-6,9 p. b.; +17,0 p. b.] |

#### Rozpad (popisný)

|  | drží — outright ≥ 40 % | drží — outright < 40 % | rozdíl [Newcombe 95 %] |
|---|---|---|---|
| ES | 38/51 = 74,5 % [61,1 %; 84,5 %] | 22/29 = 75,9 % [57,9 %; 87,8 %] | -1,4 p. b. [-19,3 p. b.; +19,2 p. b.] |
| NQ | 44/58 = 75,9 % [63,5 %; 85,0 %] | 60/79 = 75,9 % [65,5 %; 84,0 %] | -0,1 p. b. [-14,9 p. b.; +13,8 p. b.] |
| call zeď | 56/64 = 87,5 % [77,2 %; 93,5 %] | 51/66 = 77,3 % [65,8 %; 85,7 %] | +10,2 p. b. [-3,1 p. b.; +23,2 p. b.] |
| put zeď | 26/45 = 57,8 % [43,3 %; 71,0 %] | 31/42 = 73,8 % [58,9 %; 84,7 %] | -16,0 p. b. [-34,1 p. b.; +3,9 p. b.] |
| RTH, dominance ≥ medián 0,271 | 36/51 = 70,6 % [57,0 %; 81,3 %] | 19/26 = 73,1 % [53,9 %; 86,3 %] | -2,5 p. b. [-21,4 p. b.; +19,5 p. b.] |
| RTH, dominance < medián | 37/48 = 77,1 % [63,5 %; 86,7 %] | 26/41 = 63,4 % [48,1 %; 76,4 %] | +13,7 p. b. [-5,2 p. b.; +31,7 p. b.] |

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
| drží: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po dnech 95 % — dolní mez > 0 | -11,6 p. b. [-50,0 p. b.; +7,2 p. b.] (887 losů bez společné vrstvy zahozeno) | ✘ |
| přebytek drží nad nulovou p: rozdíl high − low sdružený přes RTH/noc (Mantel–Haenszel), bootstrap po dnech 95 % (kontrola) | -18,7 p. b. [-54,1 p. b.; +0,7 p. b.] (887 losů bez společné vrstvy zahozeno) | ✘ |

#### Citlivost na šířku bariéry (✔ = dolní mez > 0)

| bariéra | n high / low | drží: rozdíl MH [95 %] | přebytek: rozdíl MH [95 %] |
|---|---|---|---|
| ±4,5 bp | 9 / 9 | +15,0 p. b. [-42,9 p. b.; +50,0 p. b.] (203 losů bez společné vrstvy zahozeno) | -0,7 p. b. [-74,1 p. b.; +36,3 p. b.] (203 losů bez společné vrstvy zahozeno) |
| ±6,75 bp | 9 / 9 | -13,2 p. b. [-50,0 p. b.; +4,5 p. b.] (203 losů bez společné vrstvy zahozeno) | -24,7 p. b. [-64,9 p. b.; -0,9 p. b.] (203 losů bez společné vrstvy zahozeno) |
| ±9 bp | 8 / 7 | -11,6 p. b. [-50,0 p. b.; +7,2 p. b.] (887 losů bez společné vrstvy zahozeno) | -18,7 p. b. [-54,1 p. b.; +0,7 p. b.] (887 losů bez společné vrstvy zahozeno) |
| ±13,5 bp | 8 / 3 | -16,2 p. b. [-50,0 p. b.; +0,0 p. b.] (1219 losů bez společné vrstvy zahozeno) | -16,7 p. b. [-44,1 p. b.; -0,5 p. b.] (1219 losů bez společné vrstvy zahozeno) |
| ±9 bp bez odmítnutí knotem | 7 / 7 | -14,3 p. b. [-50,0 p. b.; +5,3 p. b.] (887 losů bez společné vrstvy zahozeno) | -19,4 p. b. [-54,1 p. b.; +0,8 p. b.] (887 losů bez společné vrstvy zahozeno) |

#### Rozpad (popisný)

|  | drží — outright ≥ 40 % | drží — outright < 40 % | rozdíl [Newcombe 95 %] |
|---|---|---|---|
| ES | 1/2 = 50,0 % [9,5 %; 90,5 %] | 1/1 = 100,0 % [20,7 %; 100,0 %] | -50,0 p. b. [-90,5 p. b.; +39,1 p. b.] |
| NQ | 5/6 = 83,3 % [43,6 %; 97,0 %] | 5/6 = 83,3 % [43,6 %; 97,0 %] | +0,0 p. b. [-42,0 p. b.; +42,0 p. b.] |
| call zeď | 5/6 = 83,3 % [43,6 %; 97,0 %] | 4/5 = 80,0 % [37,6 %; 96,4 %] | +3,3 p. b. [-39,6 p. b.; +47,9 p. b.] |
| put zeď | 1/2 = 50,0 % [9,5 %; 90,5 %] | 2/2 = 100,0 % [34,2 %; 100,0 %] | -50,0 p. b. [-90,5 p. b.; +27,3 p. b.] |
| RTH, dominance ≥ medián 0,529 | 4/4 = 100,0 % [51,0 %; 100,0 %] | — | — |
| RTH, dominance < medián | 1/3 = 33,3 % [6,1 %; 79,2 %] | 2/2 = 100,0 % [34,2 %; 100,0 %] | -66,7 p. b. [-93,9 p. b.; +13,5 p. b.] |
