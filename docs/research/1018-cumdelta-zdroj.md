# CumΔ midpoint × dxFeed tisky — verdikt zdroje (#1018)

> E-0.6 Fáze 0 (#1387) epiky #1385 · #615 krok 6 · seance 21. 9.–7. 10. 2026 (13 obchodních
> seancí na symbol, po opravě front kontraktu #1189) · skript `scripts/compare_cumdelta_sources.py`
> (čte jen partice `data/derived`) · vstup dávkového bumpu v6 E-1.14b (#1403) a brány nástrojů toku
> E-3.9 (#1439) · **doplněk 16. 10.: hybrid režimu `dxfeed` (E-1.14c #1459)** na konci dokumentu,
> rozhodnutí v ADR-0057.

## Verdikt

- **Živá midpoint CumΔ jde soustavně proti ceně.** Korelace přírůstků s pohybem ceny futures je
  záporná ve všech 13 seancích u obou symbolů v oknech 1 a 5 min (ES medián −0,16 / −0,24,
  NQ −0,21 / −0,36); na 15 min −0,33 / −0,46 (ES jedna seance ≈ 0). Záporná je i proti CVD
  futures (agresor od burzy).
- **Tisková řada dxFeed (strana agresora od burzy) jde s cenou:** kladně ve 13/13 seancích na 1 min
  (ES +0,10, NQ +0,11), na 5 min ES +0,27 a NQ +0,30 (12/13), a kladně s CVD futures (13/13).
  Kotva metody — CVD futures × cena — je kladná ve všech seancích (mediány přes okna ES +0,34
  až +0,50, NQ +0,52 až +0,67; per seance 0,13–0,76).
- **Obě řady spolu prakticky nesouvisí:** medián r přírůstků ES −0,04, NQ −0,08; opačné znaménko
  v 53 % (ES) a 63 % (NQ) minut; znaménko na close souhlasí jen v 7/13 (ES) a 5/13 (NQ) seancí;
  žádný posun −3…+3 min to nevysvětlí.
- **Příčina (mechanismus, `runtime.py:471` → `cumdelta.add_bar`, vzorec SPEC 4.5):** midpoint test
  porovnává `snapshot.last` — poslední obchod kontraktu, starší než snímek o libovolnou mezeru
  (rotační sweep, u čistě strukturovaného přírůstku i víc) — s **aktuálním** bid/ask. Když futures
  rostou, mid callu vyjede nad starý last → „prodej" (−1 × kladná delta); mid putu klesne pod
  last → „nákup" (+1 × záporná delta). Oba případy dávají záporný delta tok právě při růstu ceny.
  Odhad neměří agresora, ale obráceně pohyb ceny od posledního obchodu. Znaménková konvence delty
  je v pořádku (tisková řada i CVD vycházejí kladně), posun minut to není (5 a 15 min jsou
  zápornější než 1 min). Nález z 8. 9. („záporná hladinová korelace NQ 5/5 je nález o datech")
  tím má vysvětlení.
- **Pokrytí:** tisky tvoří z **celého** přírůstku objemu ES medián 40,5 % (34–57 %), NQ 38,3 %
  (18–53 %); z klasifikovaného objemu (bez strukturovaného) ES 52 %, NQ 43 %. Strukturovaný objem
  (legy spreadů, bloky — mimo tok, ADR-0032 bod 4) ES 21 %, NQ 10 %. Objem kontrakt-minut bez
  tisku (dnešní fallback režimu `dxfeed`) tvoří v RTH ES 34 % (17–42 %), NQ 45 % (25–72 %)
  klasifikovaného objemu. Rozsah tiskové řady je 0,2–0,4× midpointové (nese jen outright agresi).
- **Důsledek pro obchodování:** obráceně dnes čte vše, co stojí na midpoint znaménku — setupy
  (potvrzení protitoku, divergence „nákupy do slabosti / prodeje do síly", kvantilová brána),
  sklon CumΔ a divergence v tendenci (#394) a přes ni verdikt dne, plocha CumΔ v panelu, a přes
  čistý objem i FA odhad OI (ADR-0011), kalibrace α ze sekundárního řetězu (#1182) a potvrzení
  zpráv `cum_delta_slope` v news-engine. Do přepnutí je spolehlivé čtení toku linka **CVD
  futures** v panelu CumΔ. *(Oprava 16. 10. podle inventury #1459: podíl toku u gamma momentum
  je delta-vážený objem bez znaménka a alert `cum_delta_jump` byl odstraněn v #949 — ani jeden
  midpoint znaménko nečte; přibyla divergence tendence. Úplná inventura je v ADR-0057.)*
- **Kalibrace (#394 / #434):** prahy setupů jsou směrová a kvantilová srovnání (`setups.py:714,
  839, 1108-1113, 1206`), na měřítko řady necitlivá — po přepnutí se mění jejich obsah (směr),
  ne škála. Přeměřit se musí váha `cum_delta_slope` a divergence tendence (#394) a všechny
  vzorky v6.

## Rozhodnutí vlastníka 8. 10. 2026

- **1A — CumΔ jen z tisků od burzy, midpoint nikde:** objem bez tisku jde mimo tok, výpadek tasty
  větve = viditelná mezera, ne odhad. Revize R2, SPEC 3.4 a 4.5 a ADR-0032 bod 1, 3 a Důsledky
  → ADR-0057 (E-1.14c #1459), implementace E-1.14d #1460.
- **2A — nasazení s dávkovým bumpem v6** (E-1.14b #1403, ~29. 10.–1. 11.); do té doby číst tok
  z linky CVD futures.
- E-1.14c před ADR změří hybrid (tisky + midpoint fallback) z uložených dat (`printvol`, `netflow`,
  delta ze `snapshots`) a projde všechny spotřebitele midpoint znaménka (čistý objem, α, FA OI,
  news-engine) — kde rozhodnutí 1A neurčuje, co dostanou, přijdou varianty.
- Zamítnuto: **opravit midpoint** (kotace v čase obchodu, tick test) — snímek kotaci z doby
  obchodu nedá a strukturovaný přírůstek obchod nemá; tick test by byl tentýž artefakt s opačným
  znaménkem (klasifikuje pohyb ceny, ne agresora).

## Jak číst a co data neříkají

- Test platnosti nemá „pravdu" o opčním agresorovi; stojí na tom, že opční delta tok se správně
  určenou stranou jde s cenou a s CVD futures týmž směrem (dealeři hedgují, informovaný tok tlačí
  cenu). Korelace je **současná** (v témž okně), ne předpověď budoucí ceny — o edge setupů neříká
  nic, jen o tom, kterým směrem řada čte.
- Okna 5 a 15 min rozmývají posun značek minut mezi stínem a cyklem enginu (1 min ho nese).
- Seance 21. 9.–7. 10. bez víkendů a svátků (dřívější výstup nástroje počítal i ploché víkendové
  partice jako seance — opraveno v tomto PR); 8. 10. běžela a leží mimo `--to`. Partice před #1189
  (14.–18. 9.) sledovaly dobíhající kontrakt a do souhrnu nepatří.
- Hybridní režim `dxfeed` (tisky + midpoint fallback) se jako řada neukládá; skládá se
  z `printvol`, `netflow` a delty snímků — změřeno v doplňku 16. 10. (E-1.14c #1459): hybrid je
  horší než čisté tisky, u NQ jde proti ceně.

## Výstup skriptu

### Per seance

| Seance | Sym | Min. spol. (jen dx / jen live) | max \|Δ\| | max \|Δ\| % rozsahu | tvar. odch. | rozsah dx/live | r hladiny | r přírůstky | r přír. RTH | r přír. mimo RTH | max \|Δ\| RTH / mimo | opačné zn. | close dx / live | zn. close | tisky (bez strany / bez kontextu) | tisky z klasif. objemu | tisky z celého objemu | strukt. objem | fallback RTH (objem) | zahozeno bez Δ | řetěz dx/live | r přír. nejl. lag | poznámka |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-21 | ES | 1418 (16 / 5) | 725,408 | 114.8 % | 99.4 % | 0.197 | -0.661 | -0.042 | -0.018 | -0.201 | 725,408 / 586,657 | 99.5 % | 12,411 / -561,798 | NE | 50200 (48 / 0) | 42.4 % | 33.7 % | 20.5 % | 42.2 % | 0 | 0 / 1 | -0.007 (k=-2) | přerušený řetěz dx 0× / live 1× |
| 2026-09-22 | ES | 1408 (27 / 4) | 99,099 | 61.0 % | 63.4 % | 0.526 | 0.372 | -0.045 | -0.035 | -0.123 | 99,099 / 53,397 | 44.3 % | 37,363 / 46,038 | ano | 38802 (4 / 0) | 69.4 % | 57.1 % | 17.8 % | 24.5 % | 0 | 0 / 0 | 0.161 (k=+1) |  |
| 2026-09-23 | ES | 1378 (50 / 9) | 839,379 | 95.6 % | 91.8 % | 0.120 | -0.351 | -0.059 | -0.058 | -0.185 | 819,562 / 839,379 | 36.4 % | 43,949 / -795,127 | NE | 46870 (28 / 0) | 50.5 % | 39.9 % | 21.0 % | 34.0 % | 0 | 0 / 0 | 0.031 (k=-1) |  |
| 2026-09-24 | ES | 1364 (60 / 8) | 102,090 | 46.4 % | 55.1 % | 0.380 | 0.832 | 0.196 | 0.255 | -0.017 | 102,090 / 96,573 | 59.9 % | -68,183 / -150,085 | ano | 47211 (5 / 0) | 65.3 % | 52.4 % | 19.7 % | 31.3 % | 0 | 0 / 0 | 0.196 (k=+0) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-09-25 | ES | 1417 (15 / 5) | 257,466 | 79.9 % | 65.5 % | 0.704 | 0.547 | -0.095 | -0.298 | 0.232 | 257,466 / 172,371 | 23.3 % | -50,175 / -213,054 | ano | 52211 (11 / 0) | 53.7 % | 40.5 % | 24.5 % | 34.2 % | 0 | 0 / 0 | 0.046 (k=-2) |  |
| 2026-09-28 | ES | 1420 (14 / 6) | 67,856 | 78.1 % | 96.1 % | 0.665 | -0.394 | 0.022 | 0.032 | -0.002 | 57,317 / 67,856 | 61.4 % | -30,399 / 37,457 | NE | 50387 (17 / 0) | 65.3 % | 56.4 % | 13.6 % | 29.8 % | 0 | 0 / 0 | 0.022 (k=+0) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-09-29 | ES | 1379 (49 / 9) | 228,086 | 79.8 % | 63.3 % | 0.218 | 0.411 | -0.042 | -0.034 | -0.050 | 117,441 / 228,086 | 67.4 % | 21,352 / 248,193 | ano | 41832 (16 / 0) | 42.4 % | 36.1 % | 14.8 % | 29.1 % | 0 | 0 / 0 | 0.063 (k=+1) |  |
| 2026-09-30 | ES | 1403 (29 / 7) | 305,328 | 116.7 % | 100.0 % | 0.478 | -0.256 | -0.137 | 0.127 | -0.452 | 242,553 / 305,328 | 28.6 % | -86,695 / 218,633 | NE | 49955 (3 / 0) | 53.0 % | 40.5 % | 23.5 % | 33.7 % | 0 | 0 / 0 | 0.121 (k=-3) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-10-01 | ES | 1396 (29 / 11) | 210,267 | 65.8 % | 73.5 % | 0.228 | 0.204 | -0.226 | -0.200 | -0.302 | 143,038 / 210,267 | 42.7 % | 30,569 / 18,854 | ano | 52767 (1 / 0) | 48.3 % | 37.7 % | 21.9 % | 35.9 % | 0 | 0 / 1 | 0.064 (k=+3) | přerušený řetěz dx 0× / live 1× |
| 2026-10-02 | ES | 1409 (24 / 6) | 911,809 | 96.3 % | 79.2 % | 0.129 | 0.206 | -0.026 | -0.003 | -0.332 | 911,809 / 878,794 | 64.2 % | -12,412 / -877,755 | ano | 57871 (6 / 0) | 66.3 % | 48.0 % | 27.6 % | 17.2 % | 0 | 0 / 0 | 0.024 (k=+2) |  |
| 2026-10-05 | ES | 1411 (20 / 9) | 220,158 | 70.7 % | 93.0 % | 0.481 | -0.812 | 0.106 | 0.129 | -0.034 | 220,158 / 195,603 | 88.7 % | 57,800 / -45,711 | NE | 48945 (26 / 0) | 47.5 % | 38.8 % | 18.4 % | 35.6 % | 0 | 0 / 0 | 0.106 (k=+0) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-10-06 | ES | 1395 (31 / 10) | 473,813 | 108.5 % | 85.5 % | 0.199 | -0.732 | -0.044 | -0.061 | -0.021 | 349,132 / 473,813 | 38.0 % | 42,320 / -431,494 | NE | 43493 (14 / 0) | 44.3 % | 35.0 % | 21.0 % | 35.0 % | 0 | 0 / 0 | 0.103 (k=+2) |  |
| 2026-10-07 | ES | 1399 (29 / 7) | 185,493 | 52.1 % | 70.7 % | 0.464 | -0.049 | 0.013 | 0.029 | 0.005 | 185,493 / 125,658 | 52.8 % | 117,836 / 243,495 | ano | 48173 (10 / 0) | 51.7 % | 41.1 % | 20.6 % | 32.0 % | 0 | 0 / 0 | 0.045 (k=-2) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-09-21 | NQ | 1417 (14 / 5) | 226,935 | 102.6 % | 99.7 % | 0.033 | -0.927 | -0.041 | -0.034 | -0.064 | 226,935 / 226,267 | 95.2 % | 5,709 / -214,133 | NE | 6991 (0 / 0) | 19.2 % | 18.0 % | 6.5 % | 71.8 % | 0 | 0 / 0 | -0.010 (k=-3) |  |
| 2026-09-22 | NQ | 1408 (27 / 4) | 73,342 | 116.6 % | 99.5 % | 0.309 | -0.873 | -0.004 | 0.117 | -0.282 | 73,342 / 72,204 | 62.7 % | 19,038 / -45,581 | NE | 8309 (0 / 0) | 44.0 % | 38.7 % | 11.9 % | 45.1 % | 0 | 0 / 0 | 0.053 (k=-3) |  |
| 2026-09-23 | NQ | 1375 (47 / 10) | 45,403 | 85.9 % | 66.2 % | 0.228 | 0.402 | -0.110 | -0.164 | 0.008 | 45,403 / 24,413 | 6.9 % | 3,568 / 20,895 | ano | 11616 (0 / 0) | 39.7 % | 35.8 % | 9.7 % | 46.7 % | 0 | 0 / 0 | 0.005 (k=-3) |  |
| 2026-09-24 | NQ | 1365 (59 / 8) | 51,405 | 99.8 % | 87.3 % | 0.194 | 0.351 | -0.107 | -0.131 | 0.006 | 48,356 / 51,405 | 52.6 % | 2,275 / 35,333 | ano | 14431 (0 / 0) | 43.9 % | 39.0 % | 11.1 % | 45.0 % | 0 | 0 / 0 | 0.021 (k=+2) |  |
| 2026-09-25 | NQ | 1417 (15 / 5) | 81,985 | 110.6 % | 97.8 % | 0.167 | -0.748 | -0.128 | -0.142 | -0.084 | 81,985 / 71,740 | 88.2 % | 11,757 / -49,730 | NE | 14352 (0 / 0) | 39.3 % | 35.3 % | 10.3 % | 51.4 % | 0 | 0 / 0 | 0.013 (k=+3) |  |
| 2026-09-28 | NQ | 1420 (14 / 6) | 18,113 | 67.9 % | 81.6 % | 0.442 | -0.236 | -0.066 | -0.075 | 0.013 | 18,113 / 11,353 | 16.5 % | -4,085 / 7,268 | NE | 16098 (0 / 0) | 57.4 % | 53.0 % | 7.6 % | 42.1 % | 0 | 0 / 0 | 0.033 (k=+2) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-09-29 | NQ | 1377 (46 / 10) | 41,304 | 68.3 % | 96.3 % | 0.171 | -0.218 | -0.247 | -0.382 | 0.004 | 31,579 / 41,304 | 76.6 % | -7,224 / 19,853 | NE | 14117 (0 / 0) | 46.3 % | 41.6 % | 10.2 % | 34.3 % | 0 | 0 / 0 | 0.014 (k=-3) |  |
| 2026-09-30 | NQ | 1403 (29 / 7) | 34,598 | 69.1 % | 88.8 % | 0.251 | -0.746 | -0.204 | -0.199 | -0.209 | 23,897 / 34,598 | 85.7 % | 6,897 / -10,397 | NE | 15470 (0 / 0) | 42.0 % | 38.0 % | 9.5 % | 44.9 % | 0 | 0 / 0 | 0.030 (k=-3) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-10-01 | NQ | 1396 (29 / 11) | 101,731 | 99.6 % | 89.0 % | 0.065 | -0.238 | -0.011 | -0.014 | 0.005 | 101,731 / 100,348 | 65.0 % | 1,806 / -97,615 | NE | 14348 (1 / 0) | 42.6 % | 38.3 % | 10.0 % | 38.2 % | 0 | 0 / 0 | 0.535 (k=+1) |  |
| 2026-10-02 | NQ | 1409 (24 / 6) | 82,897 | 93.1 % | 72.3 % | 0.140 | 0.200 | -0.065 | -0.083 | 0.045 | 82,897 / 81,256 | 61.8 % | -2,336 / -81,379 | ano | 18081 (0 / 0) | 53.1 % | 46.6 % | 12.3 % | 25.2 % | 0 | 0 / 0 | 0.040 (k=-1) |  |
| 2026-10-05 | NQ | 1411 (20 / 9) | 67,822 | 82.2 % | 77.2 % | 0.164 | 0.310 | -0.145 | -0.206 | -0.049 | 61,596 / 67,822 | 64.3 % | -7,920 / -10,294 | ano | 15641 (0 / 0) | 40.3 % | 36.1 % | 10.5 % | 46.9 % | 0 | 0 / 0 | 0.175 (k=-3) | řady nezačínají v nule (start uprostřed seance / navázání) |
| 2026-10-06 | NQ | 1394 (32 / 10) | 92,938 | 99.2 % | 97.3 % | 0.071 | -0.372 | -0.026 | -0.028 | -0.028 | 92,938 / 84,676 | 35.8 % | -1,759 / -86,435 | ano | 13385 (0 / 0) | 39.2 % | 35.2 % | 10.4 % | 51.8 % | 0 | 0 / 0 | 0.050 (k=+2) |  |
| 2026-10-07 | NQ | 1398 (30 / 7) | 41,554 | 108.2 % | 89.0 % | 0.252 | -0.493 | -0.084 | -0.076 | -0.146 | 41,554 / 36,243 | 55.1 % | -1,180 / 8,204 | NE | 13543 (0 / 0) | 51.5 % | 46.4 % | 9.9 % | 39.4 % | 0 | 0 / 0 | -0.014 (k=+2) |  |

### Souhrn (jen použitelné seance, mediány)

| Sym | Seancí v souhrnu | zn. close shoda | med. max \|Δ\| % rozsahu | med. tvar. odch. | med. rozsah dx/live | med. r hladiny | med. r přírůstky | med. r přír. RTH | med. r přír. mimo RTH | med. opačné zn. | med. r přír. nejl. lag | nejl. lagy |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ES | 13 | 7/13 | 79.8 % | 79.2 % | 0.380 | -0.049 | -0.042 | -0.018 | -0.050 | 52.8 % | 0.063 | -2, +1, -1, +0, -2, +0, +1, -3, +3, +2, +0, +2, -2 |
| NQ | 13 | 5/13 | 99.2 % | 89.0 % | 0.171 | -0.238 | -0.084 | -0.083 | -0.028 | 62.7 % | 0.030 | -3, -3, -3, +2, +3, +2, -3, -3, +1, -1, -3, +2, +2 |

### Shoda s cenou futures — souhrn (medián r, + seancí s r > 0)

| Sym | okno | dx ~ cena | midpoint ~ cena | CVD fut. ~ cena | dx ~ CVD | midpoint ~ CVD |
|---|---|---|---|---|---|---|
| ES | 1 min | 0.104 (+ 13/13) | -0.159 (+ 0/13) | 0.343 (+ 13/13) | 0.136 (+ 13/13) | -0.084 (+ 0/13) |
| ES | 5 min | 0.268 (+ 12/13) | -0.236 (+ 0/13) | 0.498 (+ 13/13) | 0.154 (+ 13/13) | -0.139 (+ 1/13) |
| ES | 15 min | 0.163 (+ 10/13) | -0.326 (+ 1/13) | 0.457 (+ 13/13) | 0.189 (+ 12/13) | -0.245 (+ 2/13) |
| NQ | 1 min | 0.111 (+ 13/13) | -0.205 (+ 0/13) | 0.518 (+ 13/13) | 0.137 (+ 13/13) | -0.173 (+ 0/13) |
| NQ | 5 min | 0.299 (+ 12/13) | -0.362 (+ 0/13) | 0.672 (+ 13/13) | 0.284 (+ 13/13) | -0.298 (+ 0/13) |
| NQ | 15 min | 0.419 (+ 12/13) | -0.460 (+ 0/13) | 0.644 (+ 13/13) | 0.335 (+ 13/13) | -0.277 (+ 0/13) |

### Shoda s cenou futures — per seance

| Seance | Sym | okno | dx ~ cena | midpoint ~ cena | CVD fut. ~ cena | dx ~ CVD | midpoint ~ CVD |
|---|---|---|---|---|---|---|---|
| 2026-09-21 | ES | 1 min | 0.104 | -0.235 | 0.343 | 0.142 | -0.125 |
| 2026-09-21 | ES | 5 min | 0.164 | -0.349 | 0.452 | 0.221 | -0.282 |
| 2026-09-21 | ES | 15 min | 0.422 | -0.331 | 0.457 | 0.516 | -0.413 |
| 2026-09-22 | ES | 1 min | 0.157 | -0.276 | 0.296 | 0.221 | -0.127 |
| 2026-09-22 | ES | 5 min | 0.269 | -0.087 | 0.498 | 0.307 | -0.056 |
| 2026-09-22 | ES | 15 min | 0.163 | 0.000 | 0.453 | 0.258 | -0.011 |
| 2026-09-23 | ES | 1 min | 0.101 | -0.159 | 0.384 | 0.088 | -0.020 |
| 2026-09-23 | ES | 5 min | -0.075 | -0.236 | 0.559 | 0.051 | 0.149 |
| 2026-09-23 | ES | 15 min | -0.162 | -0.169 | 0.497 | 0.218 | 0.124 |
| 2026-09-24 | ES | 1 min | 0.125 | -0.145 | 0.378 | 0.191 | -0.081 |
| 2026-09-24 | ES | 5 min | 0.030 | -0.436 | 0.580 | 0.066 | -0.474 |
| 2026-09-24 | ES | 15 min | -0.096 | -0.463 | 0.487 | 0.036 | -0.455 |
| 2026-09-25 | ES | 1 min | 0.084 | -0.258 | 0.494 | 0.191 | -0.231 |
| 2026-09-25 | ES | 5 min | 0.276 | -0.375 | 0.705 | 0.282 | -0.318 |
| 2026-09-25 | ES | 15 min | 0.150 | -0.449 | 0.628 | 0.248 | -0.375 |
| 2026-09-28 | ES | 1 min | 0.140 | -0.112 | 0.449 | 0.114 | -0.084 |
| 2026-09-28 | ES | 5 min | 0.276 | -0.002 | 0.598 | 0.289 | -0.139 |
| 2026-09-28 | ES | 15 min | 0.432 | -0.024 | 0.618 | 0.411 | 0.005 |
| 2026-09-29 | ES | 1 min | 0.037 | -0.195 | 0.388 | 0.109 | -0.171 |
| 2026-09-29 | ES | 5 min | 0.120 | -0.185 | 0.447 | 0.074 | -0.203 |
| 2026-09-29 | ES | 15 min | -0.053 | -0.395 | 0.381 | 0.102 | -0.291 |
| 2026-09-30 | ES | 1 min | 0.082 | -0.177 | 0.190 | 0.236 | -0.309 |
| 2026-09-30 | ES | 5 min | 0.369 | -0.292 | 0.379 | 0.309 | -0.349 |
| 2026-09-30 | ES | 15 min | 0.391 | -0.326 | 0.246 | 0.243 | -0.196 |
| 2026-10-01 | ES | 1 min | 0.124 | -0.221 | 0.407 | 0.124 | -0.208 |
| 2026-10-01 | ES | 5 min | 0.345 | -0.464 | 0.441 | 0.154 | -0.414 |
| 2026-10-01 | ES | 15 min | 0.200 | -0.375 | 0.241 | -0.053 | -0.514 |
| 2026-10-02 | ES | 1 min | 0.139 | -0.090 | 0.261 | 0.136 | -0.058 |
| 2026-10-02 | ES | 5 min | 0.268 | -0.223 | 0.404 | 0.086 | -0.135 |
| 2026-10-02 | ES | 15 min | 0.402 | -0.262 | 0.396 | 0.189 | -0.245 |
| 2026-10-05 | ES | 1 min | 0.065 | -0.025 | 0.131 | 0.088 | -0.030 |
| 2026-10-05 | ES | 5 min | 0.071 | -0.247 | 0.425 | 0.070 | -0.134 |
| 2026-10-05 | ES | 15 min | 0.083 | -0.507 | 0.405 | 0.068 | -0.265 |
| 2026-10-06 | ES | 1 min | 0.027 | -0.032 | 0.325 | 0.028 | -0.045 |
| 2026-10-06 | ES | 5 min | 0.087 | -0.129 | 0.631 | 0.070 | -0.056 |
| 2026-10-06 | ES | 15 min | 0.137 | -0.125 | 0.755 | 0.051 | -0.068 |
| 2026-10-07 | ES | 1 min | 0.172 | -0.056 | 0.244 | 0.162 | -0.050 |
| 2026-10-07 | ES | 5 min | 0.437 | -0.175 | 0.516 | 0.261 | -0.113 |
| 2026-10-07 | ES | 15 min | 0.405 | -0.154 | 0.516 | 0.082 | -0.012 |
| 2026-09-21 | NQ | 1 min | 0.096 | -0.248 | 0.532 | 0.137 | -0.185 |
| 2026-09-21 | NQ | 5 min | 0.265 | -0.362 | 0.640 | 0.302 | -0.226 |
| 2026-09-21 | NQ | 15 min | 0.315 | -0.455 | 0.701 | 0.440 | -0.275 |
| 2026-09-22 | NQ | 1 min | 0.110 | -0.080 | 0.438 | 0.212 | -0.046 |
| 2026-09-22 | NQ | 5 min | 0.331 | -0.087 | 0.773 | 0.315 | -0.102 |
| 2026-09-22 | NQ | 15 min | 0.417 | -0.207 | 0.835 | 0.388 | -0.147 |
| 2026-09-23 | NQ | 1 min | 0.203 | -0.267 | 0.569 | 0.194 | -0.385 |
| 2026-09-23 | NQ | 5 min | 0.409 | -0.494 | 0.750 | 0.388 | -0.551 |
| 2026-09-23 | NQ | 15 min | 0.541 | -0.747 | 0.773 | 0.374 | -0.678 |
| 2026-09-24 | NQ | 1 min | 0.193 | -0.221 | 0.488 | 0.227 | -0.257 |
| 2026-09-24 | NQ | 5 min | 0.446 | -0.399 | 0.606 | 0.256 | -0.362 |
| 2026-09-24 | NQ | 15 min | 0.520 | -0.559 | 0.579 | 0.324 | -0.330 |
| 2026-09-25 | NQ | 1 min | 0.135 | -0.287 | 0.547 | 0.158 | -0.173 |
| 2026-09-25 | NQ | 5 min | 0.299 | -0.442 | 0.733 | 0.295 | -0.338 |
| 2026-09-25 | NQ | 15 min | 0.476 | -0.460 | 0.751 | 0.436 | -0.380 |
| 2026-09-28 | NQ | 1 min | 0.086 | -0.205 | 0.575 | 0.086 | -0.264 |
| 2026-09-28 | NQ | 5 min | 0.273 | -0.454 | 0.749 | 0.209 | -0.450 |
| 2026-09-28 | NQ | 15 min | 0.545 | -0.464 | 0.749 | 0.413 | -0.329 |
| 2026-09-29 | NQ | 1 min | 0.078 | -0.144 | 0.577 | 0.125 | -0.107 |
| 2026-09-29 | NQ | 5 min | 0.345 | -0.422 | 0.673 | 0.312 | -0.321 |
| 2026-09-29 | NQ | 15 min | 0.525 | -0.520 | 0.572 | 0.375 | -0.285 |
| 2026-09-30 | NQ | 1 min | 0.198 | -0.252 | 0.570 | 0.205 | -0.302 |
| 2026-09-30 | NQ | 5 min | 0.350 | -0.312 | 0.614 | 0.266 | -0.220 |
| 2026-09-30 | NQ | 15 min | 0.339 | -0.505 | 0.639 | 0.291 | -0.277 |
| 2026-10-01 | NQ | 1 min | 0.061 | -0.105 | 0.518 | 0.062 | -0.110 |
| 2026-10-01 | NQ | 5 min | 0.134 | -0.236 | 0.672 | 0.118 | -0.272 |
| 2026-10-01 | NQ | 15 min | 0.282 | -0.250 | 0.644 | 0.162 | -0.214 |
| 2026-10-02 | NQ | 1 min | 0.122 | -0.112 | 0.488 | 0.100 | -0.094 |
| 2026-10-02 | NQ | 5 min | 0.275 | -0.224 | 0.504 | 0.127 | -0.190 |
| 2026-10-02 | NQ | 15 min | 0.495 | -0.315 | 0.431 | 0.126 | -0.145 |
| 2026-10-05 | NQ | 1 min | 0.060 | -0.206 | 0.352 | 0.151 | -0.252 |
| 2026-10-05 | NQ | 5 min | -0.120 | -0.397 | 0.641 | 0.065 | -0.340 |
| 2026-10-05 | NQ | 15 min | -0.221 | -0.436 | 0.700 | 0.089 | -0.364 |
| 2026-10-06 | NQ | 1 min | 0.111 | -0.087 | 0.372 | 0.057 | -0.086 |
| 2026-10-06 | NQ | 5 min | 0.258 | -0.293 | 0.519 | 0.306 | -0.298 |
| 2026-10-06 | NQ | 15 min | 0.374 | -0.407 | 0.572 | 0.335 | -0.190 |
| 2026-10-07 | NQ | 1 min | 0.122 | -0.151 | 0.452 | 0.100 | -0.123 |
| 2026-10-07 | NQ | 5 min | 0.351 | -0.347 | 0.711 | 0.284 | -0.244 |
| 2026-10-07 | NQ | 15 min | 0.419 | -0.567 | 0.579 | 0.213 | -0.183 |

## Doplněk 16. 10. 2026 — hybrid režimu `dxfeed` (E-1.14c #1459)

> Měření, které ověřovatel #1018 požadoval před ADR: řada režimu `dxfeed`, jak je napsaný dnes
> (`compute/cumdelta.add_bar`) — tisky se stranou od burzy + midpoint test pro kontrakt-minuty bez
> jediného tisku. Okno verdiktu 21. 9.–7. 10. (13 seancí na symbol) a rozšíření do 15. 10. (18).
> `scripts/compare_cumdelta_sources.py --hybrid` (čte `derived/` a `snapshots/`, nic nezapisuje).

### Verdikt hybridu

- **Hybrid je horší než čisté tisky ve všech oknech u obou symbolů** a u NQ jde proti ceně:
  medián r s cenou futures NQ −0,04 / −0,16 / −0,34 (1 / 5 / 15 min; kladně 5 / 0 / 1 z 13 seancí),
  ES +0,04 / −0,06 / −0,08 (kladně 10 / 6 / 5 z 13). Čisté tisky ve stejných seancích: ES +0,10 /
  +0,27 / +0,16, NQ +0,11 / +0,30 / +0,42. S CVD futures hybrid ES ≈ 0 (+0,04 / +0,01 / +0,04),
  NQ záporně (−0,05 / −0,10 / −0,16).
- **Samotný fallback jde proti ceně v každé seanci** (kladně 0 z 12–13 ve všech oknech): ES −0,09 /
  −0,28 / −0,40, NQ −0,13 / −0,34 / −0,45 — stejně nebo zápornější než celá midpoint řada. Kontrakt
  bez tisku v minutě má `last` z ještě staršího obchodu, artefakt #1018 je u něj nejsilnější.
- **Midpoint v hybridu převáží:** kontrakt-minuty bez tisku nesou medián 46 % (ES) a 63 % (NQ)
  přírůstku objemu a 49 % / 68 % absolutního minutového toku hybridu; rozsah kumulativu hybridu je
  1,7× (ES) a 4,8× (NQ) rozsahu tiskové řady. Znaménko hybridu na close se s tisky shoduje jen
  v 9 / 13 (ES) a 7 / 13 (NQ) seancích.
- **Rozšíření o 8.–15. 10.** (18 seancí) pořadí nemění: tisky ES +0,09 / +0,21 / +0,16, NQ +0,11 /
  +0,30 / +0,45; hybrid ES +0,02 / −0,06 / −0,12, NQ −0,04 / −0,19 / −0,36; fallback kladně 0 z
  17–18. ES 14. 10. jdou na 5 a 15 min proti ceně i tisky (−0,31 / −0,35; hybrid −0,30 / −0,58).
- **Pořadí A > B z #1018 platí čísly:** CumΔ jen z tisků je jediná varianta, která jde s cenou
  i s CVD futures. Rozhodnutí 1A stojí, k vlastníkovi se nevrací.

### Metoda

- Hybrid(t) = stín `cumdelta_dx` `flow_ring`(t) (tisky se stranou, aktivní řetěz) + Σ midpoint toku
  kontrakt-minut bez tisku v minutě t. Minuta bez stínu (tasty neběželo) nese jen fallback —
  v režimu `dxfeed` tehdy midpoint klasifikuje celý přírůstek.
- Kontrakt-minuta bez tisku = řádek `derived/{sym}/{exp}/printvol` s `printed` 0, nebo NULL
  (trade větev neběžela). `printvol` píše jen aktivní řetěz a jen minuty s přírůstkem objemu.
- Midpoint tok kontrakt-minuty = přírůstek `netflow` (v režimu `midpoint` = znaménko midpoint testu
  × přírůstek objemu) × delta ze `snapshots` × multiplikátor (`ticker.FUTURES_MULTIPLIERS`).
  Partici `netflow` jedné expirace plní v seanci sekundární řetěz do settle a aktivní řetěz po něm
  s pokračujícím kumulativem — přírůstky se počítají přes celou seanci, započtou se jen v okně
  aktivního řetězu (první až poslední minuta `printvol` té expirace).
- **Kontrola metody:** táž skladba přes všechny kontrakt-minuty je rekonstrukce živé midpoint řady.
  Proti uložené `flow_delta` sedí přesně (relativní chyba 0) ve 34 z 36 seancí; ES 21. 9. a 1. 10.
  (restart enginu, přerušený řetěz živé řady) 2e-5 a 9e-5. Seance s chybou nad 0,1 % by do souhrnu
  hybridu nešla.
- Mimo: midpoint pro tisky bez strany. `printvol.printed` je zahrnuje, takže kontrakt-minuta jen
  s tisky bez strany se nepočítá jako fallback, přestože jí režim `dxfeed` dá midpoint celému
  přírůstku; u kontraktu s tisky se stranou chybí midpoint jejich objemu. Počtem jde o ~0,05 %
  tisků (sloupec „tisky (bez strany …)" výše), **objemem** ES o 1–15 % objemu tisků za den
  (medián ~6 %; téměř vše bloky ≥ 100 lotů daleko OTM), NQ < 1 % (ověření nálezů #1459,
  partice `flow` 21. 9.–16. 10.). Změřený hybrid tak nese méně midpointu než skutečný režim
  `dxfeed` a vychází lépe, ne hůř — závěr platí tím spíš.
- Shoda s cenou a CVD stejně jako v #1018: přírůstky sečtené v oknech 1 / 5 / 15 min zarovnaných
  na hodinu, Pearson, medián přes seance a počet seancí s r > 0.

### Výstup skriptu — hybrid

#### Shoda s cenou futures, okno verdiktu 21. 9.–7. 10. (medián r, + seancí s r > 0)

| Sym | okno | dx ~ cena | midpoint ~ cena | CVD fut. ~ cena | dx ~ CVD | midpoint ~ CVD | hybrid ~ cena | hybrid ~ CVD | jen fallback ~ cena |
|---|---|---|---|---|---|---|---|---|---|
| ES | 1 min | 0.104 (+ 13/13) | -0.159 (+ 0/13) | 0.343 (+ 13/13) | 0.136 (+ 13/13) | -0.084 (+ 0/13) | 0.037 (+ 10/13) | 0.044 (+ 11/13) | -0.092 (+ 0/13) |
| ES | 5 min | 0.268 (+ 12/13) | -0.236 (+ 0/13) | 0.498 (+ 13/13) | 0.154 (+ 13/13) | -0.139 (+ 1/13) | -0.060 (+ 6/13) | 0.011 (+ 8/13) | -0.278 (+ 0/13) |
| ES | 15 min | 0.163 (+ 10/13) | -0.326 (+ 1/13) | 0.457 (+ 13/13) | 0.189 (+ 12/13) | -0.245 (+ 2/13) | -0.081 (+ 5/13) | 0.038 (+ 10/13) | -0.398 (+ 0/12) |
| NQ | 1 min | 0.111 (+ 13/13) | -0.205 (+ 0/13) | 0.518 (+ 13/13) | 0.137 (+ 13/13) | -0.173 (+ 0/13) | -0.041 (+ 5/13) | -0.052 (+ 3/13) | -0.133 (+ 0/13) |
| NQ | 5 min | 0.299 (+ 12/13) | -0.362 (+ 0/13) | 0.672 (+ 13/13) | 0.284 (+ 13/13) | -0.298 (+ 0/13) | -0.156 (+ 0/13) | -0.097 (+ 1/13) | -0.344 (+ 0/13) |
| NQ | 15 min | 0.419 (+ 12/13) | -0.460 (+ 0/13) | 0.644 (+ 13/13) | 0.335 (+ 13/13) | -0.277 (+ 0/13) | -0.343 (+ 1/13) | -0.157 (+ 1/13) | -0.446 (+ 0/12) |

#### Shoda s cenou futures, 21. 9.–15. 10.

| Sym | okno | dx ~ cena | midpoint ~ cena | CVD fut. ~ cena | dx ~ CVD | midpoint ~ CVD | hybrid ~ cena | hybrid ~ CVD | jen fallback ~ cena |
|---|---|---|---|---|---|---|---|---|---|
| ES | 1 min | 0.093 (+ 18/18) | -0.144 (+ 0/18) | 0.349 (+ 18/18) | 0.122 (+ 17/18) | -0.081 (+ 0/18) | 0.020 (+ 12/18) | 0.038 (+ 14/18) | -0.066 (+ 0/18) |
| ES | 5 min | 0.211 (+ 16/18) | -0.263 (+ 0/18) | 0.475 (+ 18/18) | 0.120 (+ 17/18) | -0.165 (+ 1/18) | -0.061 (+ 7/18) | -0.000 (+ 9/18) | -0.268 (+ 0/18) |
| ES | 15 min | 0.157 (+ 14/18) | -0.323 (+ 1/18) | 0.473 (+ 18/18) | 0.105 (+ 14/18) | -0.196 (+ 3/18) | -0.116 (+ 6/18) | 0.013 (+ 11/18) | -0.353 (+ 0/17) |
| NQ | 1 min | 0.111 (+ 18/18) | -0.188 (+ 0/18) | 0.519 (+ 18/18) | 0.140 (+ 18/18) | -0.173 (+ 0/18) | -0.044 (+ 5/18) | -0.048 (+ 4/18) | -0.137 (+ 0/18) |
| NQ | 5 min | 0.304 (+ 17/18) | -0.371 (+ 0/18) | 0.640 (+ 18/18) | 0.261 (+ 18/18) | -0.309 (+ 0/18) | -0.194 (+ 0/18) | -0.138 (+ 1/18) | -0.342 (+ 0/18) |
| NQ | 15 min | 0.451 (+ 17/18) | -0.513 (+ 0/18) | 0.642 (+ 18/18) | 0.355 (+ 17/18) | -0.281 (+ 0/18) | -0.363 (+ 1/18) | -0.159 (+ 1/18) | -0.495 (+ 0/17) |

#### Hybrid — souhrn (21. 9.–7. 10. / 21. 9.–15. 10.)

| Sym | seancí | max chyba rekonstrukce | med. fallback z objemu | med. fallback z \|toku\| | med. r hybrid ~ dx | med. r hybrid ~ midpoint | med. rozsah hybrid/dx | zn. close hybrid = dx |
|---|---|---|---|---|---|---|---|---|
| ES | 13 | 9.4e-05 | 46.3 % | 49.3 % | 0.754 | 0.267 | 1.73 | 9/13 |
| NQ | 13 | 0.0e+00 | 63.0 % | 67.9 % | 0.502 | 0.562 | 4.79 | 7/13 |

| Sym | seancí | max chyba rekonstrukce | med. fallback z objemu | med. fallback z \|toku\| | med. r hybrid ~ dx | med. r hybrid ~ midpoint | med. rozsah hybrid/dx | zn. close hybrid = dx |
|---|---|---|---|---|---|---|---|---|
| ES | 18 | 9.4e-05 | 46.9 % | 50.9 % | 0.577 | 0.324 | 2.27 | 12/18 |
| NQ | 18 | 0.0e+00 | 63.3 % | 68.4 % | 0.471 | 0.605 | 4.89 | 10/18 |

#### Hybrid — per seance (21. 9.–15. 10.)

| Seance | Sym | rekonstrukce r / chyba | fallback z objemu | fallback z \|toku\| | r přír. hybrid ~ dx | r přír. hybrid ~ midpoint | rozsah hybrid/dx | close hybrid / dx | zn. close | bez delty | poznámka |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-21 | ES | 1.0000 / 2.1e-05 | 53.8 % | 50.7 % | 0.743 | 0.228 | 3.66 | -424,610 / 12,411 | NE | 0 |  |
| 2026-09-22 | ES | 1.0000 / 0.0e+00 | 25.4 % | 25.7 % | 0.882 | 0.035 | 0.83 | 15,840 / 37,363 | ano | 0 |  |
| 2026-09-23 | ES | 1.0000 / 0.0e+00 | 45.6 % | 51.1 % | 0.129 | 0.962 | 3.02 | -112,786 / 43,949 | NE | 0 |  |
| 2026-09-24 | ES | 1.0000 / 0.0e+00 | 31.1 % | 33.0 % | 0.902 | 0.343 | 1.73 | -116,231 / -68,183 | ano | 0 |  |
| 2026-09-25 | ES | 1.0000 / 0.0e+00 | 41.3 % | 32.4 % | 0.933 | 0.018 | 1.42 | -166,787 / -50,175 | ano | 0 |  |
| 2026-09-28 | ES | 1.0000 / 0.0e+00 | 46.3 % | 17.9 % | 0.938 | 0.127 | 0.98 | 22,758 / -30,399 | NE | 0 |  |
| 2026-09-29 | ES | 1.0000 / 0.0e+00 | 58.8 % | 51.1 % | 0.411 | 0.683 | 4.09 | 251,191 / 21,352 | ano | 0 |  |
| 2026-09-30 | ES | 1.0000 / 0.0e+00 | 45.2 % | 49.3 % | 0.754 | 0.123 | 1.18 | -55,350 / -86,695 | ano | 0 |  |
| 2026-10-01 | ES | 1.0000 / 9.4e-05 | 48.6 % | 59.5 % | 0.369 | 0.306 | 3.37 | 82,794 / 30,569 | ano | 0 |  |
| 2026-10-02 | ES | 1.0000 / 0.0e+00 | 27.4 % | 32.8 % | 0.925 | 0.005 | 0.85 | -22,314 / -12,412 | ano | 0 |  |
| 2026-10-05 | ES | 1.0000 / 0.0e+00 | 49.7 % | 44.4 % | 0.865 | 0.267 | 0.95 | 38,369 / 57,800 | ano | 0 |  |
| 2026-10-06 | ES | 1.0000 / 0.0e+00 | 50.0 % | 51.6 % | 0.317 | 0.679 | 4.38 | -377,831 / 42,320 | NE | 0 |  |
| 2026-10-07 | ES | 1.0000 / 0.0e+00 | 47.4 % | 51.8 % | 0.248 | 0.751 | 2.62 | 418,389 / 117,836 | ano | 0 |  |
| 2026-10-08 | ES | 1.0000 / 0.0e+00 | 41.2 % | 43.4 % | 0.783 | 0.083 | 1.71 | 139,524 / -10,359 | NE | 0 |  |
| 2026-10-09 | ES | 1.0000 / 0.0e+00 | 59.5 % | 57.7 % | 0.703 | 0.381 | 2.67 | -176,565 / -50,181 | ano | 0 |  |
| 2026-10-12 | ES | 1.0000 / 0.0e+00 | 68.7 % | 61.9 % | 0.180 | 0.930 | 3.78 | 19,948 / 72,914 | ano | 0 |  |
| 2026-10-13 | ES | 1.0000 / 0.0e+00 | 54.4 % | 54.9 % | 0.259 | 0.710 | 1.92 | 33,807 / 3,552 | ano | 0 |  |
| 2026-10-14 | ES | 1.0000 / 0.0e+00 | 42.1 % | 56.4 % | 0.353 | 0.648 | 3.54 | 163,837 / 84,697 | ano | 0 |  |
| 2026-10-15 | ES | 1.0000 / 0.0e+00 | 58.2 % | 69.1 % | 0.143 | 0.891 | 8.44 | 117,117 / -16,612 | NE | 0 |  |
| 2026-09-21 | NQ | 1.0000 / 0.0e+00 | 83.6 % | 85.8 % | 0.260 | 0.883 | 29.37 | -204,634 / 5,709 | NE | 0 |  |
| 2026-09-22 | NQ | 1.0000 / 0.0e+00 | 61.6 % | 64.0 % | 0.595 | 0.527 | 2.35 | -27,722 / 19,038 | NE | 0 |  |
| 2026-09-23 | NQ | 1.0000 / 0.0e+00 | 62.7 % | 68.6 % | 0.498 | 0.562 | 4.75 | 33,878 / 3,568 | ano | 0 |  |
| 2026-09-24 | NQ | 1.0000 / 0.0e+00 | 60.8 % | 67.3 % | 0.554 | 0.588 | 4.79 | 25,792 / 2,275 | ano | 0 |  |
| 2026-09-25 | NQ | 1.0000 / 0.0e+00 | 68.4 % | 71.3 % | 0.478 | 0.641 | 4.99 | -38,969 / 11,757 | NE | 0 |  |
| 2026-09-28 | NQ | 1.0000 / 0.0e+00 | 67.0 % | 44.8 % | 0.677 | 0.572 | 2.26 | 12,163 / -4,085 | NE | 0 |  |
| 2026-09-29 | NQ | 1.0000 / 0.0e+00 | 63.0 % | 67.9 % | 0.521 | 0.456 | 5.94 | -22,165 / -7,224 | ano | 0 |  |
| 2026-09-30 | NQ | 1.0000 / 0.0e+00 | 65.6 % | 69.4 % | 0.364 | 0.708 | 3.21 | 7,429 / 6,897 | ano | 0 |  |
| 2026-10-01 | NQ | 1.0000 / 0.0e+00 | 63.0 % | 72.0 % | 0.502 | 0.330 | 11.64 | -67,044 / 1,806 | NE | 0 |  |
| 2026-10-02 | NQ | 1.0000 / 0.0e+00 | 48.1 % | 61.3 % | 0.601 | 0.183 | 3.99 | -42,465 / -2,336 | ano | 0 |  |
| 2026-10-05 | NQ | 1.0000 / 0.0e+00 | 68.3 % | 71.3 % | 0.426 | 0.639 | 5.73 | -6,811 / -7,920 | ano | 0 |  |
| 2026-10-06 | NQ | 1.0000 / 0.0e+00 | 66.8 % | 67.2 % | 0.392 | 0.559 | 9.93 | -57,582 / -1,759 | ano | 0 |  |
| 2026-10-07 | NQ | 1.0000 / 0.0e+00 | 54.2 % | 59.9 % | 0.530 | 0.521 | 3.79 | 9,234 / -1,180 | NE | 0 |  |
| 2026-10-08 | NQ | 1.0000 / 0.0e+00 | 58.9 % | 66.4 % | 0.298 | 0.623 | 2.37 | 31,445 / 7,582 | ano | 0 |  |
| 2026-10-09 | NQ | 1.0000 / 0.0e+00 | 71.7 % | 74.4 % | 0.420 | 0.664 | 10.98 | -54,023 / 54 | NE | 0 |  |
| 2026-10-12 | NQ | 1.0000 / 0.0e+00 | 71.3 % | 72.6 % | 0.314 | 0.821 | 11.41 | 87,212 / -6,062 | NE | 0 |  |
| 2026-10-13 | NQ | 1.0000 / 0.0e+00 | 63.0 % | 68.1 % | 0.427 | 0.630 | 3.37 | 4,995 / -335 | NE | 0 |  |
| 2026-10-14 | NQ | 1.0000 / 0.0e+00 | 63.5 % | 70.1 % | 0.465 | 0.678 | 5.87 | 86,900 / 13,373 | ano | 0 |  |
| 2026-10-15 | NQ | 1.0000 / 0.0e+00 | 74.0 % | 77.7 % | 0.231 | 0.885 | 6.92 | 12,764 / 5,564 | ano | 0 |  |
