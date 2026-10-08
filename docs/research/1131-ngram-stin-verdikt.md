# Ngram stín (magnitudová hlava) — verdikt na živých datech (#1131)

> E-0.11 Fáze 0 (#1387) epiky #1385 · data do 8. 10. 2026 00:00 UTC · skript
> `scripts/measure_ngram_shadow_1131.py` (jen čtení PG, pevné semínko) · zadání: #740 varianta B
> (rozhodnutí 12. 9. 2026), brána #749 · vstup ADR-0053 (E-6.1, Fáze 6 #1406).

## Verdikt

- **Předregistrované číslo jobu nejde použít.** Podle rozhodnutí z 12. 9. se mělo rozhodnout „nad
  jedním číslem z jobu“: lift hlavy na subsetu `live` proti baseline kategorie, při n ≥ 2 000.
  - Baseline jobu řadí zprávy podle průměru kategorie, takže celá kategorie má jedno skóre.
  - Horní decil ES má 1 662 míst. GEOPOLITICS (326), MACRO_GROWTH (69) a MACRO_LABOR (339) ho
    nezaplní, takže hranice padne dovnitř FED: 928 míst pro 2 821 řádků. Které z nich se do
    decilu dostanou, rozhodne pořadí, v jakém je vrátí PG (dotaz nemá ORDER BY).
  - Číslo jobu proto mezi běhy přeskakuje, zatímco lift hlavy drží 1,10–1,12 (tabulka Historie
    jobu). Ze 42 běhů vyšlo u ES 29 průchodů a 13 neprůchodů, všechny s `baseline_source =
    training`. Po sobě:
    - 12.–16. 9. převážně neprůchod (8 z 9 běhů);
    - 16. 9. 21:09 až 26. 9. 15:46 průchod, **v termínu (26. 9. 15:46, n 3 635) ES 1,039 proti
      0,869**, NQ 1,018 proti 0,872;
    - 26. 9. 22:25 až 29. 9. neprůchod (baseline 1,27–1,49);
    - 30. 9. až 7. 10. 00:02 průchod;
    - 7. 10. 17:27 neprůchod (1,111 proti 1,659);
    - 8. 10. průchod (10:06, n 16 778: 1,110 proti 1,008).
  - Při náhodném pořadí řádků vychází baseline ES 1,19–1,30 (5.–95. percentil). Fyzické pořadí
    tabulky je shlukované v čase, proto skáče job víc.
  - Pro běhy po přepočtu kontaminace (26. 9. 22:25) je remíza v tomto měření doložená. Pro běhy
    před ním, včetně běhu v termínu, je pravděpodobná (stejná konstrukce baseline), ověřit ji ale
    nejde: `cont_5` i kategorie se 26. 9. přepsaly.
  - Číslo v termínu formálně ukazovalo průchod. Tento verdikt ho nahrazuje přepočtem se shodami
    započtenými očekávanou hodnotou. Je to oprava chyby měření, mění ale rozhodovací číslo.
- **Po opravě remíz kritérium splněné není. Srovnání hlavy s kategorií ale závisí na metrice.**
  - **Sdružený lift** (metrika jobu) proti baseline kategorie z tréninku jobu: hlava je pod ní ve
    všech variantách vzorku. Tento trénink sahá do 8. 10. a obsahuje i hodnocené řádky.
    - Předregistrovaná definice vzorku (definice jobu: 16 626 ES, 16 470 NQ): ES 1,111 proti
      1,248, rozdíl −0,137 [−0,260; −0,011]; NQ −0,108 [−0,243; +0,048].
    - Vzorek podle bodu 3 zadání (bez neúplných seancí, navíc bez deferred reakcí):
      ES −0,073 [−0,259; +0,156], NQ −0,025 [−0,164; +0,127].
  - **Proti walk-forward baseline** (průměry kategorií jen z dní před událostí, plán session) je
    hlava bodově pod ní v 7 z 8 kombinací. Výjimka je NQ v definici jobu: +0,151
    [−0,330; +0,470]. Všechny intervaly jdou přes nulu. Sdružené číslo této baseline navíc nejde
    číst: ES má sdruženě 1,55, ale v každém týdnu jen 0,80–1,16.
  - **Uvnitř seance je to naopak.** Hlava tam řadí lépe než kategorie: lift 1,122 [1,064; 1,182]
    proti 1,013 [0,948; 1,078], u NQ 1,094 proti 1,012. Výhoda kategorie ve sdruženém liftu tedy
    vzniká mezi dny. Uvnitř dne kategorie skoro nic neřadí (ρ +0,023 [−0,016; +0,071]).
  - Kritérium tak formálně neprošlo, samo o sobě by ale na zamítnutí nestačilo: po opravě remíz
    měří hlavně rozdíly mezi dny. Předpověď z 12. 9. („hlava baseline neporazí“) platí pro
    sdružený lift, uvnitř seance ne.
- **Rozhoduje dodatečná kontrola, která předregistrovaná nebyla: text nenese informaci o velikosti
  pohybu nad rámec denní doby.**
  - Ve vrstvách seance × hodina UTC má hlava pořadovou korelaci s |ret_5| −0,009 [−0,028; +0,008],
    u NQ −0,011 [−0,031; +0,009]. Celou výhodu hlavy uvnitř seance tedy nesou rysy hodina
    a zdroj (`h=`, `src=`).
  - Nulový model bez textu (průměr |ret_5| buňky zdroj × hodina z minulosti) je lepší než hlava.
    Uvnitř seance má lift 1,346 [1,202; 1,494], u NQ 1,274. Sdruženě dává 1,401 proti 1,111.
  - `importance` z hlavy by tak v praxi znamenala jen „zpráva v aktivní hodině = důležitá“.
- **Průřez `dir × ret` (vstup ADR-0053): směr pravidel na 5min okně živě nic nenese.**
  - Pravidla se vyjádří u 16 % zpráv (ES 2 230 ze 14 147).
  - Úspěšnost proti driftu: ES long −0,005 [−0,026; +0,017], short −0,006 [−0,043; +0,028];
    NQ long +0,001, short −0,022 [−0,054; +0,011].
  - Shoduje se s #749 (směr out-of-sample neprošel).
- **Doporučení: varianta C** (viz Varianty): `importance` z hlavy nezapínat a stín vypnout
  (`ngram_shadow_enabled` = false). Historie `source='ngram'` zůstane v PG. Doporučení stojí na
  dodatečné kontrole denní doby, ne na předregistrovaném čísle.

## Varianty

| | výhody | nevýhody |
|---|---|---|
| A. Zapnout `importance` z hlavy | – | kritérium nesplněno a importance by kopírovala denní dobu, kterou obchodník vidí na hodinách |
| B. Prodloužit stín | průběžná kontrola trvá | chybí důvod: 15 tis. vzorků a ρ uvnitř hodiny do ±0,03, víc dat obraz nezmění. Denní trénink stojí 162 s CPU nad 142 777 vzorky (log news-enginu 8. 10. 10:08 UTC) a drží největší matice news-enginu (#1105). Přibývá ~2,4 tis. řádků `ngram` denně (100 408 za 26. 8.–7. 10., `1381-tvrda-data.md` kap. 3). Číslo jobu by se muselo opravit (#1467), jinak dál klame |
| **C. Zahodit: nezapínat a stín vypnout** | ušetří CPU a paměť news-enginu a zmizí číslo, které střídá falešný průchod s neprůchodem | průběžné číslo zmizí; skript jde ale pustit kdykoli nad uloženou historií |

Vypnutí znamená:
- změnit default `ngram_shadow_enabled` v `news-engine/src/gexlens_news/config.py` (produkční
  `.env` klíč nemá);
- upravit `.env.example`;
- nasadit news-engine v pauze Globexu.

Udělá se samostatným issue, až vlastník rozhodne.

## Jak číst a co data neříkají

- **Vzorek** pokrývá 31 seancí od 26. 8. do 8. 10. 2026 (8. 10. jen do 00:00 UTC), tedy jedno
  období. ES a NQ se měří na stejných zprávách, nejsou tedy nezávislé. Bootstrap losuje celé
  seance, protože zprávy jednoho dne nezávislé nejsou: pravidlo K1 nechá ve stejném 5min okně víc
  zpráv téže kategorie.
- **Kontaminace se 26. 9. změnila zpětně pro celou historii.** Reklasifikace v2 přepočetla
  `cont_5` pravidlem K1: okno kazí jen jiná kategorie s importance ≥ 2. Popisuje to
  `data/reports/reclass-run-20260927.txt` (mimo repo) a ADMIN-MANUAL kap. 12.
  - Živý vzorek jobu proto 26. 9. skočil z 3 635 na 11 961 řádků a Ø |ret| z 2,34 na 4,84 bp
    (Historie jobu).
  - Hlava se do 26. 9. učila nad starou definicí, od 27. 9. nad K1. Týdenní tabulka zlom nemá
    (ES 1,06–1,20).
- **Deferred reakce** vznikly ze zpráv při zavřeném trhu: 1 386 řádků ES s Ø |ret| 22,8 bp
  (v nejvyšší seanci 56,7 bp), ostatní živé řádky mají 3,0 bp (sekce Vzorek). Sdílejí výnos
  uzavírky, tedy víkendový gap, takže jde o desítky kopií jednoho měření. Vzorek podle bodu 3 je
  vynechává.
  - Neúplné seance (méně než 50 % mediánu živých řádků) vynechává také: 26. 8. (start stínu),
    21. 9. a 8. 10. (konec dat).
  - Vynechání má na výsledek jen malý vliv (varianta „vč. výpadků“).
- **Zdroje vzorku** jsou v sekci Vzorek. Vzorek nese bluesky a alpaca, `rss_news` je pod 2 %, takže
  výpadek Yahoo od 24. 9. (#1451) ho prakticky nemění.
- **Walk-forward baseline kategorie** má sdružený lift 1,55 (ES, vzorek podle bodu 3), ale
  v každém týdnu jen 0,80–1,16. U skóre, které se mění v čase, míchá sdružené číslo pořadí uvnitř
  dne s rozdíly mezi dny. Proto report přidává diagnostiku uvnitř seance a uvnitř seance × hodina.
  Proč kategorie vychází lépe mezi dny (např. víc zpráv FED ve volatilních dnech), report neměří.
- **Walk-forward a look-ahead:** walk-forward bere trénovací řádky do začátku UTC dne události.
  Deferred trénovací řádky z pátku večer a ze soboty mají výnos známý až po nedělním otevření, takže
  sloupce walk-forward ve variantě job mají u nedělních řádků mírný look-ahead. Vzorek podle bodu 3
  deferred neobsahuje a verdikt to neovlivní.
- **Co bylo předem a co se doplnilo:**
  - Předregistrováno 12. 9. (#740): verdikt nad jedním číslem z jobu (sdružený lift, subset `live`,
    baseline z tréninku), brána n ≥ 2 000 a předpověď „hlava baseline neporazí“.
  - Plán session (8. 10., komentář v #1131) přidal shodu s jobem, přísně out-of-sample
    walk-forward baseline, bootstrap po dnech, vyřazení výpadků, deferred jako jedno měření
    a `dir × ret`.
  - Odchylky:
    1. Číslo jobu nahradil přepočet se shodami započtenými očekávanou hodnotou. Je to oprava
       chyby, mění ale rozhodovací číslo.
    2. Hlavní srovnání zůstalo proti baseline z tréninku jobu, jak bylo předregistrováno;
       walk-forward stojí vedle.
    3. Deferred reakce jsou vyřazené, ne sloučené do jednoho měření na uzavírku. `closure_open_ts`
       má ve vzorku vyplněné jen 2 uzavírky a reakce uzavírky měří gap, ne zprávu.
    4. Nulový model zdroj × hodina přibyl po průzkumu dat, před prvním během skriptu.
    5. Diagnostika uvnitř seance a seance × hodina přibyla po prvním běhu.
  - Doporučení C stojí na bodech 4 a 5, tedy na kontrolách, které předregistrované nebyly.
- **Experiment #749 má stejnou slabinu.** `scripts/news_predictor_experiment.py:247` řadí baseline
  kategorie přes `np.argsort` bez ošetření remíz a denní dobu nekontroluje. Jeho závěr „velikost
  ANO“ (backfill, lift 1,44 proti 1,27) proto nedokládá, že velikost nese text. Pro E-6.1 platí
  tento živý verdikt. Oprava liftu: #1467.
- **Rozsah:** všechno platí pro 5min okno a tuto hlavu. Delší okna (15, 60 min, denní) a jiný
  model, např. s rysy hustoty zpráv, tento verdikt nepokrývá. Patří do E-6.x (ADR-0053).
- **Testy:** skript testy nemá, stejně jako ostatní jednorázové měřicí skripty v `scripts/`.
  Výpočty `lift` (remízy), `_ranks` a `session_key` ověřil nezávislý ověřovatel na syntetických
  datech.

## Měření (výstup skriptu)

### Vzorek

- Klasifikací `ngram` s reakcí (5 min, nekontaminované): 137688; živých: 33096 (ES 16626, NQ 16470).
- Deferred (trh zavřený, výnos uzavírky): ES 1386, Ø |ret| 22,78 bp (nejvyšší seance 56,74 bp); ostatní živé ES Ø |ret| 2,97 bp.
- Zdroje živých řádků ES: bluesky 66,6 %, alpaca 31,4 %, rss_news 1,4 %, reddit_rss 0,4 %, ibkr_brfg 0,2 %, fed_rss 0,0 %.
- Seancí s živými řádky ES: 31, medián 511 řádků; výpadek (< 50 % mediánu): 26. 8. (171), 21. 9. (112), 8. 10. (32).
- Trénovací populace (ES, do 08. 10. 2026 00:00 UTC): 142338 řádků.

### Remízy: baseline jobu závisí na pořadí řádků

Lift baseline s remízou rozhodnutou náhodným pořadím řádků (500 pořadí) proti očekávané hodnotě (opravený výpočet). Job remízu rozhoduje pořadím, v jakém řádky vrátí PG (dotaz bez ORDER BY) — jeho čísla jsou v tabulce Historie jobu.

| symbol | n | lift modelu | baseline: 5 % | medián | 95 % | očekávaná |
|---|---|---|---|---|---|---|
| ES | 16626 | 1,111 | 1,192 | 1,245 | 1,300 | 1,248 |
| NQ | 16470 | 1,110 | 1,155 | 1,217 | 1,283 | 1,218 |

Horní decil ES = 1662 řádků; kategorie v pořadí průměru z tréninku: `GEOPOLITICS` 326, `MACRO_GROWTH` 69, `MACRO_LABOR` 339, `FED` 2821 — hranice decilu padá dovnitř poslední z nich a job z ní bere řádky v pořadí, v jakém je vrátí PG.

### Lift modelu proti baseline

Rozdíl = lift modelu − lift baseline; interval 95 % z bootstrapu po seancích (2000 replik). Brána #749: rozdíl proti baseline kategorie > 0 na n ≥ 2000.

| varianta | symbol | n | seancí | Ø \|ret\| bp | lift modelu | kategorie (job): lift / rozdíl [95 %] | kategorie WF: lift / rozdíl [95 %] | zdroj × hodina WF: lift / rozdíl [95 %] |
|---|---|---|---|---|---|---|---|---|
| job | ES | 16626 | 31 | 4,62 | 1,111 | 1,248 / −0,137 [−0,260; −0,011] | 1,161 / −0,050 [−0,571; +0,388] | 1,401 / −0,290 [−0,459; +0,001] |
| job | NQ | 16470 | 31 | 7,40 | 1,110 | 1,218 / −0,108 [−0,243; +0,048] | 0,959 / +0,151 [−0,330; +0,470] | 1,432 / −0,322 [−0,498; −0,043] |
| opravený | ES | 14925 | 28 | 2,98 | 1,123 | 1,196 / −0,073 [−0,259; +0,156] | 1,546 / −0,424 [−0,905; +0,234] | 1,296 / −0,173 [−0,466; +0,050] |
| opravený | NQ | 14719 | 28 | 4,37 | 1,091 | 1,116 / −0,025 [−0,164; +0,127] | 1,344 / −0,253 [−0,611; +0,227] | 1,198 / −0,107 [−0,332; +0,027] |
| opravený od 12. 9. | ES | 9499 | 17 | 3,08 | 1,101 | 1,285 / −0,184 [−0,374; +0,177] | 1,503 / −0,403 [−0,880; +0,287] | 1,345 / −0,244 [−0,601; +0,084] |
| opravený od 12. 9. | NQ | 9380 | 17 | 4,30 | 1,081 | 1,161 / −0,080 [−0,257; +0,179] | 1,343 / −0,262 [−0,599; +0,310] | 1,243 / −0,162 [−0,397; +0,023] |
| opravený vč. výpadků | ES | 15240 | 31 | 2,97 | 1,122 | 1,194 / −0,073 [−0,247; +0,152] | 1,547 / −0,425 [−0,899; +0,238] | 1,293 / −0,171 [−0,450; +0,035] |
| opravený vč. výpadků | NQ | 15034 | 31 | 4,36 | 1,086 | 1,113 / −0,028 [−0,169; +0,130] | 1,343 / −0,257 [−0,601; +0,216] | 1,226 / −0,140 [−0,346; +0,009] |

### Po týdnech (opravený vzorek)

| týden od | symbol | n | lift modelu | kategorie WF | zdroj × hodina WF |
|---|---|---|---|---|---|
| 24. 8. | ES | 861 | 1,204 | 1,159 | 1,381 |
| 24. 8. | NQ | 861 | 1,212 | 1,024 | 1,231 |
| 31. 8. | ES | 2664 | 1,146 | 0,953 | 1,218 |
| 31. 8. | NQ | 2652 | 1,130 | 1,068 | 1,158 |
| 7. 9. | ES | 1901 | 1,095 | 0,905 | 1,399 |
| 7. 9. | NQ | 1826 | 1,014 | 0,868 | 1,402 |
| 14. 9. | ES | 3520 | 1,116 | 0,799 | 1,365 |
| 14. 9. | NQ | 3401 | 1,091 | 0,841 | 1,152 |
| 21. 9. | ES | 1935 | 1,064 | 0,969 | 1,232 |
| 21. 9. | NQ | 1935 | 1,001 | 0,941 | 1,294 |
| 28. 9. | ES | 2394 | 1,124 | 0,931 | 1,288 |
| 28. 9. | NQ | 2394 | 1,098 | 0,934 | 1,242 |
| 5. 10. | ES | 1650 | 1,080 | 1,026 | 1,518 |
| 5. 10. | NQ | 1650 | 1,095 | 1,119 | 1,412 |

### Diagnostika uvnitř seancí (opravený vzorek)

Vážený průměr přes vrstvy s ≥ 20 řádky; interval 95 % z bootstrapu po seancích. ρ = Spearman skóre × |ret_5|.

| symbol | metrika | vrstev | řádků | model | kategorie (job) | kategorie WF | zdroj × hodina WF |
|---|---|---|---|---|---|---|---|
| ES | lift uvnitř seance | 28 | 14925 | 1,122 [1,064; 1,182] | 1,013 [0,948; 1,078] | 0,989 [0,928; 1,051] | 1,346 [1,202; 1,494] |
| ES | ρ uvnitř seance | 28 | 14925 | +0,088 [+0,069; +0,106] | +0,023 [−0,016; +0,071] | −0,004 [−0,047; +0,047] | +0,202 [+0,154; +0,249] |
| ES | ρ uvnitř seance × hodina | 338 | 11758 | −0,009 [−0,028; +0,008] | +0,003 [−0,020; +0,025] | +0,003 [−0,020; +0,025] | −0,019 [−0,039; +0,003] |
| NQ | lift uvnitř seance | 28 | 14719 | 1,094 [1,036; 1,157] | 1,012 [0,956; 1,069] | 0,989 [0,936; 1,042] | 1,274 [1,156; 1,385] |
| NQ | ρ uvnitř seance | 28 | 14719 | +0,075 [+0,060; +0,088] | +0,030 [−0,004; +0,071] | −0,007 [−0,043; +0,037] | +0,142 [+0,100; +0,183] |
| NQ | ρ uvnitř seance × hodina | 334 | 11515 | −0,011 [−0,031; +0,009] | +0,005 [−0,015; +0,025] | −0,002 [−0,021; +0,019] | −0,025 [−0,044; −0,007] |

### Průřez `dir × ret` (pravidla, opravený vzorek)

Směr první pravidlové klasifikace vzniklé ≤ 5 min od události × znaménko ret_5 (ret = 0 vynecháno). Úspěšnost = podíl reakcí ve směru; očekávaná = P(nahoru) celého vzorku symbolu pro long, 1 − P(nahoru) pro short. Interval rozdílu z bootstrapu po seancích.

| symbol | směr | n | P(nahoru) | Ø ret bp | úspěšnost | očekávaná | rozdíl [95 %] |
|---|---|---|---|---|---|---|---|
| ES | long | 1185 | 0,498 | −0,08 | 0,498 | 0,503 | −0,005 [−0,026; +0,017] |
| ES | 0 | 11917 | 0,503 | −0,08 | – | – | – |
| ES | short | 1045 | 0,509 | +0,12 | 0,491 | 0,497 | −0,006 [−0,043; +0,028] |
| NQ | long | 1219 | 0,503 | −0,00 | 0,503 | 0,502 | +0,001 [−0,025; +0,028] |
| NQ | 0 | 12271 | 0,500 | −0,05 | – | – | – |
| NQ | short | 1069 | 0,524 | +0,16 | 0,476 | 0,498 | −0,022 [−0,054; +0,011] |

### Historie jobu (`news_ngram_shadow_history`, subset live, stav při běhu skriptu)

| běh (UTC) | baseline z | ES n | ES Ø \|ret\| bp | ES lift | ES baseline | NQ lift | NQ baseline | trénink |
|---|---|---|---|---|---|---|---|---|
| 12. 09. 21:11 | training | 1807 | 2,11 | 0,984 | 1,165 | 1,005 | 0,913 | 76562 |
| 13. 09. 00:02 | training | 1807 | 2,11 | 0,984 | 1,158 | 1,005 | 0,950 | 76562 |
| 14. 09. 00:03 | training | 1819 | 2,23 | 0,920 | 1,105 | 0,916 | 0,895 | 76703 |
| 14. 09. 08:49 | training | 1887 | 2,21 | 0,951 | 1,112 | 0,940 | 0,911 | 76881 |
| 14. 09. 16:55 | training | 1955 | 2,29 | 0,952 | 1,113 | 0,954 | 0,854 | 77060 |
| 14. 09. 17:15 | training | 1960 | 2,29 | 0,950 | 1,115 | 0,954 | 0,938 | 77099 |
| 15. 09. 00:02 | training | 2048 | 2,30 | 0,964 | 1,024 | 0,955 | 0,920 | 77328 |
| 15. 09. 21:05 | training | 2242 | 2,35 | 1,000 | 0,992 | 0,981 | 0,917 | 77861 |
| 16. 09. 00:01 | training | 2252 | 2,34 | 1,001 | 1,005 | 0,981 | 0,990 | 77909 |
| 16. 09. 21:09 | training | 2344 | 2,30 | 1,009 | 0,861 | 1,001 | 0,925 | 78279 |
| 17. 09. 00:02 | training | 2344 | 2,30 | 1,009 | 0,861 | 1,001 | 0,911 | 78287 |
| 17. 09. 06:56 | training | 2346 | 2,30 | 1,009 | 0,861 | 1,001 | 0,920 | 78371 |
| 18. 09. 00:02 | training | 2437 | 2,30 | 1,010 | 0,850 | 0,965 | 0,902 | 78764 |
| 19. 09. 00:04 | training | 2631 | 2,27 | 1,001 | 0,895 | 0,969 | 0,886 | 79386 |
| 20. 09. 00:01 | training | 2631 | 2,27 | 1,001 | 0,917 | 0,969 | 0,907 | 79394 |
| 21. 09. 00:03 | training | 2647 | 2,31 | 0,980 | 0,893 | 0,960 | 0,904 | 79559 |
| 22. 09. 00:00 | training | 2713 | 2,29 | 0,987 | 0,870 | 0,971 | 0,881 | 80398 |
| 23. 09. 00:01 | training | 2938 | 2,26 | 1,003 | 0,901 | 0,988 | 0,897 | 81283 |
| 23. 09. 21:09 | training | 3106 | 2,23 | 1,035 | 0,923 | 1,011 | 0,902 | 82075 |
| 24. 09. 00:00 | training | 3139 | 2,22 | 1,039 | 0,927 | 1,021 | 0,912 | 82177 |
| 24. 09. 18:20 | training | 3326 | 2,32 | 1,025 | 0,921 | 1,017 | 0,921 | 82902 |
| 24. 09. 20:37 | training | 3349 | 2,33 | 1,024 | 0,894 | 1,015 | 0,878 | 82966 |
| 25. 09. 00:03 | training | 3388 | 2,33 | 1,032 | 0,893 | 1,025 | 0,902 | 83055 |
| 25. 09. 06:45 | training | 3475 | 2,32 | 1,030 | 0,879 | 1,031 | 0,904 | 83247 |
| 25. 09. 17:20 | training | 3579 | 2,33 | 1,028 | 0,889 | 1,022 | 0,900 | 83585 |
| 26. 09. 00:03 | training | 3635 | 2,34 | 1,039 | 0,878 | 1,018 | 0,887 | 83828 |
| 26. 09. 15:46 | training | 3635 | 2,34 | 1,039 | 0,869 | 1,018 | 0,872 | 83829 |
| 26. 09. 22:25 | training | 11961 | 4,84 | 1,118 | 1,373 | 1,113 | 1,344 | 124790 |
| 27. 09. 00:02 | training | 11961 | 4,84 | 1,118 | 1,490 | 1,113 | 1,517 | 124790 |
| 28. 09. 00:01 | training | 12174 | 5,03 | 1,104 | 1,267 | 1,100 | 1,233 | 125630 |
| 29. 09. 00:02 | training | 12705 | 4,94 | 1,114 | 1,355 | 1,109 | 1,281 | 127417 |
| 30. 09. 00:04 | training | 13276 | 4,86 | 1,114 | 1,076 | 1,113 | 0,972 | 129349 |
| 01. 10. 00:03 | training | 13718 | 4,81 | 1,106 | 1,066 | 1,105 | 0,966 | 131159 |
| 02. 10. 00:04 | training | 14130 | 4,79 | 1,101 | 0,941 | 1,097 | 0,963 | 133011 |
| 03. 10. 00:01 | training | 14562 | 4,77 | 1,108 | 0,910 | 1,106 | 0,967 | 135051 |
| 04. 10. 00:03 | training | 14562 | 4,77 | 1,108 | 0,941 | 1,106 | 0,971 | 135052 |
| 05. 10. 00:00 | training | 14864 | 4,90 | 1,111 | 0,926 | 1,110 | 1,012 | 136288 |
| 06. 10. 00:04 | training | 15469 | 4,81 | 1,110 | 0,935 | 1,110 | 1,019 | 138162 |
| 07. 10. 00:02 | training | 15959 | 4,72 | 1,110 | 1,009 | 1,108 | 1,018 | 140190 |
| 07. 10. 17:27 | training | 16371 | 4,65 | 1,111 | 1,659 | 1,111 | 1,162 | 141503 |
| 08. 10. 00:01 | training | 16610 | 4,62 | 1,112 | 1,008 | 1,110 | 1,023 | 142264 |
| 08. 10. 10:06 | training | 16778 | 4,59 | 1,110 | 1,008 | 1,110 | 1,018 | 142777 |
