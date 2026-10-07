# Tvrdá data pro epiku #1385

Změřený stav dat pro rozhodnutí Fází 4–6 epiky #1385: validace úrovní E-5.8 a E-5.9, kontext
sentimentu E-6.1 a opravy dat E-0.2. Každý bod nese dotaz, `as_of` a výsledek. Data se
neextrapolují; neúplný měsíc a cenzurovaná řada jsou označené.

- **Oddíl I** (E-0.4a, #1392): read-only SQL nad produkční PG a čtení partic `data/`.
- **Oddíl II** (E-0.4b, #1394): provoz v RTH, doplní se samostatně.

## Oddíl I — PG a partice (E-0.4a)

**Měřeno:** 7. 10. 2026 po settle seance 7. 10., produkce na `9d29be1`. Svolení vlastníka
k read-only SQL dal 7. 10. 2026 v chatu.

| zdroj | `as_of` (UTC) |
|---|---|
| `scripts/measure_hard_data_1381.py`, body 1–4, 6, 7 | 2026-10-07T21:09:30Z |
| doplňkové `psql` dotazy (bod 2 a 5), `/status`, env kontejneru, `du` | 2026-10-07 20:44–21:10Z |

**Jak:** všechny SQL dotazy skriptu běží v jedné transakci `SET TRANSACTION READ ONLY`, která
končí rollbackem. Partice se jen čtou. Měsíc a seance jsou obchodní den
(`settle.trading_session_date`, `settle.is_trading_session`). Zprávy se sčítají po hodinových
koších UTC. Ty se na obchodní den mapují přesně, protože Globex otevírá v celou hodinu CT.
Konfigurace se čte z běžícího enginu a z `/status`, ne z `.env`.

```bash
uv run python scripts/measure_hard_data_1381.py --db "$GEXLENS_HOST_DATABASE_URL" \
    --data data --reclass-at 2026-09-26T22:14:46+00:00
# bod 5: efektivní konfigurace, jen dva klíče (žádný výpis .env)
docker exec gex-engine-1 python -c "from gexlens_engine.config import Settings; s = Settings(); print(s.disk_limit_gb, s.cumdelta_source)"
docker exec gex-engine-1 sh -c 'printenv GEXLENS_DISK_LIMIT_GB >/dev/null && echo nastaveno'
curl -s http://127.0.0.1:8010/status   # cumdelta_source, disk_usage_bytes, disk_limit_bytes, disk_free_bytes
# bod 7: logy v bajtech
du -sb data/logs; du -cb data/logs/engine* | tail -1; du -cb data/logs/news-engine* | tail -1
```

Doplňkové dotazy přes `psql` v kontejneru `postgres`, každý v `BEGIN TRANSACTION READ ONLY`:

```sql
-- bod 5: runtime hodnoty v PG
SELECT key, value FROM settings WHERE key ILIKE '%disk%' OR key ILIKE '%cumdelta%';
-- bod 2: rss_news po feedech (#1451)
SELECT raw->>'feed', count(*) FILTER (WHERE ts_event >= '2026-09-10' AND ts_event < '2026-09-24'),
       count(*) FILTER (WHERE ts_event >= '2026-09-24'), max(ts_event)
FROM news_events WHERE source = 'rss_news' AND ts_event >= '2026-09-10' GROUP BY 1;
```

Říjen 2026 je ve všech tabulkách **neúplný**: 5 seancí, 1.–7. 10.

### 1. Gamma režim seancí (ES, NQ)

**Zdroj:** `em_respect.negative_gamma_share`, podíl minut seance do settle se spotem pod měřeným
flipem (`compute/emrespect.negative_share`). Flip pochází z levels řetězu, který engine ten den
sledoval. Seance je převážně negativní při podílu ≥ 0,5.

| symbol | měsíc | seance | bez levels | neg | z toho podíl ≥ 0,9 | poz | z toho podíl ≤ 0,1 |
|---|---|---|---|---|---|---|---|
| ES | 2026-07 (od 20. 7.) | 10 | 1 | 5 | 3 | 4 | 1 |
| ES | 2026-08 | 21 | 0 | 10 | 2 | 11 | 8 |
| ES | 2026-09 | 21 | 2 | 10 | 6 | 9 | 6 |
| ES | 2026-10 (neúplný) | 5 | 0 | 1 | 0 | 4 | 1 |
| NQ | 2026-07 (od 20. 7.) | 10 | 3 | 2 | 1 | 5 | 3 |
| NQ | 2026-08 | 21 | 2 | 9 | 5 | 10 | 6 |
| NQ | 2026-09 | 21 | 2 | 6 | 4 | 13 | 6 |
| NQ | 2026-10 (neúplný) | 5 | 0 | 2 | 1 | 3 | 2 |

- **Celkem:** ES má 57 seancí, z toho 26 negativních, 28 pozitivních a 3 bez levels. NQ má
  57 seancí, z toho 19 negativních, 31 pozitivních a 7 bez levels. Žádná seance nepadla mimo
  obchodní den.
- **Hloubka:** řada začíná 20. 7. 2026, protože levels existují od prvního živého dne
  19. 7. 2026 (bod 6). Gamma režim pro období před červencem 2026 **neexistuje** a z barů ho
  dopočítat nejde.
- **Pozorované tempo (ne odhad):** ES měl v srpnu i září 10 negativních seancí z 21, NQ 9 a 6.
  Kolik seancí validace úrovní potřebuje, stanoví ADR-0052 (E-5.9, plánované).
- **„Bez levels“ je zčásti podezřelé (#1452):** u 7 z 10 seancí má vybraná partice levels
  flip prázdný skoro celý den (0–5 minut), takže NULL odpovídá datům. NQ 17. 9. (1 228 minut
  flipu), 29. 7. a 1. 9. vysvětlené nejsou. Podíl se navíc počítá jen z minut s flipem a jejich
  počet se neukládá.

### 2. Zprávy podle zdroje × měsíce (`news_events`)

**Dotaz:** `SELECT source, date_trunc('hour', ts_event), count(*) FROM news_events GROUP BY 1, 2`.
Obchodní den a měsíc se dopočítají v Pythonu. Celkem je 223 865 zpráv ve třech érách:

| Éra | Měsíce | Zdroje (měsíční objem) |
|---|---|---|
| A — jen kalendář | 2023-07 → 27. 7. 2024 | `forexfactory` 367–451 (2023-07 od 30. 7.: 21), `rss_news` do 14 |
| B — backfill zpráv | 28. 7. 2024 → 27. 7. 2026 | `alpaca` 2 532–3 863 (2024-07 od 28. 7.: 558), `forexfactory` 378–460, `rss_news` do 39, `fed_rss` od 2026-01 do 6 |
| C — živý sběr z více zdrojů | od 28. 7. 2026 | viz tabulka níže |

| měsíc | alpaca | bluesky | fed_rss | finnhub | forexfactory | ibkr_brfg | ibkr_djnl | reddit_rss | rss_news | celkem |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07 (živě od 28. 7.) | 3 721 | 2 | 12 | 359 | 403 | 95 | 5 | – | 4 546 | 9 143 |
| 2026-08 | 7 961 | 3 106 | 4 | 2 078 | 362 | 589 | 18 | 169 | 23 756 | 38 043 |
| 2026-09 | 15 541 | 34 967 | 13 | 2 283 | 457 | 414 | 19 | 1 470 | 16 988 | 72 152 |
| 2026-10 (neúplný) | 4 549 | 9 286 | 5 | 298 | 144 | 44 | 2 | 343 | 541 | 15 212 |

- **Éra C** začíná 28. 7. 2026. Od toho dne existují partice `derived/sentiment` a živá
  klasifikace. Červenec 2026 tedy obsahuje jen 4 dny živého sběru. Ze srpna na září se objem
  zvýšil 1,9× (z 38 na 72 tisíc). Bluesky v září tvoří 48 % zpráv.
- **Výpadek feedu Yahoo (#1451):** `rss_news` klesl od 24. 9. z ~600–1 000 na ~80–120 zpráv
  denně. Feed `finance.yahoo.com/news/rssindex` vrací od 23. 9. 21:40 UTC HTTP 404. Za 10.–23. 9.
  dodal 9 969 zpráv, od 24. 9. žádnou. CNBC a MarketWatch běží beze změny. Korpus má tak další
  zlom na 24. 9.
- **Mimo éry:** `alpaca` má před érou B 32 sporadických zpráv (2013–2024), `bluesky` 1 (2018).
  Jde o staré články, ne o souvislou řadu.
- **Důsledek pro E-6.1:** počty zpráv za den nejsou mezi érami srovnatelné. Normalizace napříč
  zdroji a érami (ADR-0037, ADR-0053 plánované) musí počítat se zlomy 28. 7. a 24. 9.

### 3. Sentiment: pokrytí `sentiment_daily` a přepočet po klasifikaci v2

| symbol | od | do | dní | se σ | nejstarší `update_time` | nejnovější `update_time` |
|---|---|---|---|---|---|---|
| ES | 2023-07-30 | 2026-10-07 | 1 166 | 1 136 | 29. 7. 2026 13:32 UTC | 7. 10. 2026 21:05 UTC |
| NQ | 2023-07-30 | 2026-10-07 | 1 166 | 1 136 | 12. 8. 2026 21:51 UTC | 7. 10. 2026 21:05 UTC |

Řada má kalendářní dny včetně víkendů. Prvních 30 dní (do 28. 8. 2023) nemá σ, od 29. 8. 2023
ji má každý den.

**Reklasifikace v2 (ADR-0045):** ostrý běh `reclassify_news_rules.py` měl RUN_AT
2026-09-26T22:14:46Z (`data/reports/reclass-run-20260927.txt`). Tento `created_at` nese
v `news_classifications` 52 594 řádků.

| verze | zdroj | řádků | první | poslední |
|---|---|---|---|---|
| 1 | llm | 669 | 29. 7. 2026 | 13. 8. 2026 |
| 1 | rule | 223 163 | 28. 7. 2026 | 7. 10. 2026 (živě) |
| 2 | llm | 14 747 | 29. 7. 2026 | 13. 8. 2026 |
| 2 | ngram | 100 408 | 26. 8. 2026 | 7. 10. 2026 (živě) |
| 2 | rule | 28 820 | 3. 8. 2026 | 26. 9. 2026 (RUN_AT) |
| 3 | llm | 331 | 29. 7. 2026 | 2. 8. 2026 |
| 3 | rule | 23 776 | 26. 9. 2026 (RUN_AT) | 26. 9. 2026 (RUN_AT) |

**Retro přepočet SentIndexu po reklasifikaci:**

| symbol | přepočteno po RUN_AT | rozsah | první přepočet | nepřepočteno | jejich `update_time` |
|---|---|---|---|---|---|
| ES | 60 dní | 28. 7. – 25. 9. 2026 | 26. 9. 22:34 UTC | 1 094 dní | vše 29. 7. 2026 13:32:32 UTC |
| NQ | 60 dní | 28. 7. – 25. 9. 2026 | 26. 9. 22:38 UTC | 1 094 dní | vše 12. 8. 2026 21:51:36 UTC |

- **`recompute-sentindex` po v2 proběhl 20 minut po reklasifikaci a pokryl jen dny od 28. 7. 2026.**
  Postup předepisuje `--from <nejstarší partice>` (ADMIN-MANUAL kap. 12, řádek „Klasifikace zpráv
  v2“; `reclass-run-20260927.txt`). Partice `derived/sentiment/{sym}` začínají 28. 7. 2026. Krok
  nebyl vynechán, takový je rozsah postupu. Dny 30. 7. 2023 až 27. 7. 2026 (1 094 na symbol)
  mají SentIndex z jednoho běhu backfillu (ES 29. 7., NQ 12. 8. 2026), tedy z klasifikace před v2.
  **Na 28. 7. 2026 je proto zlom klasifikace**, který E-6.1 (vícedenní řada, kauzální percentily)
  musí přepočíst, nebo držet jako éru.
- **Číslo verze v `news_classifications` neoznačuje verzi pravidel**, jen pořadí záznamu
  u eventu (max+1). Živá klasifikace podle pravidel v2 se zapisuje jako `version = 1`
  (223 tisíc řádků do 7. 10.). Verzi pravidel dá jen čas (`created_at` ≥ nasazení #1293).
  MCP, které má nést verzi klasifikace (E-3.4, ADR-0049 plánované), ji z tohoto sloupce nevyčte.

### 4. Bary podle `source` × měsíce a skoky na rollu

**Zdroj:** `data/derived/{sym}/bars/*.parquet`, sloupce `ts_min`, `close` a `source`. Starší
partice sloupec `source` nemají, takže platí NULL, což `bar_source_rank` bere jako živou cestu.
Žádná minuta se v partici neopakuje.

| Období (ES i NQ) | NULL | `ibkr` | `ibkr_hist` | `tasty_candle` |
|---|---|---|---|---|
| 2024-07 → 2026-07 | 27–32 tisíc minut/měsíc (2024-07 od 28. 7.: 4 140) | – | – | – |
| 2026-08 | 15 300 | 13 680 | – | – |
| 2026-09 | – | ES 27 164 / NQ 27 055 | ES 2 513 / NQ 2 577 | ES 446 / NQ 488 |
| 2026-10 (neúplný) | – | ES 6 622 / NQ 6 620 | ES 278 / NQ 280 | – |

- **Nález 1 z #1349 potvrzen s upřesněním:** hluboký archiv od 28. 7. 2024 není značený `ibkr`,
  sloupec `source` vůbec nemá (NULL). Význam je stejný: archiv se tváří jako změřená živá minuta.
  Od živých minut před zavedením `source` (19. 7. až polovina 8. 2026) ho odliší jen datum.
- **Ad-hoc pohledy** (SPX, SPY, QQQ, KO, SOFI, CL) mají bary jen pro jednotlivé dny, převážně
  `tasty_candle`. RTY, MES a MNQ mají jen pár dní z července 2026.

**Seance s doplněnými bary** (jiné než NULL a `ibkr`):

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
  a 16. 9. (ES 61, NQ 61), což sedí. Rekonstruované svíčky ale nese i **18. 9.** (ES 338,
  NQ 314 minut), den kvartální expirace. Je to vstup pro E-0.2 (#1390).

**Skoky ≥ 60 bp přes hranici seance** (první bar seance proti poslednímu baru předchozí).
Skoky uvnitř seance sem nepatří, skenuje je E-0.2 (#1390). Archiv **není spojitý přes roll**.
Každá kvartální expirace má na otevření seance expirace nebo 1–2 seance před ní skok o velikosti
basisu, s mezerou 61 minut přes denní pauzu. Tím se potvrzuje nález 1 z #1301 (ADR-0028
dodatek klade hranici kontraktu na expiraci).

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

- **Dvě pravidla rollu:** archiv do září 2026 přechází na nový kontrakt v týdnu expirace.
  Skok 16. 9. 2026 22:00 UTC je okamžik nasazení ADR-0039 (Důsledky: „nasazením 16. 9. večer
  engine přepne na ESZ6/NQZ6“). Od té doby jede živá pipeline včetně barů na novém kontraktu
  od `roll_date` (expirace − 8 dní). Příští skok živé řady proto přijde v seanci 10. 12. 2026,
  ne v týdnu expirace 18. 12.
- **Rozpor velikosti basisu u NQ:** u ES skok odpovídá basisu U6×Z6 ~0,9 % (#1349). U NQ #1349
  i epika uvádějí ~1,5 %, ale všech 9 skoků NQ má 95–126 bp (září 2026: 103 bp). Před otázkou 17
  (převod úrovní přes basis, Fáze 4) je potřeba basis NQ změřit přímo.
- **Důsledek:** metriky uvnitř seance (rozsah, EM) to neovlivní. Výnos close-to-close přes roll
  nebo vícedenní okno (E-6.1, vícedenní horizont; E-5.8) ale nese falešný skok +85 až +126 bp,
  dokud se řada o basis neočistí. Očištění musí znát obě pravidla.
- **Ostatní skoky přes hranici seance:**
  - víkendové gapy s mezerou 2 941 minut (2 881 při změně času);
  - gapy přes denní pauzu v dubnu 2025 a 7. 4. 2026;
  - u NQ 14. a 16. 7. 2026 mezera 1 321 minut, tedy díra barů 14.–16. 7., kterou opravuje E-0.2;
  - u ad-hoc symbolů mezery mezi dny pohledu.

### 5. Konfigurace: `disk_limit_gb` a `cumdelta_source`

| klíč | default (`config.py`) | env kontejneru enginu | PG `settings` | efektivně (`/status`) |
|---|---|---|---|---|
| `disk_limit_gb` | 20,0 (`config.py:393`) | nastaveno, `Settings()` čte 5,0 | 20 | `disk_limit_bytes` 21 474 836 480 = 20 GiB |
| `cumdelta_source` | `midpoint` (`config.py:171`) | nenastaveno | – | `midpoint` |

- Engine za běhu přepíše env hodnotou z PG `settings` (`runtime_settings.apply_runtime_settings`,
  klíč v `RUNTIME_SETTINGS`). **Efektivní limit je dnes 20 GiB**, ne 5. Tělo #1336 („uložená
  hodnota 5 v `settings` přebíjí default“) popisuje starší stav. V prostředí kontejneru zůstává
  5,0, a to je výchozí hodnota, dokud engine nenačte `settings`.
- **Využití (21:10 UTC):** `disk_usage_bytes` 7 448 779 737 (7,45 GB), `disk_free_bytes`
  25 594 163 200 (25,6 GB).

### 6. Hloubka archivu

| řada | ES | NQ |
|---|---|---|
| `data/snapshots/{sym}/{exp}/{den}` | 19. 7. – 7. 10. 2026 (75 dní) | 19. 7. – 7. 10. 2026 (75 dní) |
| `data/derived/{sym}/{exp}/levels/{den}` | 19. 7. – 7. 10. 2026 (75 dní) | 19. 7. – 7. 10. 2026 (75 dní) |
| `data/derived/{sym}/bars/{den}` | 28. 7. 2024 – 7. 10. 2026 (687 dní) | 28. 7. 2024 – 7. 10. 2026 (687 dní) |
| PG `oi_eod` | 15. 7. – 7. 10. 2026 (76 dní, 103 838 řádků) | 17. 7. – 7. 10. 2026 (75 dní, 77 843 řádků) |

- **Ad-hoc a krátkodobé symboly** mají jen pár dní snapshotů: SPX 1 den (24. 9.), SPY 4, QQQ 4,
  KO 2, SOFI 1, CL 1, RTY 1, MES a MNQ po 2.
- **Důsledek pro E-5.8 a E-5.9:** úrovně, GEX a OI lze validovat jen od 19. 7. 2026. Dva roky
  barů dávají reakci ceny, ale ne úrovně, ze kterých by šlo zpětně ověřovat.

### 7. Velikost `data/` a PG

Všechny hodnoty jsou v GB (10⁹ B).

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
39 souborů `engine*` má 0,975 GB, news-engine 0,0014 GB.

### Nálezy mimo rozsah E-0.4a

- **#1451 (nové, P1):** feed Yahoo `rssindex` vrací od 23. 9. HTTP 404. `rss_news` tím přišel
  o 94 % zpráv a částečné selhání feedu nikdo nehlásí (bod 2).
- **#1452 (nové, P2):** `em_respect.negative_gamma_share` je NULL u 10 seancí a 3 z nich nemají
  vysvětlení. Podíl se ukládá bez počtu minut (bod 1).
- **#1453 (nové, P3):** prostředí enginu drží zastaralé `GEXLENS_TASTY_SHADOW` (#763).
  `Settings()` při načtení varuje a hodnotu převezme jako `tasty_enabled`.
- **#1349 a E-0.2 (#1390):**
  - `tasty_candle` i 18. 9. (bod 4);
  - rozpor basisu NQ (bod 4).
- **#1336:** efektivní limit disku je 20 GiB z PG `settings`, env kontejneru drží 5,0 (bod 5).
- **#1301:** roll v archivu potvrzen. Od ADR-0039 platí pro živou řadu jiné pravidlo (bod 4).
- **E-6.1 (ADR-0053 plánované):** zlomy klasifikace a korpusu 28. 7. a 24. 9. 2026 a tři éry
  zdrojů zpráv (body 2 a 3).
- **E-3.4 (ADR-0049 plánované):** `news_classifications.version` neoznačuje verzi pravidel
  (bod 3).
- **#1397:** AGENTS.md uvádí PostgreSQL 17, `compose.yml:26` i `compose.dev.yml:30` používají
  `postgres:16`.
