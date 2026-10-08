# Ngram stín (magnitudová hlava) — verdikt na živých datech (#1131)

> E-0.11 Fáze 0 (#1387) epiky #1385 · data do 8. 10. 2026 00:00 UTC · skript
> `scripts/measure_ngram_shadow_1131.py` (jen čtení PG, pevné semínko) · zadání: #740 varianta B
> (rozhodnutí 12. 9. 2026), brána #749 · vstup ADR-0053 (E-6.1, Fáze 6 #1406).

## Verdikt

- **Předregistrované kritérium není splněné a předpověď z 12. 9. platí.** Živý subset (definice
  jobu: prospektivní klasifikace ≤ 5 min, lag ingestu ≤ 1 den, nekontaminované `cont_5`) má
  16 626 řádků ES a 16 470 NQ. Hlava v něm dává lift 1,111 (ES) a 1,110 (NQ). Baseline kategorie
  ze stejného tréninku jako job, s remízami spočtenými správně, dává 1,248 a 1,218. Hlava je tedy
  pod baseline: ES −0,137 [−0,260; −0,011], NQ −0,108 [−0,243; +0,048].
  - V opraveném vzorku (bez deferred reakcí a bez neúplných seancí) je hlava bodově také pod
    baseline: ES −0,073, NQ −0,025, intervaly přes nulu.
  - Jen na datech po předregistraci (od 12. 9.) je to ES −0,184 a NQ −0,080.
- **Číslo jobu nejde použít, tvrdí falešné „prošlo“.** Poslední běh v `news_ngram_shadow_history`
  ukazuje ES 1,110 proti baseline 1,008, tedy průchod. Baseline jobu ale řadí zprávy podle průměru
  kategorie, a celá kategorie má tedy jedno skóre. Horní decil ES (1 662 řádků) zaplní
  GEOPOLITICS (326), MACRO_GROWTH (69) a MACRO_LABOR (339). Zbylých 928 míst padne dovnitř FED
  (2 821 řádků) a job je vybere v pořadí, v jakém je PG vrátí (dotaz nemá ORDER BY).
  - Výsledek: baseline jobu skáče mezi běhy (ES od 27. 9. mezi 0,91 a 1,66), zatímco lift modelu
    drží 1,10–1,12.
  - Při náhodném pořadí vychází baseline ES 1,19–1,30 (5.–95. percentil). Fyzické pořadí tabulky
    je shlukované v čase, proto rozptyl jobu vychází ještě větší.
  - Před přepočtem kontaminace 26. 9. (běh 26. 9. 15:46) job ukazoval ES 1,039 proti 0,869,
    rovněž „průchod“ ze stejné příčiny.
- **Text nenese informaci o velikosti pohybu nad rámec denní doby.**
  - Uvnitř seance hlava řadí zprávy lépe než kategorie: lift 1,122 proti 1,013, ρ +0,088
    [+0,069; +0,106].
  - Ve vrstvách seance × hodina UTC ale pořadová korelace hlavy s |ret_5| klesne na −0,009
    [−0,028; +0,008] (NQ −0,011 [−0,031; +0,009]). Všechno, co hlava umí, tedy nese hodina
    a zdroj, které model dostává jako rysy `h=` a `src=`.
  - Nulový model bez textu (průměr |ret_5| buňky zdroj × hodina z minulosti) má uvnitř seance lift
    1,346 (NQ 1,274), tedy víc než hlava. `importance` z hlavy by v praxi znamenala jen „zpráva
    v aktivní hodině = důležitá“.
- **Kategorie také neřekne, která zpráva dne pohne trhem víc.** Sdružený lift baseline kategorie
  (1,2–1,5) vzniká mezi dny: ve volatilních dnech chodí víc zpráv FED a GEOPOLITICS. Uvnitř seance
  kategorie skoro nic neřadí (lift 1,013, ρ +0,023 [−0,016; +0,071]).
- **Průřez `dir × ret` (vstup ADR-0053): směr pravidel na 5min okně živě nic nenese.**
  - Pravidla se vyjádří u 16 % zpráv (ES 2 230 ze 14 147).
  - Úspěšnost proti driftu: ES long −0,005 [−0,026; +0,017], short −0,006 [−0,043; +0,028];
    NQ long +0,001, short −0,022 [−0,054; +0,011].
  - Shoduje se s #749 (směr out-of-sample neprošel).
- **Doporučení: varianta C** (viz Varianty). `importance` z hlavy nezapínat a stín vypnout
  (`ngram_shadow_enabled` = false). Historie `source='ngram'` zůstane v PG.

## Varianty

| | výhody | nevýhody |
|---|---|---|
| A. Zapnout `importance` z hlavy | – | kritérium nesplněno; importance by kopírovala denní dobu, kterou obchodník vidí na hodinách |
| B. Prodloužit stín | průběžná kontrola trvá | chybí důvod: 15 tis. vzorků a ρ uvnitř hodiny do ±0,03, víc dat obraz nezmění; denní trénink stojí 162 s CPU (142 777 vzorků) a drží největší matice news-enginu (#1105), přibývá ~2,5 tis. řádků denně; číslo jobu by se muselo opravit (remízy), jinak dál klame |
| **C. Zahodit: nezapínat a stín vypnout** | ušetří CPU a paměť news-enginu; zmizí číslo, které tvrdí falešný průchod | průběžné číslo zmizí, skript ale jde pustit kdykoli nad uloženou historií |

Vypnutí znamená změnit default `ngram_shadow_enabled` v `news-engine/src/gexlens_news/config.py`
(produkční `.env` klíč nemá), upravit `.env.example` a nasadit news-engine v pauze Globexu.
Udělá se to samostatným issue, až vlastník rozhodne.

## Jak číst a co data neříkají

- **Vzorek** pokrývá 31 seancí od 26. 8. do 7. 10. 2026, tedy jedno období. ES a NQ se měří na
  stejných zprávách, nejsou proto nezávislé. Bootstrap losuje celé seance, protože zprávy jednoho
  dne nezávislé nejsou: pravidlo K1 nechá ve stejném 5min okně víc zpráv téže kategorie.
- **Kontaminace se 26. 9. změnila zpětně pro celou historii.** Reklasifikace v2 přepočetla
  `cont_5` pravidlem K1: okno kazí jen jiná kategorie s importance ≥ 2 (`reclass-run-20260927.txt`).
  - Živý vzorek jobu proto 26. 9. skočil z 3,6 na 12 tis. řádků a Ø |ret| z 2,3 na 4,8 bp.
  - Hlava se do 26. 9. učila nad starou definicí, od 27. 9. nad K1. Týdenní tabulka zlom nemá
    (ES 1,06–1,20).
- **Deferred reakce** (1 386 řádků ES) vznikly ze zpráv při zavřeném trhu. Sdílejí výnos uzavírky
  (víkendový gap, Ø |ret| až 57 bp za den) a jsou to tedy desítky kopií jednoho měření. Opravený
  vzorek je vynechává.
  - Neúplné seance (méně než 50 % mediánu živých řádků) vynechává také: 26. 8. (start stínu),
    21. 9. a 8. 10. (konec dat).
  - Na výsledek má vynechání jen malý vliv (varianta „vč. výpadků“).
- **Zdroje vzorku:** bluesky 66 %, alpaca 32 %, `rss_news` jen 1,5 %. Výpadek Yahoo od 24. 9.
  (#1451) vzorek prakticky nemění.
- **Walk-forward baseline kategorie** (průměry jen z dní před událostí) má sdružený lift 1,55
  (ES, opravený vzorek), ale v každém týdnu jen 0,80–1,16. Sdružené číslo skóre, které se mění
  v čase, míchá pořadí uvnitř dne s rozdíly mezi dny. Proto má report diagnostiku uvnitř seance
  a seance × hodina.
- **Co bylo předem a co se doplnilo:**
  - Kritérium rozhoduje beze změny. Je to sdružený lift proti baseline kategorie na n ≥ 2 000
    (#749, 12. 9.).
  - Oprava remíz a nulový model zdroj × hodina přibyly po průzkumu dat, ještě před prvním během
    skriptu.
  - Diagnostika uvnitř vrstev přibyla po prvním běhu.
  - Žádný doplněk verdikt neotáčí. Kritérium vychází záporně ve všech variantách a diagnostika
    vysvětluje proč.
- **Experiment #749 má stejnou slabinu.** `scripts/news_predictor_experiment.py:246` řadí baseline
  kategorie přes `np.argsort` bez ošetření remíz a denní dobu nekontroluje. Jeho závěr „velikost
  ANO“ (backfill, lift 1,44 proti 1,27) proto nedokládá, že velikost nese text. Pro E-6.1 platí
  tento živý verdikt.
- **Rozsah:** všechno platí pro 5min okno. Delší okna (15, 60 min, denní) a jiný model (např. rysy
  hustoty zpráv) tento verdikt nepokrývá. Patří do E-6.x (ADR-0053).

## Měření (výstup skriptu)

### Vzorek

- Klasifikací `ngram` s reakcí (5 min, nekontaminované): 137688; živých: 33096 (ES 16626, NQ 16470).
- Deferred (trh zavřený, výnos uzavírky): ES 1386.
- Seancí s živými řádky ES: 31, medián 511 řádků; výpadek (< 50 % mediánu): 26. 8. (171), 21. 9. (112), 8. 10. (32).
- Trénovací populace (ES, do 08. 10. 2026 00:00 UTC): 142338 řádků.

### Remízy: baseline jobu závisí na pořadí řádků

Lift baseline tak, jak ho počítá job (pořadí řádků bez ORDER BY), přes 500 náhodných pořadí, proti očekávané hodnotě (opravený výpočet):

| symbol | n | lift modelu | baseline: 5 % | medián | 95 % | očekávaná |
|---|---|---|---|---|---|---|
| ES | 16626 | 1,111 | 1,192 | 1,245 | 1,300 | 1,248 |
| NQ | 16470 | 1,110 | 1,151 | 1,217 | 1,284 | 1,218 |

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

### Historie jobu (`news_ngram_shadow_history`, subset live, poslední běh dne)

| den (UTC) | ES n | ES lift | ES baseline | NQ lift | NQ baseline | trénink |
|---|---|---|---|---|---|---|
| 12. 9. | 1807 | 0,984 | 1,165 | 1,005 | 0,913 | 76562 |
| 13. 9. | 1807 | 0,984 | 1,158 | 1,005 | 0,950 | 76562 |
| 14. 9. | 1960 | 0,950 | 1,115 | 0,954 | 0,938 | 77099 |
| 15. 9. | 2242 | 1,000 | 0,992 | 0,981 | 0,917 | 77861 |
| 16. 9. | 2344 | 1,009 | 0,861 | 1,001 | 0,925 | 78279 |
| 17. 9. | 2346 | 1,009 | 0,861 | 1,001 | 0,920 | 78371 |
| 18. 9. | 2437 | 1,010 | 0,850 | 0,965 | 0,902 | 78764 |
| 19. 9. | 2631 | 1,001 | 0,895 | 0,969 | 0,886 | 79386 |
| 20. 9. | 2631 | 1,001 | 0,917 | 0,969 | 0,907 | 79394 |
| 21. 9. | 2647 | 0,980 | 0,893 | 0,960 | 0,904 | 79559 |
| 22. 9. | 2713 | 0,987 | 0,870 | 0,971 | 0,881 | 80398 |
| 23. 9. | 3106 | 1,035 | 0,923 | 1,011 | 0,902 | 82075 |
| 24. 9. | 3349 | 1,024 | 0,894 | 1,015 | 0,878 | 82966 |
| 25. 9. | 3579 | 1,028 | 0,889 | 1,022 | 0,900 | 83585 |
| 26. 9. | 11961 | 1,118 | 1,373 | 1,113 | 1,344 | 124790 |
| 27. 9. | 11961 | 1,118 | 1,490 | 1,113 | 1,517 | 124790 |
| 28. 9. | 12174 | 1,104 | 1,267 | 1,100 | 1,233 | 125630 |
| 29. 9. | 12705 | 1,114 | 1,355 | 1,109 | 1,281 | 127417 |
| 30. 9. | 13276 | 1,114 | 1,076 | 1,113 | 0,972 | 129349 |
| 1. 10. | 13718 | 1,106 | 1,066 | 1,105 | 0,966 | 131159 |
| 2. 10. | 14130 | 1,101 | 0,941 | 1,097 | 0,963 | 133011 |
| 3. 10. | 14562 | 1,108 | 0,910 | 1,106 | 0,967 | 135051 |
| 4. 10. | 14562 | 1,108 | 0,941 | 1,106 | 0,971 | 135052 |
| 5. 10. | 14864 | 1,111 | 0,926 | 1,110 | 1,012 | 136288 |
| 6. 10. | 15469 | 1,110 | 0,935 | 1,110 | 1,019 | 138162 |
| 7. 10. | 16371 | 1,111 | 1,659 | 1,111 | 1,162 | 141503 |
