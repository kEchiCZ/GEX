# Stínová brána podle polohy v pásmu — verdikt na mechanice v5 (#1064)

> E-0.5 Fáze 0 (#1387) epiky #1385 · data k 8. 10. 2026 10:13 UTC · skript
> `scripts/measure_band_gate_1064.py` (jen čtení PG, pevné semínko) · zadání a síta: #1060
> (rozhodnutí 7. 9. 2026) · vstup dávkového bumpu v6 E-1.14b (#1403).

## Verdikt

- **Předregistrovaná předpověď neplatí.** Na v5 není skupina „block" horší o ≥ 0,2 R — je naopak
  bodově lepší: `band_gate_simple` Ø R pass +0,071 × block +0,187 (rozdíl −0,116 R),
  `band_gate_regime` +0,093 × +0,143 (−0,050 R). Čisté R po nákladech ADR-0030 má stejný směr
  (simple −0,032 × +0,070).
- **Síta:** u obou pravidel prošlo jen síto velikosti (menší skupina 194, resp. 230). Dolní mez
  rozdílu úspěšnosti (Newcombe) i rozdílu Ø R (bootstrap po seancích) je pod nulou; interakce
  poloha × gamma režim p = 0,14 (bez max_pain_pin p = 0,12). Vynechání max_pain_pin výsledek nemění.
- **Per šablona** žádná šablona nemá ve prospěch brány dolní mez nad nulou; failed_break a
  wall_bounce mají „block" bodově lepší (−0,68 R, resp. −0,26 R), trend_continuation rozdíl ~0.
  Per symbol totéž (ES i NQ rozdíl ~0).
- **Poloha na v5 z velké části zastupuje gamma režim:** `inside` je z 87 % v pozitivní gammě
  (222 z 254), `outside` + `no_zone` z 91 % v negativní (174 z 191 se známým režimem). Fáze 1 (#575, 985 setupů
  v1–v4) viděla inside +0,21 × outside −0,24 R; v5 ukazuje opak. Efekt polohy není stabilní napříč
  mechanikami a obdobími.
- **Důsledek:** žádné z pravidel se naostro nezapíná; do bumpu v6 (E-1.14b) brána polohy nejde.
  Posun confidence +10 / 0 / −15 (`BAND_CONFIDENCE_ADJUST`) stojí na fázi 1 a v5 ho nepotvrzuje —
  o něm rozhoduje vlastník (varianty v #1064).

## Jak číst a co data neříkají

- Vzorek je 22 obchodních seancí jednoho období (8. 9.–8. 10. 2026). Intervaly rozdílu Ø R jsou
  široké (±0,4 R): data nevylučují malý kladný ani záporný efekt. „Brána nepotvrzena" neznamená
  „opak prokázán" — ani lepší výsledek skupiny block není významný.
- Výhra = R > 0 (stejně jako dlaždice obrazovky Setupy). Wilsonova mez v rozhodnutí 7. 9. je
  interval podílu, proto se počítá na rozdílu úspěšností (Newcombe, metoda 10); rozdíl Ø R nese
  bootstrap, který losuje celé seance (setupy jednoho dne nejsou nezávislé).
- Interakce: Freedman–Lane permutace reziduí aditivního modelu R ~ poloha + režim, statistika =
  součet čtverců interakce; poloha ve třídách pravidla (outside = outside ∪ no_zone), 3 setupy bez
  známého režimu vynechány.
- Vzorek: všech 840 setupů v5 je uzavřených, 88 vyřazuje predikát statistik (po settle #1324,
  značka #1346), 250 vzniklo před nasazením brány 8. 9. a bránu nenese.

## Výstup skriptu

### Vzorek


Mechanika v5, vznik 2026-09-08 22:01 – 2026-10-08 10:13 UTC, 22 obchodních seancí.


| krok | setupů |
|---|---|
| v5 | 840 |
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

|  | n pass | Ø R pass | Ø čisté R pass | n block | Ø R block | Ø čisté R block | Ø R pass − block [95 %] |
|---|---|---|---|---|---|---|---|
| failed_break | 30 | -0,309 | -0,350 | 20 | +0,371 | +0,332 | -0,681 [-1,741; +0,265] |
| gamma_momentum | 10 | +0,298 | +0,230 | 1 | +3,000 | +2,944 | — (n < 10) |
| max_pain_pin | 10 | +0,022 | +0,008 | 26 | +0,017 | -0,012 | +0,005 [-0,504; +0,469] |
| trend_continuation | 223 | +0,131 | +0,002 | 98 | +0,148 | -0,047 | -0,017 [-0,501; +0,447] |
| wall_bounce | 35 | -0,036 | -0,064 | 49 | +0,224 | +0,180 | -0,260 [-0,879; +0,471] |

#### `band_gate_regime`

|  | n pass | Ø R pass | Ø čisté R pass | n block | Ø R block | Ø čisté R block | Ø R pass − block [95 %] |
|---|---|---|---|---|---|---|---|
| failed_break | 29 | -0,286 | -0,327 | 21 | +0,306 | +0,267 | -0,592 [-1,651; +0,313] |
| gamma_momentum | 6 | +0,333 | +0,280 | 5 | +0,795 | +0,714 | — (n < 10) |
| max_pain_pin | 5 | -0,229 | -0,245 | 31 | +0,058 | +0,032 | — (n < 10) |
| trend_continuation | 198 | +0,167 | +0,051 | 123 | +0,087 | -0,116 | +0,080 [-0,344; +0,468] |
| wall_bounce | 34 | -0,008 | -0,037 | 50 | +0,199 | +0,157 | -0,207 [-0,811; +0,515] |


### Per symbol


#### `band_gate_simple`

|  | n pass | Ø R pass | Ø čisté R pass | n block | Ø R block | Ø čisté R block | Ø R pass − block [95 %] |
|---|---|---|---|---|---|---|---|
| ES | 112 | +0,228 | +0,025 | 134 | +0,264 | +0,108 | -0,037 [-0,425; +0,337] |
| NQ | 196 | -0,018 | -0,065 | 60 | +0,016 | -0,017 | -0,034 [-0,830; +0,695] |

#### `band_gate_regime`

|  | n pass | Ø R pass | Ø čisté R pass | n block | Ø R block | Ø čisté R block | Ø R pass − block [95 %] |
|---|---|---|---|---|---|---|---|
| ES | 84 | +0,314 | +0,116 | 162 | +0,213 | +0,047 | +0,101 [-0,274; +0,492] |
| NQ | 188 | -0,005 | -0,053 | 68 | -0,024 | -0,058 | +0,019 [-0,738; +0,687] |
