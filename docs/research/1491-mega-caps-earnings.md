# Výjimka mega caps pro kartu breaking news — předregistrované měření (E-6.27)

ADR-0059 bod 4 podmínil výjimku pro kartu breaking news měřením. Výjimka by na kartu pustila zprávu
tier 1–2 o výsledcích mega caps (`EARNINGS`, importance ≥ 2), přestože výsledky firem nejsou nikdy
významné (ADR-0045, #1291). S výjimkou stojí i SEC (E-6.25b, ADR-0059 bod 6). Měření patří do
sub-issue #1491, Fáze 6 #1406.

**Verdikt: kritérium nesplněno.** Výjimka mega caps se nezavádí, SEC se nepoužije (ADR-0059
varianta A) a E-6.25b se ruší jako nepotřebný.

## Jak se měřilo

Skript `scripts/measure_megacap_earnings.py` jen čte PG. Testy čistých funkcí jsou
v `news-engine/tests/test_measure_megacap_earnings.py`. Snímek `as_of` **2026-10-09T12:31:10Z**.

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
  - kontrola robustnosti přes unikátní minutu okna NQ. Titulky téže minuty mají tytéž bary, a tedy
    nejsou nezávislá měření. Minuta, ve které je titulek mega cap, patří jen do skupiny mega.
- **Kritérium** musí platit v obou jednotkách. Při neshodě jednotek rozhoduje vlastník.

## Výsledek

### Primárně: živý sběr od 2026-07-28

| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) | rozdíl (bp) | 95% CI | kritérium |
|---|---|---|---|---|---|---|---|---|
| titulek | `range_5` | 81 | 2 876 | 7,58 | 8,48 | −0,90 | [−1,97; 0,16] | ✗ |
| titulek | \|`ret_5`\| | 81 | 2 876 | 3,35 | 3,60 | −0,25 | [−1,33; 0,68] | (sekundární) |
| minuta okna | `range_5` | 80 | 2 295 | 7,54 | 8,00 | −0,46 | [−1,52; 0,60] | ✗ |
| minuta okna | \|`ret_5`\| | 80 | 2 295 | 3,34 | 3,43 | −0,09 | [−1,15; 0,67] | (sekundární) |

Obě skupiny mají n ≥ 30, ale CI rozdílu `range_5` obsahuje nulu v obou jednotkách. Bodový odhad je
dokonce záporný: titulky o výsledcích mega caps mají v pětiminutovém okně NQ medián výchylky
o 0,5–0,9 bp **menší** než ostatní titulky `EARNINGS`.

### Sekundárně: celá historie (vychýlená srovnávací skupina)

| jednotka | metrika | n mega | n ostatní | medián mega (bp) | medián ostatní (bp) | rozdíl (bp) | 95% CI |
|---|---|---|---|---|---|---|---|
| titulek | `range_5` | 1 479 | 3 093 | 9,11 | 8,46 | +0,64 | [0,19; 1,22] |
| titulek | \|`ret_5`\| | 1 479 | 3 093 | 3,78 | 3,62 | +0,16 | [−0,21; 0,52] |
| minuta okna | `range_5` | 1 462 | 2 489 | 9,10 | 8,00 | +1,10 | [0,58; 1,69] |
| minuta okna | \|`ret_5`\| | 1 462 | 2 489 | 3,77 | 3,49 | +0,28 | [−0,05; 0,67] |

Celá historie by kritérium splnila, ale srovnává dvě různá období:

- skupina mega pochází z 95 % z backfillu (1 398 z 1 479 titulků jsou před 28. 7. 2026);
- skupina ostatní pochází z 93 % ze živého sběru (2 876 z 3 093).

Rozdíl celé historie tak míchá dvě věci, které s výsledky mega caps nesouvisí:

- **Volatilitu období.** Medián `range_5` titulků mega caps je v backfillu 9,25 bp (n 1 398)
  a v živém sběru 7,58 bp (n 81).
- **Nesrovnatelnou skupinu v backfillu.** Uvnitř backfillu mají ostatní titulky medián 8,09 bp,
  ale je jich jen 217. Filtr do backfillu pustil jen titulky s tickerem indexového ETF nebo bez
  tickeru, tedy souhrny sezóny výsledků, ne výsledky jiných firem.

Kde jsou obě skupiny z téhož období a bez filtru (primární tabulka), rozdíl se od nuly neliší.

## Důsledky

- **Výjimka mega caps se nezavádí.** EARNINGS zůstává mimo kartu i u mega caps, takže skupinu
  „firmy“ tvoří jen `TECH` s importance ≥ 2 (ADR-0059 bod 4, revize 9. 10.).
- **SEC se nepoužije** (ADR-0059 bod 6, varianta A): 8-K mega caps zůstanou jen přes Benzingu.
  E-6.25b se ruší. Sdílená konstanta mega caps nevzniká a seznam zůstává jen filtrem backfillu Alpaca.
- **Přeměření** má smysl až po další sezóně výsledků s živým sběrem (Q3 2026 začíná v polovině
  října). Primární vzorek mega caps dnes pokrývá jen jednu sezónu (Q2 2026, od 28. 7.). Návrat
  výjimky na stůl by potřeboval nové rozhodnutí vlastníka; předregistrace platí, jak je.
