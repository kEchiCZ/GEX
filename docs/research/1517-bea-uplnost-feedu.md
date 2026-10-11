# Úplnost feedu BEA `www.bea.gov/news/rss` proti kalendáři (#1517, E-6.25d)

- **Datum:** 11. 10. 2026
- **Otázka:** má feed položku ke každému releasu BEA z kalendáře FF? Otevřený bod z E-6.22
  (`docs/research/1474-kandidati-zdroju-zprav.md`, oddíl 2.2): snímek měl mezeru 21. 7. → 24. 9.
- **Data:** snímek feedu 11. 10. 2026 (10 položek, `pubDate` 5. 2. – 6. 10. 2026) proti řádkům
  kalendáře FF v produkční DB (`news_events`, `source = 'forexfactory'`, `kind = 'scheduled'`).
  Goods Trade Balance se nesrovnává — je to Census, ne BEA.

## Výsledek

| řada (titulek FF) | releasů v kalendáři 5. 2.–6. 10. | ve feedu | položka feedu | čas položky |
|---|---|---|---|---|
| GDP (`USD Advance/Prelim/Final GDP q/q`) | 9 | 1 | GDP (Third Estimate) …, 2nd Quarter 2026 | 30. 9. 8:30 EDT = FF |
| PCE (`USD Core PCE Price Index m/m`) | 9 | 1 | Personal Income and Outlays, August 2026 | 30. 9. 8:30 EDT = FF |
| Obchodní bilance (`USD Trade Balance`) | 9 | 1 | U.S. International Trade in Goods and Services, August 2026 | 6. 10. 8:30 EDT = FF |
| Běžný účet (`USD Current Account`) | 3 | 1 | U.S. International Transactions and Investment Position, 2nd Quarter 2026 | 24. 9. 8:30 EDT = FF |

Zbylých 6 položek jsou roční vydání bez vlivu na trh: přímé investice, aktivity nadnárodních
firem, outdoor, PCE po státech a GDP po okresech. Mají data 5. 2. – 21. 7.

## Verdikt

- **Jako historie je feed neúplný.** Chybí 8 z 9 releasů GDP i PCE.
- **Vzor je jednoznačný:** feed drží jen **nejnovější vydání každé řady**, ne posledních N
  releasů. Každá řada má ve feedu právě 1 položku, vždy nejnovější. Ke každé chybějící položce
  je ve feedu novější vydání téže řady.
  - Proto ve feedu je roční vydání z února, ale ne Prelim GDP z 26. 8.
  - Mezera z E-6.22 má stejnou příčinu.
- **Živý proud** (objeví se každé nové vydání v 8:30?) jeden snímek nedokáže. Wayback Machine
  feed nearchivuje, dotaz CDX vrátil 0 snímků. Konzistentní je, že všechna 4 nejnovější vydání
  trhových řad ve feedu jsou a čas položky přesně odpovídá kalendáři.
- **Rozhodnutí vlastníka 11. 10. (varianta A):** adaptér hned, úplnost živého proudu se změří
  na nejbližších releasech: GDP Advance Q3 29. 10. 8:30 ET, Personal Income and Outlays 4. 11.,
  obchod 20. 11. (`www.bea.gov/news/schedule`). Kdyby feed vydání nezveřejnil včas, zdroj se
  vypne v registru `news_sources`. Release i hodnoty nese i bez BEA kalendář FF.

## Důsledky pro adaptér

- První stažení zapíše 10 položek: 4 trhové a 6 ročních. Historie starších releasů z feedu
  nevznikne.
- `pubDate` je čas releasu, `ts_event` je čas položky (`raw.ts_source = item`, ADR-0059 bod 5).
- Kalendář FF hodnotí obchodní bilanci a běžný účet jako Low impact (importance 1), GDP ve
  všech odhadech a Core PCE jako High (3).
