# Tvrdá data pro epiku #1385

Změřený stav dat, ze kterého vycházejí rozhodnutí Fází 4–6 epiky #1385 (validace úrovní E-5.8
a E-5.9, kontext sentimentu E-6.1, opravy dat E-0.2). Každý bod nese dotaz, `as_of` a výsledek.
Data se neextrapolují: neúplný měsíc nebo cenzurovaná řada je označená.

- **Oddíl I** (E-0.4a, #1392): read-only SQL nad produkční PG a čtení partic `data/`.
- **Oddíl II** (E-0.4b, #1394): provoz v RTH, doplní se samostatně.

## Oddíl I — PG a partice (E-0.4a)

**Měřeno:** 7. 10. 2026, `as_of` 2026-10-07T20:51:28Z (po settle seance 7. 10.), produkce na
`9d29be1`. **Svolení vlastníka** k read-only SQL: 7. 10. 2026 (chat session).

**Jak:** `scripts/measure_hard_data_1381.py`. Všechny SQL dotazy běží v jedné transakci
`SET TRANSACTION READ ONLY`, ukončené rollbackem. Partice se jen čtou. Měsíc a seance jsou
obchodní den (`settle.trading_session_date`, `settle.is_trading_session`). Zprávy se sčítají
po hodinových koších UTC, které se na obchodní den mapují přesně, protože Globex otevírá
v celou hodinu CT. Konfigurace je čtená z běžícího enginu a z `/status`, ne z `.env`.

```bash
uv run python scripts/measure_hard_data_1381.py --db "$GEXLENS_HOST_DATABASE_URL" \
    --data data --reclass-at 2026-09-26T22:14:46+00:00
```

Doplňkové dotazy přes `psql` v kontejneru `postgres`, každý v `BEGIN TRANSACTION READ ONLY`:

```sql
-- bod 5: runtime hodnoty v PG
SELECT key, value FROM settings WHERE key ILIKE '%disk%' OR key ILIKE '%cumdelta%';
-- bod 2: rss_news po feedech (#1451)
SELECT raw->>'feed', count(*) FILTER (WHERE ts_event >= '2026-09-10' AND ts_event < '2026-09-24'),
       count(*) FILTER (WHERE ts_event >= '2026-09-24'), max(ts_event)
FROM news_events WHERE source = 'rss_news' AND ts_event >= '2026-09-10' GROUP BY 1;
-- bod 4: zprávy v okně −20/+2 min kolem skoků 2026
SELECT t.ts, e.ts_event, e.source, e.importance, e.title FROM (VALUES (timestamptz '2026-03-23 11:05Z'),
  ('2026-03-26 20:11Z'), ('2026-04-07 22:32Z'), ('2026-06-26 19:59Z'), ('2026-06-09 16:38Z')) AS t(ts)
JOIN news_events e ON e.ts_event BETWEEN t.ts - interval '20 minutes' AND t.ts + interval '2 minutes';
```

Říjen 2026 je ve všech tabulkách **neúplný** (5 seancí, 1.–7. 10.).

### 1. Gamma režim seancí (ES, NQ)

**Zdroj:** `em_respect.negative_gamma_share`, tedy podíl minut seance do settle se spotem pod
měřeným flipem (`emrespect.negative_share`). Flip pochází z levels řetězu, který engine ten den
sledoval. Seance je převážně negativní při podílu ≥ 0,5.

| symbol | měsíc | seance | bez levels | neg | z toho podíl ≥ 0,9 | poz | z toho podíl ≤ 0,1 |
|---|---|---|---|---|---|---|---|
| ES | 2026-07 | 10 | 1 | 5 | 3 | 4 | 1 |
| ES | 2026-08 | 21 | 0 | 10 | 2 | 11 | 8 |
| ES | 2026-09 | 21 | 2 | 10 | 6 | 9 | 6 |
| ES | 2026-10 (neúplný) | 5 | 0 | 1 | 0 | 4 | 1 |
| NQ | 2026-07 | 10 | 3 | 2 | 1 | 5 | 3 |
| NQ | 2026-08 | 21 | 2 | 9 | 5 | 10 | 6 |
| NQ | 2026-09 | 21 | 2 | 6 | 4 | 13 | 6 |
| NQ | 2026-10 (neúplný) | 5 | 0 | 2 | 1 | 3 | 2 |

- **Celkem:** ES 57 seancí, z toho 26 negativních, 28 pozitivních a 3 bez levels. NQ 57 seancí,
  z toho 19 negativních, 31 pozitivních a 7 bez levels. Žádný řádek mimo obchodní den.
- **Hloubka:** řada začíná 20. 7. 2026, protože levels existují od prvního živého dne
  (19. 7. 2026, viz bod 6). Gamma režim pro období před červencem 2026 **neexistuje** a z barů
  ho dopočítat nejde.
- **Tempo (pozorované, ne odhad):** srpen a září měly 10 negativních seancí ES ze 21, u NQ 9
  a 6 ze 21. Kolik seancí potřebuje validace úrovní, stanoví ADR-0052 (E-5.9).
- **Bez levels** u NQ (7 seancí) znamená, že seance neměla žádnou společnou minutu spotu
  a měřeného flipu. Příčinu tento úkol nezkoumá.

### 2. Zprávy podle zdroje × měsíce (`news_events`)

**Dotaz:** `SELECT source, date_trunc('hour', ts_event), count(*) FROM news_events GROUP BY 1, 2`,
poté obchodní den a měsíc v Pythonu. Celkem 223 799 zpráv. Řada se dělí do tří ér:

| Éra | Měsíce | Zdroje (měsíční objem) |
|---|---|---|
| A — jen kalendář | 2023-07 → 2024-06 | `forexfactory` 367–451 (2023-07 od 30. 7.: 21), `rss_news` do 14 |
| B — backfill zpráv | 2024-07 → 2026-06 | `alpaca` 2 532–3 863 (2024-07 od 28. 7.: 558), `forexfactory` 378–460, `rss_news` do 39, `fed_rss` od 2026-01 do 6 |
| C — živý sběr z více zdrojů | 2026-07 → | viz tabulka níže |

| měsíc | alpaca | bluesky | fed_rss | finnhub | forexfactory | ibkr_brfg | ibkr_djnl | reddit_rss | rss_news | celkem |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07 | 3 721 | 2 | 12 | 359 | 403 | 95 | 5 | – | 4 546 | 9 143 |
| 2026-08 | 7 961 | 3 106 | 4 | 2 078 | 362 | 589 | 18 | 169 | 23 756 | 38 043 |
| 2026-09 | 15 541 | 34 967 | 13 | 2 283 | 457 | 414 | 19 | 1 470 | 16 988 | 72 152 |
| 2026-10 (neúplný) | 4 523 | 9 251 | 5 | 298 | 144 | 44 | 2 | 339 | 540 | 15 146 |

- **Objem v éře C** vzrostl za tři měsíce osminásobně: z 9 tisíc na 72 tisíc zpráv měsíčně.
  Bluesky v září tvoří 48 % zpráv.
- **Výpadek feedu Yahoo (#1451):** `rss_news` klesl od 24. 9. z ~600–1 000 na ~80–120 zpráv
  denně. Feed `finance.yahoo.com/news/rssindex` vrací od 23. 9. 21:40 UTC HTTP 404: 9 969 zpráv
  za 10.–23. 9., od 24. 9. žádná. CNBC a MarketWatch běží beze změny. Korpus má tedy další zlom
  na 24. 9.
- **Mimo éry:** `alpaca` má před érou B 32 sporadických zpráv (2013–2024) a `bluesky` 1 (2018).
  Jde o staré články, ne o souvislou řadu.
- **Důsledek pro E-6.1:** počty zpráv za den nejsou mezi érami srovnatelné. Normalizace napříč
  zdroji a érami (ADR-0037, ADR-0053) musí s tímto skokem počítat.

### 3. Sentiment: pokrytí `sentiment_daily` a přepočet po klasifikaci v2

| symbol | od | do | dní | se σ | nejstarší `update_time` | nejnovější `update_time` |
|---|---|---|---|---|---|---|
| ES | 2023-07-30 | 2026-10-07 | 1 166 | 1 136 | 29. 7. 2026 13:32 UTC | 7. 10. 2026 20:44 UTC |
| NQ | 2023-07-30 | 2026-10-07 | 1 166 | 1 136 | 12. 8. 2026 21:51 UTC | 7. 10. 2026 20:44 UTC |

Řada má kalendářní dny včetně víkendů. Prvních 30 dní (do 28. 8. 2023) nemá σ, od 29. 8. 2023
ji má každý den.

**Reklasifikace v2 (ADR-0045):** ostrý běh `reclassify_news_rules.py` proběhl s RUN_AT
2026-09-26T22:14:46Z (`data/reports/reclass-run-20260927.txt`). V `news_classifications` nese
tento `created_at` 52 594 řádků.

| verze | zdroj | řádků | první | poslední |
|---|---|---|---|---|
| 1 | llm | 669 | 29. 7. 2026 | 13. 8. 2026 |
| 1 | rule | 223 102 | 28. 7. 2026 | 7. 10. 2026 (živě) |
| 2 | llm | 14 747 | 29. 7. 2026 | 13. 8. 2026 |
| 2 | ngram | 100 347 | 26. 8. 2026 | 7. 10. 2026 (živě) |
| 2 | rule | 28 820 | 3. 8. 2026 | 26. 9. 2026 (RUN_AT) |
| 3 | llm | 331 | 29. 7. 2026 | 2. 8. 2026 |
| 3 | rule | 23 776 | 26. 9. 2026 (RUN_AT) | 26. 9. 2026 (RUN_AT) |

**Retro přepočet SentIndexu po reklasifikaci:**

| symbol | dny před RUN_AT přepočtené po něm | rozsah | první přepočet | dny před RUN_AT nepřepočtené |
|---|---|---|---|---|
| ES | 60 | 28. 7. – 25. 9. 2026 | 26. 9. 22:34 UTC | 1 094 |
| NQ | 60 | 28. 7. – 25. 9. 2026 | 26. 9. 22:38 UTC | 1 094 |

- **`recompute-sentindex` po v2 proběhl jen od 28. 7. 2026**, tedy 20 minut po reklasifikaci se
  stejným `--from` jako po #1150 (ADR-0036). Dny 30. 7. 2023 až 27. 7. 2026 (1 094 dní na
  symbol) mají SentIndex z backfillu z 29. 7. a 12. 8. 2026, tedy z klasifikace před v2.
  **Na 28. 7. 2026 je tak zlom klasifikace**, který E-6.1 (vícedenní řada, kauzální percentily)
  musí buď přepočíst, nebo držet jako éru.
- **Číslo verze v `news_classifications` neoznačuje verzi pravidel**, ale pořadí záznamu u eventu
  (max+1). Živá pravidlová klasifikace s pravidly v2 se zapisuje jako `version = 1`
  (223 tisíc řádků až do 7. 10.). Verzi pravidel lze odvodit jen z času (`created_at` ≥ nasazení
  #1293). Pro MCP (E-3.4, ADR-0049), který má nést verzi klasifikace, to znamená, že ji tento
  sloupec nedá.

### 4. Bary podle `source` × měsíce a skoky

**Zdroj:** `data/derived/{sym}/bars/*.parquet`, sloupce `ts_min`, `close` a `source`. Starší
partice sloupec `source` nemají, což podle `bar_source_rank` znamená živou cestu (NULL). Minuta
se v archivu neopakuje v žádné partici.

| Období (ES i NQ) | NULL | `ibkr` | `ibkr_hist` | `tasty_candle` |
|---|---|---|---|---|
| 2024-07 → 2026-07 | 27–32 tisíc minut/měsíc (2024-07 od 28. 7.: 4 140) | – | – | – |
| 2026-08 | 15 300 | 13 680 | – | – |
| 2026-09 | – | ES 27 164 / NQ 27 055 | ES 2 513 / NQ 2 577 | ES 446 / NQ 488 |
| 2026-10 (neúplný) | – | ES 6 611 / NQ 6 609 | ES 278 / NQ 280 | – |

- **Potvrzen nález 1 z #1349 s upřesněním:** hluboký archiv od 28. 7. 2024 není značený `ibkr`,
  ale nemá sloupec `source` vůbec (NULL). Sémantika je stejná, archiv se tváří jako změřená živá
  minuta. Od živé minuty před zavedením `source` (19. 7. až polovina 8. 2026) ho odliší jen datum.
- **Ad-hoc pohledy** (SPX, SPY, QQQ, KO, SOFI, CL) mají bary jen pro jednotlivé dny, převážně
  `tasty_candle`. RTY, MES a MNQ mají jen pár dní z července 2026.

**Seance s doplněnými bary** (ne živá cesta, tj. jiné než NULL a `ibkr`):

| seance | ES `ibkr_hist` | ES `tasty_candle` | NQ `ibkr_hist` | NQ `tasty_candle` |
|---|---|---|---|---|
| 9. 9. | 159 | – | 165 | – |
| 10. 9. | 153 | – | 155 | – |
| 11. 9. | 70 | 47 | 118 | 113 |
| 14. 9. | 320 | – | 327 | – |
| 15. 9. | 615 | – | 614 | – |
| 16. 9. | – | 61 | – | 61 |
| **18. 9.** | – | **338** | – | **314** |
| 21. 9. | 1 | – | – | – |
| 23. 9. | 14 | – | 17 | – |
| 24. 9. | 29 | – | 29 | – |
| 25. 9. | 1 | – | 1 | – |
| 28. 9. | 1 017 | – | 1 017 | – |
| 29. 9. | 134 | – | 134 | – |
| 1. 10. | 40 | – | 40 | – |
| 2. 10. | 221 | – | 221 | – |
| 6. 10. | 14 | – | 14 | – |
| 7. 10. | 3 | – | 5 | – |

- **Rozpor s #1349 (nález 3):** issue uvádí zbylé `tasty_candle` jen 11. 9. (ES 47, NQ 113)
  a 16. 9. (ES 61 a NQ 61), což sedí. Rekonstruované svíčky ale nese i **18. 9.** (ES 338,
  NQ 314 minut), což je den kvartální expirace. Vstup pro E-0.2 (#1390).

**Skoky ≥ 60 bp mezi po sobě jdoucími bary téže seance:** ES 58 (2024: 3, 2025: 50, 2026: 5)
a NQ 70 (7, 57, 6), všechny v NULL archivu a vždy s mezerou 1 minuty. Skoky z let 2024–2025
padají na časy známých událostí: CPI ve 12:30 a 13:30 UTC, FOMC 18. 9. 2024 v 18:00 UTC, propad
5. 8. 2024 a cla 2.–10. 4. 2025, kam patří většina skoků roku 2025. Jednotlivě je se zprávami
nepárujeme. Skoky z roku 2026 jsme porovnali se zprávami v okně −20/+2 min od skoku:

| skok | zpráva v okně −20/+2 min |
|---|---|
| ES/NQ 23. 3. 2026 11:05–11:06 UTC (+182/+170 bp) | `alpaca` 10:58: titulek ke geopolitice (Írán), importance 1 |
| ES/NQ 26. 3. 2026 20:11 UTC (+94/+84 bp) | `forexfactory` 20:00 „USD President Trump Speaks“ |
| ES/NQ 7. 4. 2026 22:32–22:33 UTC (+65/−63/+83 bp) | **žádná** |
| ES 26. 6. 2026 19:59 UTC (−75 bp) | **žádná** relevantní (článek 20:00 bez vazby) |
| NQ 9. 6. 2026 16:38 UTC (−64 bp) | **žádná** |

Skoky bez dohledané zprávy jsou kandidáti pro sken skoků v E-0.2.

**Skoky ≥ 60 bp přes hranici seance**, kdy se první bar seance porovnává s posledním barem
předchozí: archiv **není spojitý přes roll**. U každé kvartální expirace je skok o velikosti
basisu na otevření seance expirace nebo 1–2 seance před ní, s mezerou 61 minut, tedy přes denní
pauzu:

| expirace | ES | NQ |
|---|---|---|
| 20. 9. 2024 | +107 bp (19. 9. 22:00 UTC) | +121 bp |
| 20. 12. 2024 | +119 bp (17. 12. 23:00 UTC) | +126 bp |
| 21. 3. 2025 | +87 bp (20. 3. 22:00 UTC) | +104 bp |
| 20. 6. 2025 | +87 bp (18. 6. 22:00 UTC) | +103 bp |
| 19. 9. 2025 | +91 bp (18. 9. 22:00 UTC) | +102 bp |
| 19. 12. 2025 | +85 bp (16. 12. 23:00 UTC) | +101 bp |
| 20. 3. 2026 | +87 bp (19. 3. 22:00 UTC) | +105 bp |
| 19. 6. 2026 | +90 bp (17. 6. 22:00 UTC) | +95 bp |
| 18. 9. 2026 | +89 bp (16. 9. 22:00 UTC, `ibkr`) | +103 bp (`ibkr`) |

- U ES skok odpovídá basisu U6×Z6 ~0,9 % (#1349). U NQ je 95–126 bp, tedy méně než uvedený
  basis ~1,5 %; skok přes denní pauzu obsahuje i pohyb ceny. Bary sledují front kontrakt až do
  týdne expirace, ne do `expiry_calendar.roll_date` (expirace − 8 dní).
- **Důsledek:** metriky uvnitř seance (rozsah, EM, skoky) to neovlivní. Výnos close-to-close přes
  roll nebo vícedenní okno (E-6.1, vícedenní horizont; E-5.8) ale nese falešný skok +85 až
  +126 bp, pokud se řada neočistí o basis.
- **Ostatní skoky přes hranici seance:** víkendové gapy s mezerou 2 941 minut (2 881 při změně
  času), gapy přes denní pauzu v dubnu 2025 a 7. 4. 2026 a u NQ 14. a 16. 7. 2026 s mezerou
  1 321 minut. Ty leží v díře barů 14.–16. 7., kterou opravuje E-0.2. U ad-hoc symbolů jde
  o mezery mezi dny pohledu.

### 5. Konfigurace: `disk_limit_gb` a `cumdelta_source`

| klíč | default (`config.py`) | env kontejneru enginu | PG `settings` | efektivně (`/status`) |
|---|---|---|---|---|
| `disk_limit_gb` | 20,0 (`config.py:393`) | nastaveno, `Settings()` čte 5,0 | 20 | `disk_limit_bytes` 21 474 836 480 = 20 GiB |
| `cumdelta_source` | `midpoint` (`config.py:171`) | nenastaveno | – | `midpoint` |

- Engine za běhu překryje env hodnotou z PG `settings` (`runtime_settings.apply_runtime_settings`).
  **Efektivní limit je dnes 20 GiB**, ne 5. Tělo #1336 („uložená hodnota 5 v `settings` přebíjí
  default“) popisuje starší stav. V prostředí kontejneru ale zůstává 5,0. To je výchozí
  hodnota, dokud engine nenačte `settings`.
- **Využití:** `disk_usage_bytes` 7 447 386 189 (7,45 GB), `disk_free_bytes` 25,6 GB.

### 6. Hloubka archivu

| řada | ES | NQ |
|---|---|---|
| `data/snapshots/{sym}/{exp}/{den}` | 19. 7. – 7. 10. 2026 (75 dní) | 19. 7. – 7. 10. 2026 (75 dní) |
| `data/derived/{sym}/{exp}/levels/{den}` | 19. 7. – 7. 10. 2026 (75 dní) | 19. 7. – 7. 10. 2026 (75 dní) |
| `data/derived/{sym}/bars/{den}` | 28. 7. 2024 – 7. 10. 2026 (687 dní) | 28. 7. 2024 – 7. 10. 2026 (687 dní) |
| PG `oi_eod` | 15. 7. – 7. 10. 2026 (76 dní, 103 838 řádků) | 17. 7. – 7. 10. 2026 (75 dní, 77 843 řádků) |

- Ad-hoc a krátkodobé symboly: SPX 1 den (24. 9.), SPY 4 dny, QQQ 4 dny, KO 2, SOFI 1, CL 1,
  RTY 1, MES a MNQ 2 dny snapshotů.
- **Důsledek pro E-5.8 a E-5.9:** úrovně, GEX a OI lze validovat jen od 19. 7. 2026. Dva roky
  barů dávají reakci ceny, ale ne úrovně, ze kterých by šlo ověřovat zpětně.

### 7. Velikost `data/` a PG

| adresář | GB |
|---|---|
| `snapshots` | 5,86 |
| `logs` | 0,98 |
| `derived` | 0,59 |
| `trades` | 0,02 |
| ostatní (`scenarios`, `backup`, `reports`) | < 0,01 |
| **celkem `data/`** | **7,45** |
| databáze PG (`pg_database_size`) | 0,97 |

`data/` vyrostla z 5,97 GB (29. 9., podle #1338) na 7,45 GB (7. 10.). Logy tvoří 13 % objemu:
38 souborů enginu má 931 MB, news-engine 1,4 MB.

### Nálezy mimo rozsah E-0.4a

- **#1451 (nové, P1):** feed Yahoo `rssindex` vrací od 23. 9. HTTP 404, `rss_news` přišel
  o 94 % zpráv a částečné selhání feedu nikdo nehlásí (bod 2).
- **#1349 / E-0.2:** `tasty_candle` i 18. 9. (bod 4); skoky bez zprávy 7. 4., 9. 6. a 26. 6. 2026;
  archiv barů bez očištění o basis přes roll.
- **#1336:** efektivní limit disku je 20 GiB z PG `settings`, env kontejneru drží 5,0 (bod 5).
- **E-6.1 (ADR-0053):** zlom klasifikace SentIndexu na 28. 7. 2026 a tři éry zdrojů zpráv
  (body 2 a 3).
- **E-3.4 (ADR-0049):** `news_classifications.version` neoznačuje verzi pravidel (bod 3).
- **Prostředí enginu** nese zastaralé `GEXLENS_TASTY_SHADOW` (#763). Engine při každém startu
  `Settings()` varuje a hodnotu převezme jako `tasty_enabled`.
- **AGENTS.md uvádí PostgreSQL 17**, `compose.yml:26` i `compose.dev.yml:30` ale používají
  `postgres:16`.
