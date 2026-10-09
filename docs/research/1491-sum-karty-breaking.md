# Šum karty breaking news — potvrzené × nepotvrzené zprávy (E-6.27)

Vlastník 9. 10. 2026 rozhodl, že významná zpráva s efektivním tierem 3 (článek) jde na kartu
**hned** se štítkem „článek, zatím nepotvrzeno“. Štítek zmizí s první viditelnou kopií tier 1–2
(Rozhodnuto v #1385, revize ADR-0059 bod 4). Report měří, kolik takových zpráv karta ukáže.
Při velkém šumu předkládá varianty zpřísnění. Patří do sub-issue #1491, Fáze 6 #1406.

**Shrnutí:** za 30 dní šlo na kartu ~81 zpráv za obchodní den a polovina z nich byla nepotvrzená
(~40 za obchodní den). Největší zdroj šumu je Finnhub: 40 % nepotvrzených zpráv s mediánem zpoždění
11 h. Kolik nepotvrzených zpráv později potvrdí tier 1–2, zatím změřit nejde, protože kopie se
zaznamenávají teprve od 9. 10. 12:01 UTC. Přeměření je připomínka #1492.

## Jak se měřilo

Skript `scripts/measure_breaking_noise.py` jen čte PG. Testy čistých funkcí jsou
v `news-engine/tests/test_measure_breaking_noise.py`. Snímek `as_of` **2026-10-09T12:40:00Z**,
okno 30 dní.

```bash
# heslo se předá z .env bez výpisu hodnoty
uv run python scripts/measure_breaking_noise.py --as-of 2026-10-09T12:40:00+00:00
```

Výběr a potvrzení počítá tatáž funkce jako karta (`gexlens_news.breaking`) nad efektivním tierem
v čase *t* (`compute/news_tier.effective_tier`):

- **vstup na kartu** je první okamžik, kdy je zpráva breaking, tedy příjem prvního doručení nebo
  kopie, která efektivní tier posunula do 1–3;
- **potvrzení** je první okamžik od vstupu s efektivním tierem 1–2;
- **náskok karty** je potvrzení − vstup u zpráv, které vstoupily nepotvrzené. Tolik minut by
  zpráva na kartě chyběla, kdyby se čekalo na potvrzení (zamítnutá varianta A z 9. 10.).

Omezení:

- Kopie (`news_event_sources`) se zaznamenávají až od nasazení E-6.24b (2026-10-09 12:01:22 UTC,
  #1489). Ve starších dnech proto nic potvrzené později není a počet nepotvrzených je **horní mez**.
- Importance a kategorie jsou dnešní, stejně jako na kartě. Reklasifikace se promítne i zpětně.
- Den je UTC den vstupu; 9. 9. a 9. 10. jsou neúplné.

## Výsledek

### Denně

| den | breaking | potvrzené při vstupu | nepotvrzené | z nich později potvrzené | nepotvrzené `is_key` |
|---|---|---|---|---|---|
| 2026-09-09 st | 50 | 13 | 37 (74 %) | 0 | 16 |
| 2026-09-10 čt | 117 | 43 | 74 (63 %) | 0 | 34 |
| 2026-09-11 pá | 102 | 47 | 55 (54 %) | 0 | 21 |
| 2026-09-12 so | 32 | 4 | 28 (88 %) | 0 | 19 |
| 2026-09-13 ne | 35 | 13 | 22 (63 %) | 0 | 11 |
| 2026-09-14 po | 72 | 32 | 40 (56 %) | 0 | 17 |
| 2026-09-15 út | 88 | 35 | 53 (60 %) | 0 | 15 |
| 2026-09-16 st | 152 | 94 | 58 (38 %) | 0 | 18 |
| 2026-09-17 čt | 106 | 51 | 55 (52 %) | 0 | 16 |
| 2026-09-18 pá | 82 | 37 | 45 (55 %) | 0 | 10 |
| 2026-09-19 so | 21 | 6 | 15 (71 %) | 0 | 7 |
| 2026-09-20 ne | 13 | 5 | 8 (62 %) | 0 | 2 |
| 2026-09-21 po | 53 | 31 | 22 (42 %) | 0 | 4 |
| 2026-09-22 út | 92 | 46 | 46 (50 %) | 0 | 20 |
| 2026-09-23 st | 80 | 34 | 46 (58 %) | 0 | 13 |
| 2026-09-24 čt | 100 | 48 | 52 (52 %) | 0 | 32 |
| 2026-09-25 pá | 74 | 37 | 37 (50 %) | 0 | 16 |
| 2026-09-26 so | 21 | 10 | 11 (52 %) | 0 | 8 |
| 2026-09-27 ne | 23 | 8 | 15 (65 %) | 0 | 9 |
| 2026-09-28 po | 68 | 26 | 42 (62 %) | 0 | 18 |
| 2026-09-29 út | 95 | 72 | 23 (24 %) | 0 | 12 |
| 2026-09-30 st | 87 | 42 | 45 (52 %) | 0 | 11 |
| 2026-10-01 čt | 103 | 63 | 40 (39 %) | 0 | 17 |
| 2026-10-02 pá | 82 | 50 | 32 (39 %) | 0 | 14 |
| 2026-10-03 so | 5 | 3 | 2 (40 %) | 0 | 1 |
| 2026-10-04 ne | 15 | 11 | 4 (27 %) | 0 | 2 |
| 2026-10-05 po | 38 | 22 | 16 (42 %) | 0 | 2 |
| 2026-10-06 út | 54 | 29 | 25 (46 %) | 0 | 13 |
| 2026-10-07 st | 69 | 38 | 31 (45 %) | 0 | 11 |
| 2026-10-08 čt | 78 | 39 | 39 (50 %) | 0 | 14 |
| 2026-10-09 pá | 19 | 12 | 7 (37 %) | 0 | 3 |

Celkem 2 026 zpráv na kartě, z toho 1 001 potvrzených při vstupu a 1 025 nepotvrzených.
Mimo víkendy je to ~81 zpráv za obchodní den.

### Od záznamu kopií (9. 10. 12:01–12:40 UTC)

Za 39 minut nevstoupila na kartu žádná zpráva, takže podíl pozdějších potvrzení ani náskok karty
zatím změřit nejde. Přeměření po 5 obchodních dnech je připomínka #1492.

Předpoklad, který přeměření ověří: **potvrzení kopií bude vzácné.** Kopie je jen **táž zpráva**,
tedy týž titulek nebo Jaccard ≥ 0,9 (ADR-0059 bod 3). Článek CNBC nebo Reuters o téže události má
jiný titulek než headline Benzinga Newsdesku, a proto ho kopie nepotvrdí. To umí až shluk
(E-6.6, E-6.17). Štítek „nepotvrzeno“ tak u většiny zpráv tier 3 nejspíš zůstane, i když událost
tier 2 mezitím přinesl.

### Zdroje nepotvrzených zpráv

| zdroj | nepotvrzené | medián zpoždění `ts_ingested − ts_event` (min) | zpoždění nad 60 min |
|---|---|---|---|
| `finnhub` | 405 | 659,5 | 366 (90 %) |
| `rss_news` | 364 | 4,6 | 93 (26 %) |
| `alpaca` (ostatní autoři) | 183 | 0,0 | 2 (1 %) |
| `ibkr_brfg` | 73 | 3,9 | 14 (19 %) |

Skupiny nepotvrzených zpráv: geopolitika 702, centrální banky 151, ostatní 88, makro data 77,
firmy 7.

Finnhub general nese Reuters a Bloomberg se zpožděním v řádu hodin (audit E-6.21, #1473). Na kartu
breaking news tak přináší z 90 % zprávy starší než hodina, které mají štítek „nepotvrzeno“
a ve skutečnosti už breaking nejsou.

### Varianty zpřísnění (nepotvrzené při vstupu, celé okno)

| varianta | celkem | průměr na obchodní den | víkend a svátky |
|---|---|---|---|
| A — všechny (rozhodnutí 9. 10.) | 1 025 | 40,0 | 105 |
| B — jen `is_key` (importance 3) | 406 | 15,1 | 59 |
| C — jen čerstvé (zpoždění příjmu ≤ 60 min) | 550 | 23,0 | 21 |
| B + C | 164 | 6,8 | 7 |

## K rozhodnutí vlastníka

Kritéria podle AGENTS.md: rychlost, výkon, relevance dat pro obchodní rozhodnutí.

- **A — ponechat** (platí od 9. 10.):
  - \+ nic významného neunikne;
  - − polovina karty nese štítek „nepotvrzeno“ (~40 zpráv za obchodní den) a 90 % zpráv z Finnhubu
    je starších než hodina.
- **B — tier 3 jen se zásadní zprávou (`is_key`)**:
  - \+ jednoduché, využívá stávající odznak;
  - − zahodí i čerstvé články s importance 2 (téma Fedu, cla) a staré zprávy z Finnhubu s
    importance 3 ponechá.
- **C — tier 3 jen čerstvý (zpoždění příjmu za publikací ≤ 60 min)**:
  - \+ míří přímo na zjištěný zdroj šumu: zahodí 366 ze 405 zpráv z Finnhubu, a přitom ponechá
    rychlé články CNBC a Benzingy;
  - − nové kritérium karty, zatím mimo ADR (doplnila by ho revize ADR-0059 bod 4);
  - − nepomůže proti počtu zpráv v hlavních hodinách.
- **B + C:** ~7 nepotvrzených za obchodní den; nejtišší karta, ale nejvíc vynechaných zpráv.

**Doporučení: C.** Šum tvoří převážně staré zprávy, ne slabé zprávy. Kritérium „čerstvá“ míří
na relevanci dat (point-in-time) a nepotřebuje slovník ani kritérium délky titulku. Hranice
60 min odpovídá tabulce zdrojů: tier 2 i rychlé články mají medián zpoždění do 5 min. Než se
rozhodne o B, má smysl počkat na přeměření (#1492), protože potvrzení kopií může počet
nepotvrzených změnit. Do rozhodnutí platí A.
