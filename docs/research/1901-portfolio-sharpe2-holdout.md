# Portfolio nohou MNQ/MES a cíl Sharpe > 2: jediný běh holdoutu (#1901)

> Zadání vlastníka 26. 11. 2026: navrhnout ziskovou strategii se Sharpe > 2 pro MNQ, pak MES (účet 5 000 $).
> Navazuje na `1901-kandidati-mnq-mes.md` (intradenní kandidáti). Tady: denní a kalendářní pravidla
> z literatury, složená do portfolia a ověřená na datech, která laboratoř nečetla.
> Data: minutové bary NQ a ES 29. 7. 2024 – 25. 11. 2026, 587 obchodních dnů (front kontrakt, přechod na další
> až v týdnu expirace); denní řady QQQ, SPY, VIX a TNX pro signály a dlouhou historii; kalendář makro událostí
> aplikace. Náklady 1 mikra: MNQ 1,11 b (2,22 $), MES 0,744 b (3,72 $) za round-trip. Sharpe = denní čisté USD
> přes všechny obchodní dny (nuly ve dnech bez obchodu) × √252; chyba podle Mertense (šikmost, špičatost).

## Verdikt

- **Sharpe > 2 není doložen.** Hlavní portfolio `book_mnq_clean` mělo na discovery Sharpe 2,33, na holdoutu
  **0,69** (95 % −1,38 až 2,76) a na celém vzorku 1,62 (t = 2,47). Předem zapsaný verdikt: **„zisk naznačen,
  neprůkazné"** (holdout > 0, t celku mezi 2,0 a 2,5). Samotný holdout kladné očekávání nedokládá (p ≈ 0,26).
- **Dvojka z discovery je nadsazená výběrem.** Průměrný Sharpe osmi noh spadl z 1,38 na 0,27. Kladných zůstalo
  5 z 8, z toho tři jen díky jedinému dni. Korelace Sharpe discovery × holdout je 0,05 (pořadová 0,19; při
  n = 8 to nic nerozliší).
- **Nejlépe vyšly dvě nohy z osmi, žádná průkazně:** swing po slabém závěru na ES (holdout 1,85, t = 1,76) a noc
  před FOMC na NQ (1,83, t = 1,74). Nejlepší z osmi noh bez hrany dosáhne 1,85 ve 23–28 % případů. Dvojčata na
  druhém symbolu vyšla hůř (swing NQ 0,17, noc před FOMC ES 0,24); rozdíl průkazný není (p ≈ 0,11 a 0,06).
- **Riziko celých mikro kontraktů je mimo účet 5 000 $.** `account_s`: holdout +2 655 $, propad −2 085 $
  (41,7 % počátečního vkladu), nejhorší den −1 129 $ (22,6 %). `account_m`: holdout −680 $, propad −7 807 $
  (156 % vkladu), nejhorší den −3 646 $ (72,9 %). Nohy bez stopu nemají omezené riziko na obchod, takže neprojdou
  sizingem ADR-0038 (1 % účtu na setup = 50 $, tvrdý strop 2 %).
- **Doporučení: neobchodovat.** Holdout je spotřebovaný; každá další úprava je nová hypotéza a měří se dopředu
  od 26. 11. 2026.

## Protokol

1. **Discovery** = seance do 31. 12. 2025 (360 dnů): 8 balíků strategií, 328 zapsaných pokusů (227 na futures,
   101 na dlouhé historii ETF). **Holdout** = 2. 1. – 25. 11. 2026 (227 dnů), fyzicky oddělená kopie dat, kterou
   laboratoř nečetla (všech 328 pokusů má rozsah discovery). Totéž období ale znal už první dokument pro
   momentum a fade do závěru, momentum pásma šumu, ORB 5 min a fade předchozího high/low — u těchto rodin
   holdout nezávislý není.
2. Před holdoutem zapsáno: pravidlo přijetí nohy (opora v dlouhé historii ETF s t ≥ 2,5 a kladným výsledkem
   po zveřejnění, nebo odolnost na discovery),
   tři konfigurace portfolia se škálami 1/σ z discovery, dvě účtové konfigurace s celými mikry, pásma verdiktu,
   otisky skriptů.
3. Každou nohu znovu naprogramoval nezávislý ověřovatel jen z popisu pravidel: 0 rozdílných dnů z 360 u všech.
   Audit look-ahead a test uříznutí dat (stejné denní řady na datech uříznutých 7. 7. 2025) bez nálezu.
4. Jediná oprava před holdoutem: noha rebalancování (interval 18:00 → 18:00) se zapisovala o seanci později.
   Po opravě kleslo discovery Sharpe hlavního portfolia z 2,66 na 2,31.
5. Holdout běžel **jednou** 27. 11. 2026 nezměněnými skripty (log holdoutu má 21 záznamů, každá noha jednou;
   otisky 14 skriptů odpovídají předregistraci). Řádky discovery vyšly stejně jako v discovery běhu (0 rozdílných
   dnů; u rebalancování přibyl jen 31. 12. 2025, jehož interval končí až v holdoutu → discovery 2,33).
6. Koncept tohoto dokumentu prošel nezávislou kontrolou: tři recenzenti (přepočet, data, statistika) a ke každému
   ověřovatel. Všechny tabulky se reprodukovaly z denních řad; ze 49 výhrad k textu bylo 43 uznáno a zapracováno.

## Nohy portfolia

Všechny vstupují marketem na open minutového baru, velikost 1 mikro, bez stopu a cíle. Výjimka: `rebal_ES_w`
drží zlomky MES (průměrná |pozice| 0,27, nejvíc 1,06 dne 23. 11. 2026).

| Noha | Pravidlo | Opora v dlouhé historii (denní bary ETF) |
|---|---|---|
| `a1` swing po slabém závěru | V 16:00 ET long, když platí aspoň 2 ze 4 stavů denního baru: IBS < 0,2 (ven při IBS > 0,8); RSI(2) < 10 nad SMA 200 (ven nad SMA 5); RSI(5) < 30, RSI(5) tři dny po sobě klesal a před třemi dny byl pod 60, nad SMA 200 (ven při RSI(5) > 50); pondělí se závěrem pod pátečním. Ven v 16:00, když platí méně než 2. | QQQ 2000–2025 t = 3,4; SPY 1994–2025 t = 6,2. Je to replikace vzorku zdroje, období po zveřejnění neexistuje; od 2014 proti buy-and-hold t = 2,0 / 3,0; pozice o den později QQQ t = 2,5. |
| `vixjump25_D` den po skoku VIX | VIX close vzrostl o ≥ 2,5 bodu → další obchodní den long 09:30 → 16:00 ET. | SPY t = 3,1; QQQ t = 2,9 (do 2025 včetně discovery okna). Po vzorku zdroje 2018/06–2025 t = 1,7, bez discovery okna t = 0,8. |
| `fade_calm` závěrečný fade (jen MNQ) | V 15:30 ET proti pohybu od předchozího závěru, když VIX < 20 a pohyb < 1 denní σ; ven 16:00. | žádná (jen discovery) |
| `fomc_night` noc před FOMC | Long v 16:00 ET den před oznámením FOMC (první den dvoudenního zasedání) → ven 09:30 ET v den oznámení. | SPY 1994–2025: 255 nocí, t = 3,0 (z toho 2011/04–2025 117 nocí t = 4,0; do 2011/03 138 nocí t = 1,1). QQQ 214 nocí, t = 3,8. Noční část byla vydělena až po rozkladu dat. |
| `rebal_ES_w` předbíhání rebalancování 60/40 | Zlomky MES 18:00 → 18:00 proti odchylce modelového portfolia 60/40 (prahové a měsíční rebalancování). Míra, ne obchodovatelná noha. | SPY 1993–2025 t = 5,1 při plnění na tomtéž close jako signál; o den později t = 2,1; po vzorku zdroje 2023–2025 t = 1,2. |

## Výsledky noh (1 mikro, po nákladech)

| Noha | Discovery $ / SR | Holdout $ | Holdout SR (t) | Holdout propad / nejhorší den | Celek $ / SR (t) |
|---|---|---|---|---|---|
| `a1_NQ` | 8 740 / 1,76 | 1 158 | 0,17 (0,16) | −5 780 / −3 646 | 9 898 / 0,79 (1,21) |
| `a1_ES` | 3 645 / 1,49 | 3 724 | 1,85 (1,76) | −699 / −579 | 7 369 / 1,64 (2,51) |
| `vixjump25_D_nq` | 8 716 / 1,48 | −2 174, 16 obch. | −0,76 (−0,72) | −4 687 / −2 361 | 6 543 / 0,74 (1,13) |
| `vixjump25_D_es` | 4 746 / 1,32 | −1 405, 16 obch. | −0,95 (−0,90) | −2 685 / −1 129 | 3 341 / 0,64 (0,98) |
| `fade_calm_NQ` | 1 932 / 1,31 | −845, 108 obch. | −0,63 (−0,60) | −2 024 / −748 | 1 087 / 0,38 (0,58) |
| `fomc_night_NQ` | 1 574 / 1,39 | 1 180, 7 obch. | 1,83 (1,74) | −160 / −160 | 2 754 / 1,55 (2,36) |
| `fomc_night_ES` | 630 / 1,36 | 46, 7 obch. | 0,24 (0,23) | −192 / −130 | 677 / 1,01 (1,54) |
| `rebal_ES_w` (opravené datování) | 2 337 / 0,90 | 882 | 0,45 (0,43) | −2 119 / −536 | 3 219 / 0,70 (1,07) |

U `rebal_ES_w` bylo před holdoutem známo discovery 2 200 $ / 0,85; hodnota v tabulce zahrnuje 31. 12. 2025
(+137 $), jehož interval končí v holdoutu.

**Kladných 5 z 8, tři jen díky jednomu dni.** Bez svého nejlepšího dne jsou záporné `rebal_ES_w` (23. 11. 2026
+1 316 $, bez něj −434 $), `a1_NQ` (11. 6. 2026 +1 859 $, bez něj −701 $) a `fomc_night_ES` (+46 $ ze 7 obchodů,
bez nejlepšího −47 $). Kladné i bez něj zůstávají jen `a1_ES` (2 818 $) a `fomc_night_NQ` (750 $).

**Noc před FOMC na NQ** stojí na sedmi nocích: +1 180 $, dvě nejlepší tvoří 66 %. Průměr se od sedmi náhodných
nocí holdoutu neliší (p = 0,23); Sharpe 1,83 drží nízký rozptyl těch sedmi nocí. Na ES daly tytéž noci +46 $.

**5. 11. 2026** stál `a1_NQ` 3 646 $ — víc než celý rozdíl zisku proti ES (2 566 $). NQ spadl od 16:00 do 16:00
o 6,4 % (28 567,75 → 26 744,75), ES o 4,4 %; denní řada z jiného zdroje ukazuje totéž (QQQ −6,5 %, SPY −4,4 %,
VIX 16,6 → 28,2) — pohyb je skutečný, ne chyba dat. `a1_NQ` byl ten den long, `a1_ES` vystoupil o den dřív.
Rozdíl Sharpe ale ten den vysvětluje jen ze 40 % (bez něj 0,84 proti 1,85); zbytek jsou rozdílné pozice
ve 40 z 227 dnů. Je to vlastnost pravidla bez stopu, ne smůla k odečtení.

**23. 11. 2026** byl NQ před otevřením na horním nočním limitu +7 % (26 878,50; 09:03–09:05 a 09:16–09:29,
celkem 17 minutových barů). `vixjump25_D_nq` má vstup zaúčtovaný za tuto cenu (open baru 09:30); pro market
příkaz ale nebyla spolehlivě dosažitelná (rozpětí baru 09:30: 26 839–27 137; podle open QQQ 27 041–27 106).
Se vstupem podle QQQ má noha na holdoutu −2 498 až −2 629 $ místo −2 174 $ (na low baru −2 095 $);
`book_mnq_clean` holdout 0,65–0,66 místo 0,69 a celek 1,60–1,61 (t = 2,45); `book_mnq` holdout 0,29–0,30 místo
0,33; `account_m` −1 005 až −1 135 $ s propadem −8 132 až −8 262 $. Verdikt se nemění; tabulky nesou zaúčtovaná
čísla jediného běhu.

## Portfolia a účty

Škály 1/σ z discovery zmrazené před holdoutem; účty jsou prostý součet celých mikro kontraktů.
P(SR > 2) je normální aproximace bez korekce na výběr.

| Konfigurace | Discovery SR | Holdout SR (95 %) | Celek SR (t; 95 %) | P(SR > 2) holdout / celek |
|---|---|---|---|---|
| `book_mnq_clean` (hlavní): a1_NQ, vixjump NQ, fomc_night NQ, rebal | 2,33 | 0,69 (−1,38 až 2,76) | 1,62 (2,47; 0,41–2,82) | 0,11 / 0,27 |
| `book_mnq`: totéž + fade_calm_NQ | 2,66 | 0,33 (−1,74 až 2,41) | 1,62 (2,48; 0,38–2,87) | 0,06 / 0,28 |
| `book_mes`: a1_ES, vixjump ES, fomc_night ES, rebal | 2,09 | 1,14 (−0,83 až 3,10) | 1,74 (2,66; 0,60–2,88) | 0,19 / 0,33 |
| `account_s`: 1 MES a1 + 1 MES vixjump + 1 MNQ fade + 1 MNQ fomc_night | 2,37 | 0,90 (−1,20 až 2,99) | 1,82 (2,78; 0,68–2,96) | 0,15 / 0,38 |
| `account_m`: 1 MNQ a1 + 1 MNQ vixjump + 1 MNQ fade + 1 MNQ fomc_night | 2,51 | −0,09 (−2,15 až 1,97) | 1,24 (1,90; −0,05–2,54) | 0,02 / 0,13 |

- Čísla za celek obsahují discovery, na kterém se nohy vybíraly → jsou nadsazená. Nezkreslený odhad je holdout.
- **Pásmo „zisk naznačen" říká málo.** Podmínku „holdout > 0 a t celku ≥ 2,0" splní hlavní portfolio i bez jakékoli
  hrany na holdoutu v polovině případů: t celku nese discovery a holdout se součtem nula dá t = 2,01.
- Verdikt byl předem vázán na `book_mnq_clean`. Stejná pásma by u `book_mes` dala „zisková, Sharpe > 2 nedoloženo"
  (holdout > 0, t celku 2,66); bez hrany na holdoutu to vyjde asi ve 20 % případů a nebyl to předem zvolený test.
- Účet `account_s` (počáteční vklad 5 000 $): discovery +11 896 $, holdout +2 655 $ (4 ztrátové měsíce z 11,
  nejhorší −1 674 $). Propad −2 085 $ je 41,7 % vkladu a 21,4 % z vrcholu 9 740 $; účet neklesl pod 4 950 $.
- Účet `account_m`: discovery +20 962 $, holdout −680 $ (nejhorší měsíc −4 104 $). Propad −7 807 $ je 156 %
  vkladu a 64 % z vrcholu 12 127 $ (22. 6. 2026); účet otevřený na vrcholu by nepřežil.

Předem dané vedlejší otázky:

- **Známka A u noci před FOMC** (VIX nad 252denním mediánem): na discovery A 8 nocí, průměr +179 $, ostatní 4 noci
  +35 $; na holdoutu A 4 noci, průměr +32 $, ostatní 3 noci +350 $. Pořadí se obrátilo → známka A nepotvrzena (n = 7).
- **Podíl pěti nejlepších dnů** na zisku holdoutu `book_mnq_clean`: 192 %. Bez nich je holdout záporný (Sharpe −0,78).

## Informativní běhy (do portfolia předem nepřijaté)

| Noha | Discovery $ / SR | Holdout $ | Holdout SR (t) | Holdout propad / nejhorší den | Celek $ / SR (t) |
|---|---|---|---|---|---|
| `s1_noise_nq_canon` momentum pásma šumu | 4 557 / 0,78 | 6 445, 195 obch. | 1,55 (1,48) | −1 831 / −659 | 11 002 / 1,10 (1,68) |
| `s1_noise_nq_hs50` totéž, tvrdý stop | 5 371 / 0,97 | 5 503, 258 obch. | 1,52 (1,44) | −1 430 / −818 | 10 874 / 1,19 (1,81) |
| `s2_atr_nq_base` ATR breakout | 5 323 / 0,80 | 3 036, 134 obch. | 0,66 (0,63) | −4 334 / −868 | 8 359 / 0,74 (1,13) |
| `orb5_both_10R` ORB 5 min (období známé z prvního dokumentu) | 3 535 / 0,69 | −1 442, 226 obch. | −0,31 (−0,29) | −5 864 / −451 | 2 093 / 0,21 (0,32) |
| `fade_calm_NQ_stop50` fade se stopem 50 $ | 2 283 / 1,67 | 222, 108 obch. | 0,23 (0,22) | −716 / −102 | 2 505 / 1,07 (1,63) |
| `rod_fade_NQ` fade bez brány klidu | 3 586 / 1,37 | 1 206, 227 obch. | 0,64 (0,61) | −2 219 / −748 | 4 792 / 1,06 (1,62) |
| `rebal_ES_disc05` celé MES (opravené datování) | 2 893 / 0,76 | 997 | 0,43 (0,40) | −2 297 / −780 | 3 890 / 0,63 (0,96) |
| `rebal_NQ_disc05` celé MNQ (opravené datování) | 5 551 / 0,81 | 3 895 | 0,71 (0,67) | −5 954 / −1 269 | 9 446 / 0,76 (1,16) |
| `prefomc_NQ_A_vixhi252` pre-FOMC drift s filtrem | 1 626 / 1,12 | −459, 4 obch. | −0,92 (−0,87) | −566 / −418 | 1 166 / 0,58 (0,88) |
| `fomcrev_NQ_5b` FOMC reverze | 4 195 / 0,85 | −6 214 | −0,99 (−0,94) | −9 692 / −3 646 | −2 018 / −0,17 (−0,26) |
| `a1_stop2_NQ` swing se stopem 2 % ceny | 7 050 / 1,80 | −3 473, 15 obch. | −0,87 (−0,83) | −6 704 / −1 228 | 3 577 / 0,44 (0,67) |
| `c1_NQ` příbuzný swing | 8 007 / 1,58 | 2 450 | 0,42 (0,40) | −4 024 / −3 646 | 10 456 / 0,91 (1,39) |
| `ibs_NQ` samotné IBS | 6 900 / 0,75 | 568 | 0,06 (0,06) | −5 426 / −3 646 | 7 468 / 0,41 (0,62) |

`a1_stop2_NQ` se účtuje po obchodech v den výstupu, takže jeho Sharpe, propad a nejhorší den nejsou s `a1_NQ`
srovnatelné. U `rebal_*_disc05` jsou v logu pokusů pod stejným jménem čísla s původním datováním (ES 2 675 / 0,71
a 1 215 / 0,51; NQ 5 141 / 0,77 a 4 304 / 0,78); discovery v tabulce zahrnuje 31. 12. 2025 (ES +218 $, NQ +410 $),
jehož interval končí v holdoutu.

Momentum pásma šumu vyšlo na holdoutu nejlépe (+6 445 $), ale nezávislé potvrzení to není: jeho výsledek na celém
vzorku do 25. 11. 2026 byl znám už z `1901-kandidati-mnq-mes.md` (S1 +0,19 R, t = 1,74). Na discovery noha
neprošla pravidlem přijetí: vstup o minutu později ponechal 29 % výsledku a pět nejlepších dnů tvořilo 162 % zisku
(u varianty s tvrdým stopem 49,9 % a 132 %).

## Co z toho plyne

1. **Dvojka z discovery je nadsazená výběrem; o kolik, data neurčí.** Pět přijatých noh varianty MNQ prošlo
   pravidlem přijetí, nebyly to nejlepší pokusy (podle discovery Sharpe 5., 16., 22., 34. a 70. z 227 pokusů na
   futures; hlavní portfolio má čtyři z nich, `fade_calm_NQ` je jen v `book_mnq`). Měřené příspěvky: oprava
   datování −0,33 (2,66 → 2,33 včetně 31. 12. 2025), škály z téhož vzorku asi −0,3. Předregistrace čekala holdout 1,2–1,5,
   před během upřesněno k dolnímu okraji 1,0–1,3; vyšlo 0,33 / 0,69 / 1,14. Sám rozdíl 2,33 proti 0,69 průkazný
   není (z ≈ 1,3), takže výběr, šum a změnu režimu od sebe oddělit nejde.
2. **Co data říkají:** holdout 0,3–1,1 (hlavní portfolio 0,69, 95 % −1,4 až 2,8) kladné očekávání nedokládá
   (p ≈ 0,26; bez pěti nejlepších dnů −0,78) ani nevylučuje. Odhad „kolem Sharpe 1" je předpoklad z dlouhé
   historie noh, ne výsledek měření; celek 1,6–1,7 je nadsazený výběrem.
3. **Dodatečný výběr nic nedokazuje.** Účet 1 MES `a1_ES` + 1 MNQ `fomc_night_NQ` má na holdoutu Sharpe 2,35 a na
   celku 2,04 (+10 123 $, propad −1 184 $, nejhorší den −579 $). Jsou to dvě nejlepší nohy z osmi vybrané po
   odkrytí holdoutu: nejlepší z 28 dvojic bez hrany dosáhne 2,35 v 11–16 % případů. Dodatečný je i propad —
   5. 11. 2026 by tatáž noha v pozici stála 1 MES 1 702 $ (34 % vkladu); vystoupila o den dřív. Číslo je popis,
   ne odhad.
4. **Délka dat rozliší jen velký rozdíl.** Chyba ročního Sharpe z denních dat je zhruba √((1 + SR²/504) / roky)
   ≈ 1/√roky: 0,66 na 2,33 roku, 1,05 na holdoutu. Předregistrované kritérium „doloženo" (holdout ≥ 2,0 a dolní
   mez 95 % celku > 1,0, tedy celek asi ≥ 2,3) dosažitelné bylo; vyšlo 0,69 a 1,62. Jemnější otázku data
   nerozhodnou: skutečný Sharpe 2–2,5 se na 2,33 roku od hodnoty pod 2 odlišit nedá (dolní mez nad 2 dá až
   pozorovaný Sharpe ≳ 3,3). Strategie se skutečným Sharpe 2 potřebuje ~1 rok, aby dolní mez přešla nulu,
   a ~3,9 roku, aby přešla 1.
5. **Bez stopu to účet 5 000 $ neunese.** Jeden den (5. 11. 2026) stál 1 MNQ 3 646 $. Jediná zkoušená varianta
   swingu se stopem (2 % ceny ≈ 1 000–1 230 $ na 1 MNQ, ne 50 $) dopadla na holdoutu hůř (−3 473 $ proti +1 158 $,
   rozdíl neprůkazný). Stop v rozpočtu ADR-0038 u swingu zkoušen nebyl.

## Další krok (k rozhodnutí vlastníka)

Holdout je spotřebovaný: na 587 dnech už nejde nic nezávisle potvrdit. Všechny cesty vedou jen dopředu od
26. 11. 2026.

| Varianta | Výhody | Nevýhody |
|---|---|---|
| A. Směr uzavřít jako negativní výsledek | Žádná další práce; nehrozí další ladění na spotřebovaných datech. | Dvě nejlepší nohy zůstanou bez dopředného měření. |
| B. Dopředné měření `a1_ES`, `fomc_night_NQ` a momenta pásma šumu se zmrazenými pravidly, bez zásahu do enginu a bez obchodování (rozšíření skriptu z #1901) | Levné (jeden skript nad bary); jediný způsob, jak získat nezávislá data. | Rok měření má chybu Sharpe asi ±1 (1σ; na 95 % asi ±2) a sám nerozhodne; při skutečném Sharpe 1 jsou potřeba asi 4 roky. Swing dělá ~15–18 obchodů ročně, noc před FOMC 8. |
| C. Nová hypotéza nad daty, která veřejná pravidla nemají (opční positioning aplikace), se stopem v rozpočtu ADR-0038 | Jediný směr, kde může být hrana mimo veřejně známé efekty; sedí na risk framework účtu. | Nový výzkum s vlastní předregistrací; historie těchto dat je krátká (minutové snímky opcí od 19. 7. 2026, asi 4 měsíce), takže se měří hlavně dopředu. |

Doporučení: **B** jako levné pokračování bez obchodování; **C** jako samostatné zadání do triáže.

## Omezení

- Jeden holdout o 227 dnech, chyba ročního Sharpe ≈ 1,05. Rozdíl mezi 0,7 a 1,6 není statisticky rozlišitelný.
- Znaménko závěrečného fade bylo na holdoutu známé z prvního dokumentu; noha přesto vyšla záporně.
- `rebal_ES_w` drží zlomky mikra — je to míra efektu, ne obchodovatelná noha; `book_*` proto nejsou obchodovatelná
  portfolia, obchodovatelné jsou jen `account_*`.
- Plnění marketem na open baru s konstantním skluzem 1 tick na stranu; marže a likvidita přes noc nemodelovány.
  Bar s objemem existuje na všech minutách plnění ve všech 227 seancích. Výnosy sedí na QQQ/SPY s mediánem
  odchylky 0,02 p. b. (95. percentil 0,05–0,08 p. b.). Největší odchylka je 23. 11. 2026 (NQ přes noc −0,9 p. b.,
  ES −0,2 p. b.): zámek na limitu model nezná.
- Plnění na dobíhajícím kontraktu před rollem není ověřeno proti skutečné likviditě: 9 seancí holdoutu na symbol
  (16.–19. 3., 15.–17. 6., 15.–16. 9. 2026) má 5–44 % běžného objemu RTH. Padají do nich 3 ze 7 nocí před FOMC
  (na NQ +539 $ z +1 180 $), 1 z 16 obchodů `vixjump` a 3 ze 108 obchodů `fade_calm`.
- Bary jsou NQ a ES; ceny a likvidita MNQ a MES se berou jako shodné.
- 2,3 roku jednoho režimu trhu (2024–2026). Dlouhá historie je na denních barech ETF (close a open, po nákladech
  ETF a financování), ne na futures s jejich plněním, a končí rokem 2025, takže obsahuje i discovery okno.
- Škály 1/σ z discovery přidávaly na discovery asi 0,3 Sharpe; na holdoutu jsou kauzální.
- Výpočet z doby před holdoutem „pět nejlepších z dvaceti strategií bez hrany dá Sharpe ≥ 2,6 ve 28 % případů"
  je jen ilustrace: při deseti strategiích vyjde 3–4 %, při padesáti 87 %. Počet nezávislých pokusů není znám,
  takže to není korekce na výběr.

## Reprodukce

Pravidla v tabulce noh a v tomto dokumentu odpovídají zmrazeným skriptům; slepá reimplementace z popisu dala shodu
na den. Otisky zmrazených skriptů (sha256, prvních 16 znaků), zapsané před holdoutem:

```
746c911ebd3e9c3b  swing/final_a1_NQ.py
cfa59969213c2476  swing/final_a1_ES.py
b95f91636a2c4efc  overnight/final_vixjump25_D_nq.py
c791cfee6c99cbc0  overnight/final_vixjump25_D_es.py
c775ae01e4c2c1bf  eod/final_fade_calm_NQ.py
329b42606f63e9cf  calendar/final_fomc_night_NQ.py
81a3572505a38f99  calendar/final_fomc_night_ES.py
044c263620ef72b5  calendar/final_rebal_ES_w.py
```

Laboratoř (skripty, log 328 + 21 pokusů, předregistrace) není součástí repa.

## Zdroje

- Pagonidis (2013), The IBS Effect: Mean Reversion in Equity ETFs, NAAIM.
- Connors, Alvarez (2008), Short Term Trading Strategies That Work (RSI(2)).
- Empirica Lab (2026), Four dip rules in one account — kombinace „aspoň 2 ze 4" u nohy `a1`.
- Hu, Pan, Wang, Zhu (2022), Premium for Heightened Uncertainty, JFE 145(3) — den po skoku VIX.
- Lucca, Moench (2015), The Pre-FOMC Announcement Drift, JF 70(1).
- Boyarchenko, Larsen, Whelan (2023), The Overnight Drift, RFS 36(9).
- Harvey, Mazzoleni, Melone (2025), The Unintended Consequences of Rebalancing, NBER WP 33554.
- Baltussen, Da, Lammers, Martens (2021), Hedging demand and market intraday momentum, JFE.
- Zarattini, Aziz, Barbon (2024), Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY).
- Lo (2002), The Statistics of Sharpe Ratios, FAJ; Mertens (2002), poznámka k chybě Sharpe při šikmosti a špičatosti.
- Bailey, López de Prado (2014), The Deflated Sharpe Ratio, JPM.
