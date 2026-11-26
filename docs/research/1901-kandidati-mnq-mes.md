# Kandidáti intradenního setupu pro MNQ a MES se známkou A/B/C (#1901)

> Zadání vlastníka 26. 11. 2026: z měřených dat navrhnout setup dlouhodobě ziskový na MNQ, pak
> na MES; setupy známkovat A/B/C (A obchodovat s plnou velikostí, i když je vzácnější).
> Data: RTH minutové bary NQ a ES 29. 7. 2024 – 25. 11. 2026 (581 seancí bez zkrácených).
> Skript: `scripts/measure_intraday_candidates_1901.py` (předregistrace; od 27. 11. 2026 se
> nemění). Náklady 1 mikra v konvenci v6: MNQ 1,11 b, MES 0,744 b za round-trip.

## Verdikt

- **MNQ: dva kandidáti, ani jeden prokázaný.** S1 momentum z pásma šumu (+0,19 R, t = 1,74)
  a S2 ORB 5 min (+0,17 R, t = 1,33) jsou na NQ kladné ve všech pěti pololetích. Práh pro
  6 testů (Bonferroni, t ≥ 2,4) nesplnil žádný.
- **Známka A zatím neprokázaná.** První známkování (úzký předchozí den + objem otevření)
  selhalo ve všech šesti případech. Druhé (vysoká volatilita + makro zpráva High USD) dává
  pořadí A > B > C jen u NQ S1 (A +0,34 / B +0,14 / C +0,09 R); jedno správné pořadí ze šesti
  je úroveň náhody (pravděpodobnost správného pořadí tří skupin je 1/6).
- **MES: žádný kandidát.** ES S1 −0,03 R, S2 +0,06 R (t = 0,46), S3 −0,27 R (t = −3,3, záporná
  ve všech pololetích). Jediný podnět je reverze do closu v pozitivní gammě (H2, +2,7 b, t = 1,9,
  n = 41).
- **Neobchodovat, měřit dopředu** (#1901, kap. Dopředné měření). Důkaz branou aplikace při
  edge +0,2 R potřebuje ~1 050 obchodů S1, tedy ≈ 4 roky.

## Postup

Každý běh měl pravidla, parametry a směr předpovědi zapsané před spuštěním; parametry jsou
z pramenů nebo konvence a neladily se. Prameny končí vzorkem nejpozději v roce 2024, takže naše
data jsou pro jejich pravidla mimo vzorek. Pořadí:

1. **H1** momentum do closu (Gao a kol. 2018, Baltussen a kol. 2021): vstup 15:30 ET ve směru
   pohybu od předchozího settle, výstup 16:00 ET.
2. **H2** reverze do closu podle gamma režimu (`setups.gex_regime`): vznikla z výsledku H1,
   proto test mechanismu, ne jen znaménka.
3. **H3** momentum z pásma šumu bez úprav pravidel; **H4** jeho obchodovatelnost (zpoždění,
   náklady ×2, tvrdý stop).
4. **S1–S3** se známkami G1 a G2 a simulací účtu 5 000 $ (ADR-0038). Pravidla jsou v hlavičce
   skriptu.

## Výsledky

H1 a H2 (body na 1 mikro po nákladech):

| Test | NQ | ES |
|---|---|---|
| H1 momentum do closu | −5,8 b, t = −2,3 (n = 545) | −2,0 b, t = −3,8 (n = 551) |
| H2 reverze, pozitivní gamma | +8,6 b, t = 0,9 (n = 36) | +2,7 b, t = 1,9 (n = 41) |
| H2 reverze, negativní gamma | −1,3 b, t = −0,1 (n = 43) | +1,4 b, t = 0,6 (n = 41) |

H3 a H4 na NQ (denní P/L na 1 MNQ):

| Varianta | Ø b/den | t | Sharpe | Σ $ | max. DD $ | nejhorší den $ | 5 nejlepších dnů |
|---|---|---|---|---|---|---|---|
| H3 bez tvrdého stopu | +9,6 | 1,66 | 1,12 | +10 612 | −3 982 | −867 | 90 % zisku |
| zpoždění 1 min | +7,6 | 1,36 | 0,92 | +8 434 | −4 780 | −862 | 106 % |
| náklady ×2 | +8,6 | 1,48 | 1,00 | +9 471 | −4 386 | −874 | 100 % |
| tvrdý stop 25 b (1 %) | +4,9 | 1,18 | 0,79 | +5 454 | −3 556 | −522 | 120 % |
| tvrdý stop 50 b (2 %) = S1 | +10,2 | 1,92 | 1,29 | +11 316 | −2 224 | −920 | 80 % |

S1–S3 v čistém R na obchod:

| Strategie | NQ → MNQ | ES → MES |
|---|---|---|
| S1 momentum z pásma šumu, tvrdý stop 50 / 20 b | **+0,187**, t = 1,74, n = 592, 5/5 pololetí kladných | −0,032, t = −0,60, n = 554 |
| S2 ORB 5 min, stop 10 % ATR(14) (medián 37,9 / 7,2 b) | **+0,165**, t = 1,33, n = 536, 5/5 kladných | +0,059, t = 0,46, n = 534 |
| S3 fade high/low předchozí seance, cíl 2 R | −0,060, t = −0,67, n = 245 | −0,270, t = −3,28, n = 278 |

NQ S1 má jednostrannou 95% dolní mez +0,010 R, takže by v Knihovně jako jediná buňka prošel
bránou šablon (`risk.template_gate`). Testů bylo ale šest; při šesti nezávislých pokusech projde
aspoň jeden na hladině 5 % náhodou s pravděpodobností ~26 %, proto se to za důkaz nepočítá.

## Známky A/B/C

Předpověď byla vždy A > B > C. **G1** (trend: A = předchozí den NR4/NR7 a RVOL5 ≥ 1; fade obráceně):

| | NQ S1 | NQ S2 | NQ S3 | ES S1 | ES S2 | ES S3 |
|---|---|---|---|---|---|---|
| A | −0,03 (63) | +0,31 (53) | −0,09 (82) | −0,30 (51) | −0,19 (49) | −0,30 (99) |
| B | +0,20 (302) | +0,15 (293) | −0,07 (134) | +0,06 (285) | +0,25 (285) | −0,27 (148) |
| C | +0,28 (188) | +0,20 (161) | +0,12 (18) | −0,08 (184) | −0,10 (170) | −0,17 (22) |

**G2** (A = ATR(14)/close předchozí seance nad 60denním mediánem a v den je zpráva High USD
s časem před 16:00 ET; vzniklo po selhání G1):

| | NQ S1 | NQ S2 | NQ S3 | ES S1 | ES S2 | ES S3 |
|---|---|---|---|---|---|---|
| A | **+0,34** (155) | −0,12 (132) | −0,12 (60) | −0,10 (149) | +0,03 (131) | −0,31 (81) |
| B | **+0,14** (313) | +0,31 (243) | −0,04 (99) | +0,05 (265) | +0,04 (239) | −0,38 (121) |
| C | **+0,09** (86) | +0,28 (124) | +0,05 (63) | −0,18 (104) | +0,21 (124) | +0,06 (52) |

Čtení: G1 nikde nesedí. G2 sedí jen u NQ S1 a rozdíl A − C (+0,25 R) tam má t ≈ 0,7; směr odpovídá
pramenům (momentum silnější ve volatilitě), ale velikost je v šumu. Známka A pro S1 zůstává
hypotéza k dopřednému ověření, ne pravidlo pro velikost pozice.

## Velikost pozice na účtu 5 000 $

Kontrakty = ⌊riziko / (R × $ za bod mikra)⌋; obchod s 0 kontrakty se nevezme.

| | riziko | NQ S1 | NQ S2 | ES S2 |
|---|---|---|---|---|
| vše 1 % | 50 $ | 0 obchodů (1 MNQ × 50 b = 100 $) | 37 obch., +727 $, DD −522 $ | 441 obch., +2 001 $, DD −1 907 $ |
| vše 2 % | 100 $ | 592 obch., +11 059 $, DD −1 916 $ (38 %) | 431 obch., +6 687 $, DD −2 153 $ | 524 obch., +5 128 $, DD −4 254 $ |
| A/B/C podle G2 | 100 / 50 / 0 $ | 155 obch. (jen A), +5 203 $, DD −1 090 $ (22 %) | 108 obch., +1 007 $, DD −1 448 $ | 326 obch., +2 073 $, DD −2 286 $ |

- Na MNQ se stopem 38–50 b nejde známku B (1 %) koupit ani jedním kontraktem, takže A/B/C
  se zúží na „jen A za 2 %". To ADR-0038 dovoluje až po ≥ 50 živých obchodech s edge ≥ +0,2 R.
- U NQ S1 má „jen A" proti „vše 2 %" skoro stejný poměr zisku k drawdownu (4,8 proti 5,8):
  filtr zatím snižuje četnost, ne riziko na dolar zisku.
- MES (stop ~7 b) škálovat jde, ale není co: žádná strategie na ES není kladná s rozumným t.

## Gamma režim (od 20. 7. 2026, jen popisně)

Malé vzorky a nekonzistentní: NQ S1 negativní +0,40 R (n = 64) × pozitivní +0,21 R (n = 42);
NQ S2 +0,17 (48) × +0,28 (35); ES S1 +0,16 (67) × −0,02 (25). Na závěr to nestačí.

## Dopředné měření (#1901) — předregistrace

- **Běh:** tentýž skript s `--from 2026-11-27`. Bary a levels v `data/derived/` se nikdy nemažou
  (ADR-0029), kalendář zůstává v `news_events`, takže noční job není potřeba.
- **Kontrola kolapsu po 60 plných seancích (26. 2. 2027; S1 ≈ 60 obchodů):** kandidát se zamítne,
  když horní mez oboustranného 95% intervalu Ø čistého R dopředných obchodů < 0. Řádek
  `dopředně` skriptu.
- **Důkaz:** brána aplikace (n ≥ 30, jednostranná 95% dolní mez > 0) na dopředných obchodech.
  Při σ ≈ 2,6 R (S1) a 2,9 R (S2) vychází `risk.samples_needed` pro edge +0,2 R na ~1 051 a
  ~1 268 obchodů, tedy u S1 (~255 obchodů ročně) ≈ 4 roky. Zkrátit to může jen delší historie
  minutových barů NQ jako další mimovzorkový test; zdroj je otevřená otázka triáže.
- **Známka A > C** jen popisně, dokud A nemá ≥ 30 dopředných obchodů.

## Omezení

- 2,3 roku dat; zisk H3 stojí z 80–90 % na pěti nejlepších dnech, takže výsledek visí na
  několika trendových seancích.
- S1 dovoluje až 12 vstupů za seanci; nejhorší den (9. 6. 2026) je −920 $ na 1 MNQ (devět
  stopů). Denní brzda aplikace (3 R, ADR-0038) by takový den utnula; vliv brzdy na výsledek
  změřen není (nebyl předregistrovaný).
- Skluz 1 tick na stranu; stop příkazy v rychlém trhu mohou sklouznout víc (náklady ×2 ubírají
  ~1 b/den).
- První běh G2 dělil ATR closem téže seance (drobný pohled do budoucna). Skript a čísla výše
  používají close předchozí seance; pořadí známek se nezměnilo (A +0,37 → +0,34).
- Objem archivních a živých barů má jinou škálu; RVOL se proto počítá jen z okna s týmž zdrojem.

## Zdroje

- Zarattini, Aziz, Barbon (2024): *Beat the Market — An Effective Intraday Momentum Strategy
  for S&P500 ETF (SPY)* —
  [plný text](https://alexandria.unisg.ch/server/api/core/bitstreams/a99aba00-f967-49b3-aceb-f544dc386e0b/content)
- Zarattini, Barbon, Aziz (2024): *A Profitable Day Trading Strategy For The U.S. Equity
  Market* — [Alexandria](https://alexandria.unisg.ch/handle/20.500.14171/122125),
  [stránka autora](https://abarbon.com/papers/a-profitable-day-trading-strategy-for-the-u-s-equity-market)
- Gao, Han, Li, Zhou (2018): *Market Intraday Momentum*, Journal of Financial Economics.
- Baltussen, Da, Lammers, Martens (2021): *Hedging Demand and Market Intraday Momentum*,
  Journal of Financial Economics.
- Crabel (1990): *Day Trading with Short Term Price Patterns and Opening Range Breakout* (NR4/NR7).
