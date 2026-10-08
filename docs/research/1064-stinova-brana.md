# Stínová brána podle polohy v pásmu — verdikt na mechanice v5 (#1064)

> E-0.5 Fáze 0 (#1387) epiky #1385 · data k 8. 10. 2026 10:13 UTC · skript
> `scripts/measure_band_gate_1064.py` (jen čtení PG, pevné semínko) · zadání a síta: #1060
> (rozhodnutí 7. 9. 2026) · vstup dávkového bumpu v6 E-1.14b (#1403).

## Verdikt

- **Předregistrovaná předpověď neplatí.** Na v5 není skupina „block" horší o ≥ 0,2 R — je naopak
  bodově lepší: `band_gate_simple` Ø R pass +0,071 × block +0,187 (rozdíl −0,116 R),
  `band_gate_regime` +0,093 × +0,143 (−0,050 R). Čisté R po nákladech ADR-0030 (základ brány v6,
  ot. 1 v #1385) má stejný směr: rozdíl simple −0,102, regime −0,017.
- **Síta:** u obou pravidel prošlo jen síto velikosti (menší skupina 194, resp. 230). Dolní mez
  rozdílu úspěšnosti (Newcombe) i rozdílu Ø R (bootstrap po seancích) je pod nulou; interakce
  poloha × gamma režim p = 0,14 (bez max_pain_pin p = 0,12). Vynechání max_pain_pin výsledek nemění.
- **Per šablona** nemá žádná šablona u žádného pravidla rozdíl ve prospěch brány s dolní mezí nad
  nulou. failed_break a wall_bounce mají „block" bodově lepší (simple −0,68 a −0,26 R, regime
  −0,59 a −0,21 R); trend_continuation je u simple ~0, u regime bodově +0,08 R ve prospěch brány
  [−0,34; +0,47]. **Per symbol** u simple ES i NQ ~0, u regime ES +0,10 [−0,27; +0,49], NQ ~0.
- **Poloha na v5 z velké části zastupuje gamma režim:** `inside` je z 87 % v pozitivní gammě
  (222 z 254), `outside` + `no_zone` z 91 % v negativní (174 z 191 se známým režimem). Fáze 1
  (#575, 985 setupů v1–v5) viděla v sdruženém vzorku inside +0,21 × outside −0,24 R, ale už tehdy
  jen v5 (278 setupů) rozdíl neukazovala; v5 s 502 setupy ukazuje bodově opak. Efekt polohy není
  stabilní napříč mechanikami a obdobími.
- **Důsledek:** žádné z pravidel se naostro nezapíná; do bumpu v6 (E-1.14b) brána polohy nejde.
  Posun confidence +10 / 0 / −15 (`BAND_CONFIDENCE_ADJUST`) stojí na fázi 1 a v5 ho nepotvrzuje.
- **Rozhodnutí vlastníka 8. 10. 2026:** posun confidence podle polohy se ruší
  (`BAND_CONFIDENCE_ADJUST` = 0) v nasazení s E-1.14b (#1403); stínové verdikty a pásmové metriky
  se zapisují dál; přeměření na v6 bez připomínky (tento skript jde spustit kdykoli).

## Jak číst a co data neříkají

- Vzorek je 22 obchodních seancí jednoho období (9. 9.–8. 10. 2026; první setup s bránou vznikl
  8. 9. 22:01 UTC, tedy v seanci 9. 9.). Intervaly rozdílu Ø R jsou široké (±0,4 R): data
  nevylučují malý kladný ani záporný efekt. „Brána nepotvrzena" neznamená „opak prokázán" — ani
  lepší výsledek skupiny block není významný.
- Výhra = R > 0 (stejně jako dlaždice obrazovky Setupy). Wilsonova mez v rozhodnutí 7. 9. je
  interval podílu, proto se počítá na rozdílu úspěšností (Newcombe, metoda 10); rozdíl Ø R nese
  bootstrap, který losuje celé seance (setupy jednoho dne nejsou nezávislé).
- Newcombe i permutace interakce berou setupy jednotlivě, tedy jako nezávislé; při korelaci uvnitř
  seance jsou jejich meze a p spíš optimistické. Verdikt to nemění — neprošlo ani tak.
- Interakce: Freedman–Lane permutace reziduí aditivního modelu R ~ poloha + režim, statistika =
  součet čtverců interakce; poloha ve třídách pravidla (outside = outside ∪ no_zone), 3 setupy bez
  známého režimu vynechány.
- Vzorek: všech 840 uzavřených setupů v5 (841. byl při běhu aktivní), 88 vyřazuje predikát
  statistik (po settle #1324, značka #1346), 250 bránu nenese (vznik před nasazením 8. 9. nebo
  nezměřená hloubka, `band_context` bez profilu).

## Výstup skriptu

### Vzorek


Mechanika v5, vznik 2026-09-08 22:01 – 2026-10-08 10:13 UTC = 22 obchodních seancí 2026-09-09 – 2026-10-08.


| krok | setupů |
|---|---|
| v5 | 841 |
| uzavřené | 840 |
| ve statistikách | 752 |
| s bránou | 502 |


### Skupiny pass × block


#### `band_gate_simple`

| skupina | n | úspěšnost | Ø R | Ø čisté R | Σ R |
|---|---|---|---|---|---|
| pass | 308 | 29,5 % | +0,071 | -0,032 | +21,962 |
| block | 194 | 36,6 % | +0,187 | +0,070 | +36,350 |

#### `band_gate_regime`

| skupina | n | úspěšnost | Ø R | Ø čisté R | Σ R |
|---|---|---|---|---|---|
| pass | 272 | 29,8 % | +0,093 | -0,001 | +25,408 |
| block | 230 | 35,2 % | +0,143 | +0,016 | +32,904 |


### Poloha × gamma režim


| poloha | režim | n | úspěšnost | Ø R | Ø čisté R | Σ R |
|---|---|---|---|---|---|---|
| inside | positive | 222 | 28,4 % | +0,031 | -0,070 | +6,825 |
| inside | negative | 32 | 28,1 % | +0,093 | +0,042 | +2,974 |
| transition | positive | 36 | 27,8 % | -0,096 | -0,272 | -3,445 |
| transition | negative | 18 | 50,0 % | +0,867 | +0,780 | +15,608 |
| outside | positive | 9 | 44,4 % | +0,432 | +0,301 | +3,890 |
| outside | negative | 97 | 29,9 % | +0,140 | -0,038 | +13,583 |
| no_zone | positive | 8 | 62,5 % | +0,421 | +0,346 | +3,371 |
| no_zone | negative | 77 | 42,9 % | +0,208 | +0,159 | +16,017 |
| no_zone | neznámý | 3 | 0,0 % | -0,170 | -0,180 | -0,511 |


### Síta — všechny šablony


#### `band_gate_simple` — síta NEsplněna

| síto | hodnota |  |
|---|---|---|
| n v menší skupině ≥ 100 | 194 | ✔ |
| úspěšnost pass − block, Newcombe 95 % — dolní mez > 0 | -7,1 % [-15,5 %; 1,3 %] | ✘ |
| Ø R pass − block, bootstrap po seancích 95 % — dolní mez > 0 | -0,116 [-0,515; +0,257] | ✘ |
| předpověď: block horší o ≥ 0,2 R | -0,116 | ✘ |

#### `band_gate_regime` — síta NEsplněna

| síto | hodnota |  |
|---|---|---|
| n v menší skupině ≥ 100 | 230 | ✔ |
| úspěšnost pass − block, Newcombe 95 % — dolní mez > 0 | -5,4 % [-13,6 %; 2,7 %] | ✘ |
| Ø R pass − block, bootstrap po seancích 95 % — dolní mez > 0 | -0,050 [-0,412; +0,274] | ✘ |
| předpověď: block horší o ≥ 0,2 R | -0,050 | ✘ |
| interakce poloha × režim, permutace (n = 499) — p < 0,05 | SS 10,69, p = 0,1384 | ✘ |


### Síta — bez max_pain_pin


#### `band_gate_simple` — síta NEsplněna

| síto | hodnota |  |
|---|---|---|
| n v menší skupině ≥ 100 | 168 | ✔ |
| úspěšnost pass − block, Newcombe 95 % — dolní mez > 0 | -7,5 % [-16,4 %; 1,3 %] | ✘ |
| Ø R pass − block, bootstrap po seancích 95 % — dolní mez > 0 | -0,141 [-0,577; +0,251] | ✘ |
| předpověď: block horší o ≥ 0,2 R | -0,141 | ✘ |

#### `band_gate_regime` — síta NEsplněna

| síto | hodnota |  |
|---|---|---|
| n v menší skupině ≥ 100 | 199 | ✔ |
| úspěšnost pass − block, Newcombe 95 % — dolní mez > 0 | -4,6 % [-13,1 %; 3,9 %] | ✘ |
| Ø R pass − block, bootstrap po seancích 95 % — dolní mez > 0 | -0,057 [-0,464; +0,292] | ✘ |
| předpověď: block horší o ≥ 0,2 R | -0,057 | ✘ |
| interakce poloha × režim, permutace (n = 465) — p < 0,05 | SS 12,05, p = 0,1195 | ✘ |


### Per šablona


#### `band_gate_simple`

|  | pass: n / úspěšnost / Ø R / Ø čisté R / Σ R | block: n / úspěšnost / Ø R / Ø čisté R / Σ R | Ø R pass − block [95 %] |
|---|---|---|---|
| failed_break | 30 / 23,3 % / -0,309 / -0,350 / -9,284 | 20 / 50,0 % / +0,371 / +0,332 / +7,426 | -0,681 [-1,741; +0,265] |
| gamma_momentum | 10 / 30,0 % / +0,298 / +0,230 / +2,977 | 1 / 100,0 % / +3,000 / +2,944 / +3,000 | — (n < 10) |
| max_pain_pin | 10 / 50,0 % / +0,022 / +0,008 / +0,218 | 26 / 38,5 % / +0,017 / -0,012 / +0,438 | +0,005 [-0,504; +0,469] |
| trend_continuation | 223 / 30,0 % / +0,131 / +0,002 / +29,308 | 98 / 29,6 % / +0,148 / -0,047 / +14,517 | -0,017 [-0,501; +0,447] |
| wall_bounce | 35 / 25,7 % / -0,036 / -0,064 / -1,257 | 49 / 42,9 % / +0,224 / +0,180 / +10,969 | -0,260 [-0,879; +0,471] |

#### `band_gate_regime`

|  | pass: n / úspěšnost / Ø R / Ø čisté R / Σ R | block: n / úspěšnost / Ø R / Ø čisté R / Σ R | Ø R pass − block [95 %] |
|---|---|---|---|
| failed_break | 29 / 24,1 % / -0,286 / -0,327 / -8,284 | 21 / 47,6 % / +0,306 / +0,267 / +6,426 | -0,592 [-1,651; +0,313] |
| gamma_momentum | 6 / 33,3 % / +0,333 / +0,280 / +2,000 | 5 / 40,0 % / +0,795 / +0,714 / +3,977 | — (n < 10) |
| max_pain_pin | 5 / 40,0 % / -0,229 / -0,245 / -1,143 | 31 / 41,9 % / +0,058 / +0,032 / +1,800 | — (n < 10) |
| trend_continuation | 198 / 30,8 % / +0,167 / +0,051 / +33,092 | 123 / 28,5 % / +0,087 / -0,116 / +10,732 | +0,080 [-0,344; +0,468] |
| wall_bounce | 34 / 26,5 % / -0,008 / -0,037 / -0,257 | 50 / 42,0 % / +0,199 / +0,157 / +9,969 | -0,207 [-0,811; +0,515] |


### Per symbol


#### `band_gate_simple`

|  | pass: n / úspěšnost / Ø R / Ø čisté R / Σ R | block: n / úspěšnost / Ø R / Ø čisté R / Σ R | Ø R pass − block [95 %] |
|---|---|---|---|
| ES | 112 / 33,9 % / +0,228 / +0,025 / +25,490 | 134 / 38,1 % / +0,264 / +0,108 / +35,405 | -0,037 [-0,425; +0,337] |
| NQ | 196 / 27,0 % / -0,018 / -0,065 / -3,528 | 60 / 33,3 % / +0,016 / -0,017 / +0,945 | -0,034 [-0,830; +0,695] |

#### `band_gate_regime`

|  | pass: n / úspěšnost / Ø R / Ø čisté R / Σ R | block: n / úspěšnost / Ø R / Ø čisté R / Σ R | Ø R pass − block [95 %] |
|---|---|---|---|
| ES | 84 / 35,7 % / +0,314 / +0,116 / +26,366 | 162 / 36,4 % / +0,213 / +0,047 / +34,529 | +0,101 [-0,274; +0,492] |
| NQ | 188 / 27,1 % / -0,005 / -0,053 / -0,958 | 68 / 32,4 % / -0,024 / -0,058 / -1,625 | +0,019 [-0,738; +0,687] |
