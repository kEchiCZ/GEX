# Výjimka mega caps pro kartu breaking news — předregistrované měření (E-6.27)

ADR-0059 bod 4 podmínil výjimku pro kartu breaking news měřením. Výjimka by na kartu pustila zprávu
tier 1–2 o výsledcích mega caps (`EARNINGS`, importance ≥ 2), přestože výsledky firem nejsou nikdy
významné (ADR-0045, #1291). S výjimkou stojí i SEC (E-6.25b, ADR-0059 bod 6). Měření patří do
sub-issue #1491, Fáze 6 #1406.

**Verdikt: kritérium nesplněno.** Výjimka mega caps se nezavádí, SEC se nepoužije (ADR-0059
varianta A) a E-6.25b se ruší jako nepotřebný.

> **Přeměření 10. 10. 2026 (#1494): verdikt beze změny.** Vzorek z 9. 10. neměl minutovou fázi
> reakcí u 43 ze 44 titulků o výsledcích mega caps z týdne 28. 7. (hlavní výsledky Q2: MSFT, META,
> AAPL, AMZN). Bary pro ty dny vznikly až po výpočtu reakcí a job minutovou fázi znovu
> nepočítal. Po doplnění (`scripts/backfill_minute_reactions.py --apply`, 27 968 párů) je
> v primárním vzorku 94 titulků mega caps místo 81. Rozdíl mediánů `range_5` je −0,77 bp,
> CI [−1,73; 0,66], tedy pořád nesplněno. Tabulky jsou v oddílu „Přeměření 10. 10.“ na konci.

## Jak se měřilo

Skript `scripts/measure_megacap_earnings.py` jen čte PG. Testy čistých funkcí jsou
v `news-engine/tests/test_measure_megacap_earnings.py`. Verdikt padl v předregistrovaném běhu
`as_of` **2026-10-09T12:31:10Z**; tabulky níže jsou z opakovaného běhu **2026-10-09T12:51:35Z**,
který přidal rozpad backfillu (mezitím přibyl jeden titulek; verdikt je stejný, první běh měl
CI titulků [−1,97; 0,16] a minut [−1,52; 0,60]).

```bash
# heslo se předá z .env bez výpisu hodnoty
uv run python scripts/measure_megacap_earnings.py
```

Postup je zapsaný v těle #1491 **před spuštěním**. Znění ADR (n ≥ 30 v každé skupině, 95% bootstrap
CI rozdílu mediánů `range_5` nad nulou) upřesňuje takto:

- **Data:** titulky `category = 'EARNINGS'` × `news_reactions` NQ, okno 5 min. Vstupují jen okna
  bez kontaminace (`cont_5` false) s `range_5` a `ret_5`, bez odložených reakcí (`deferred_min`).
  Dynamika gapu po uzavírce je jiná (ADR-0043, #1311).
- **Éra:** primárně `ts_event ≥ 2026-07-28`, tedy živý sběr. Backfill Alpaca před tímto datem bral
  jen zprávy s tickerem mega caps nebo indexového ETF, případně bez tickeru
  (`scripts/alpaca_news_backfill.py:58-80`). Výsledky malých firem v něm chybí, a tím by byla
  srovnávací skupina vychýlená. Celá historie jde jen do sekundární tabulky.
- **Skupiny:** mega = `symbols` ∩ seznam 29 tickerů `MEGA_CAP` z backfillu Alpaca, ostatní = zbytek.
- **Statistika:** rozdíl mediánů `range_5` (mega − ostatní) s 95% percentilovým bootstrapem,
  10 000 opakování, seed 1491. |`ret_5`| je jen sekundární.
- **Jednotka:**
  - titulek podle znění ADR;
  - kontrola robustnosti přes unikátní minutu okna NQ. Titulky téže minuty mají (téměř) tytéž bary,
    a tedy nejsou nezávislá měření. Minuta, ve které je titulek mega cap, patří jen do skupiny mega.
    Minuta je přiblížení: okno začíná přesně v `ts_event` (`reactions.compute_reactions`), takže
    titulek v hh:mm:00 má okno o bar dřív než titulek v hh:mm:01–59. S přesným klíčem (začátek
    prvního baru okna) vychází týž verdikt: −0,44 bp, CI [−1,52; 0,64] (jednorázový výpočet
    nad týmiž daty 9. 10.).
- **Kritérium** musí platit v obou jednotkách. Při neshodě jednotek rozhoduje vlastník.

## Výsledek

### Primárně: živý sběr od 2026-07-28

| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) | rozdíl (bp) | 95% CI | kritérium |
|---|---|---|---|---|---|---|---|---|
| titulek | `range_5` | 81 | 2 877 | 7,58 | 8,48 | −0,90 | [−1,96; 0,21] | ✗ |
| titulek | \|`ret_5`\| | 81 | 2 877 | 3,35 | 3,60 | −0,25 | [−1,32; 0,73] | (sekundární) |
| minuta okna | `range_5` | 80 | 2 296 | 7,54 | 8,00 | −0,46 | [−1,53; 0,63] | ✗ |
| minuta okna | \|`ret_5`\| | 80 | 2 296 | 3,34 | 3,43 | −0,09 | [−1,18; 0,65] | (sekundární) |

Obě skupiny mají n ≥ 30, ale CI rozdílu `range_5` obsahuje nulu v obou jednotkách. Bodový odhad je
dokonce záporný: titulky o výsledcích mega caps mají v pětiminutovém okně NQ medián výchylky
o 0,5–0,9 bp **menší** než ostatní titulky `EARNINGS`.

### Sekundárně: celá historie (vychýlená srovnávací skupina)

| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) | rozdíl (bp) | 95% CI |
|---|---|---|---|---|---|---|---|
| titulek | `range_5` | 1 479 | 3 094 | 9,11 | 8,46 | +0,64 | [0,19; 1,22] |
| titulek | \|`ret_5`\| | 1 479 | 3 094 | 3,78 | 3,62 | +0,16 | [−0,22; 0,52] |
| minuta okna | `range_5` | 1 462 | 2 490 | 9,10 | 8,00 | +1,10 | [0,59; 1,69] |
| minuta okna | \|`ret_5`\| | 1 462 | 2 490 | 3,77 | 3,49 | +0,28 | [−0,03; 0,67] |

### Sekundárně: jen backfill (před 2026-07-28)

| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) | rozdíl (bp) | 95% CI |
|---|---|---|---|---|---|---|---|
| titulek | `range_5` | 1 398 | 217 | 9,25 | 8,09 | +1,16 | [−0,45; 2,12] |
| titulek | \|`ret_5`\| | 1 398 | 217 | 3,83 | 4,01 | −0,18 | [−0,71; 0,92] |
| minuta okna | `range_5` | 1 382 | 194 | 9,24 | 8,11 | +1,13 | [−0,38; 2,04] |
| minuta okna | \|`ret_5`\| | 1 382 | 194 | 3,81 | 3,97 | −0,16 | [−0,70; 0,89] |

Celá historie by kritérium splnila, ale srovnává dvě různá období:

- skupina mega pochází z 95 % z backfillu (1 398 z 1 479 titulků jsou před 28. 7. 2026);
- skupina ostatní pochází z 93 % ze živého sběru (2 877 z 3 094).

Uvnitř žádné z obou ér kritérium splněné není: v živém sběru je rozdíl záporný, v backfillu má CI
nulu uvnitř. Kladný rozdíl celé historie tak vzniká jen složením ér a míchá dvě věci, které
s výsledky mega caps nesouvisí:

- **Volatilitu období.** Medián `range_5` titulků mega caps je v backfillu 9,25 bp (n 1 398)
  a v živém sběru 7,58 bp (n 81).
- **Nesrovnatelnou skupinu v backfillu.** Ostatních titulků je v backfillu jen 217. Filtr do
  backfillu pustil jen titulky s tickerem indexového ETF nebo bez tickeru, tedy souhrny sezóny
  výsledků, ne výsledky jiných firem.

Kde jsou obě skupiny z téhož období a bez filtru (primární tabulka), rozdíl se od nuly neliší.

## Důsledky

- **Výjimka mega caps se nezavádí.** EARNINGS zůstává mimo kartu i u mega caps, takže skupinu
  „firmy“ tvoří jen `TECH` s importance ≥ 2 (ADR-0059 bod 4, revize 9. 10.).
- **SEC se nepoužije** (ADR-0059 bod 6, varianta A): 8-K mega caps zůstanou jen přes Benzingu.
  E-6.25b se ruší. Sdílená konstanta mega caps nevzniká a seznam zůstává jen filtrem backfillu Alpaca.
- **Přeměření** má smysl až po další sezóně výsledků s živým sběrem (Q3 2026 začíná v polovině
  října). Primární vzorek mega caps dnes pokrývá jen jednu sezónu (Q2 2026, od 28. 7.). Návrat
  výjimky na stůl by potřeboval nové rozhodnutí vlastníka; předregistrace platí, jak je.

## Přeměření 10. 10. 2026 po doplnění minutové fáze (#1494)

`as_of` **2026-10-10T09:17:24Z**, týž skript, seed a postup jako předregistrovaný běh.

Co se změnilo v datech: ze 44 titulků `EARNINGS` o mega caps z týdne 28. 7.–1. 8. 2026 mělo
minutovou fázi reakcí NQ jen 1. Po doplnění ji mají všechny. Do primárního vzorku jich přibylo 13,
protože filtry předregistrace zbytek vyřadí: 28 oken je kontaminovaných jinou kategorií (K1, týden
s FOMC 29. 7. a NFP 1. 8.) a 4 reakce jsou odložené.

### Primárně: živý sběr od 2026-07-28

| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) | rozdíl (bp) | 95% CI | kritérium |
|---|---|---|---|---|---|---|---|---|
| titulek | `range_5` | 94 | 2 897 | 7,70 | 8,47 | −0,77 | [−1,73; 0,66] | ✗ |
| titulek | \|`ret_5`\| | 94 | 2 897 | 3,38 | 3,59 | −0,21 | [−1,18; 0,75] | (sekundární) |
| minuta okna | `range_5` | 93 | 2 313 | 7,59 | 7,98 | −0,40 | [−1,31; 0,99] | ✗ |
| minuta okna | \|`ret_5`\| | 93 | 2 313 | 3,35 | 3,41 | −0,06 | [−1,05; 0,63] | (sekundární) |

### Sekundárně: celá historie a jen backfill

| výběr | jednotka | n mega | n ostatní | rozdíl `range_5` (bp) | 95% CI |
|---|---|---|---|---|---|
| celá historie | titulek | 1 742 | 3 139 | +0,37 | [−0,01; 0,84] |
| celá historie | minuta okna | 1 721 | 2 532 | +0,83 | [0,37; 1,33] |
| jen backfill | titulek | 1 648 | 242 | +0,72 | [−0,85; 1,56] |
| jen backfill | minuta okna | 1 628 | 219 | +0,65 | [−0,89; 1,54] |

Celá historie po doplnění kritérium nesplní v jednotce titulků (dolní mez CI −0,01). Jednotky se
neshodují, takže by o ní stejně rozhodoval vlastník. Platí ale i výhrada výše: celá historie míchá
dvě éry s různou volatilitou.

**Verdikt beze změny: nesplněno.** Výjimka mega caps se nezavádí a SEC se nepoužije.
