# Audit stávajících zdrojů zpráv (E-6.21)

Změřený stav zdrojů zpráv jako vstup pro test kandidátů E-6.22 (#1474) a ADR-0059 o tierech obsahu
(E-6.23). Patří k bloku breaking news Fáze 6 (#1406, rozhodnutí 8. 10. 2026 v #1385). Každé číslo
má dotaz a `as_of`. Data se neextrapolují a cenzura je pojmenovaná (oddíl 3).

## Jak se měřilo

**Měřeno:** 8. 10. 2026, `as_of` = **2026-10-08T21:00:00Z** (čtvrtek 23:00 SELČ, po close seance 8. 10.).
Produkční news-engine po nasazení `59b9be3` (8. 10. 10:06 UTC, Yahoo headline feedy, #1451).
Svolení vlastníka k read-only SQL: zadání úkolu 8. 10. 2026 v chatu.

```bash
uv run python scripts/measure_news_sources_6_21.py --as-of 2026-10-08T21:00:00+00:00
```

Skript pouští všechny dotazy (konstanty `*_SQL` ve skriptu) na spojení
s `default_transaction_read_only`. Běží v jedné transakci `SET TRANSACTION READ ONLY`, která končí
rollbackem. URL databáze se nevypisuje.

- **Okna:** klouzavých 24 h a 7 dní podle `ts_ingested` (kdy zpráva přišla), ne obchodní dny. Zprávy chodí
  i o víkendu. 24 h = 7. 10. 21:00 → 8. 10. 21:00 UTC, tedy jedna celá seance. 7 d = od 1. 10. 21:00,
  včetně víkendu 3.–4. 10.
- **Zpoždění** = `ts_ingested − ts_event` v sekundách. Zahrnuje zpoždění zdroje i naši periodu
  stahování: RSS à 60 s, Finnhub à 60 s, Reddit à 300 s po jednom feedu (round robin, `__main__.py:143`,
  takže každý subreddit à 600 s), Alpaca a Bluesky push, IBKR tick.
- **Hodiny (ruční měření, ne skriptem):** news-engine jde proti NTP o **+0,29 s napřed**. Měřeno
  jedním dotazem SNTP (offset = ((t1 − t0) + (t2 − t3)) / 2) proti time.cloudflare.com, pool.ntp.org
  a time.google.com, 8. 10. 21:05 UTC v kontejneru `gex-news-engine-1` i na hostiteli; všechny tři
  servery −0,28 až −0,29 s. Zpoždění nad sekundu to nemění.

## 1. Shrnutí po zdroji

Typ obsahu je **ruční posouzení** vzorku titulků (2.6) opřené o autora, vydavatele a medián délky
titulku (2.1). Zpoždění je medián okna 7 d.
Úplná čísla jsou v oddílu 2.

| zdroj | typ obsahu | zdarma | n 24 h | n 7 d | zpoždění p50 | poznámka |
|---|---|---|---|---|---|---|
| `alpaca` (Benzinga WS) | **breaking headline** (Newsdesk 71 %: analytické akce, firemní a makro headliny), šablonové články (Insights 10 %), články autorů (19 %) | ano, Alpaca i s paper účtem (#387) | 1 001 | 4 675 | ≈ 0 s (push) | `created_at` není čas publikace s přesností pod sekundu, viz 3.4 |
| `bluesky` | sociální (kurátorovaní autoři 20 %) | ano | 1 471 | 9 224 | 2,6–5,1 s | 10 % s časem rovným příjmu, viz 3.3 |
| `fed_rss` | **oficiální** (minutes, projevy) | ano | 1 | 3 | 70–73 s | jen 2 feedy Fedu, viz 4 |
| `finnhub` (general) | články agentur (Reuters 93 %, Bloomberg 6 %) přes odkazy news.google.com | ano, free tier s klíčem | 56 | 317 | **8,3 h** (Reuters) | **není breaking**: denní medián 30 dní 3,0–30,7 h |
| `forexfactory` | kalendář (forecast/previous/actual) | ano | 20 releasů | 89 releasů | — | `ts_event` = čas releasu; zpoždění `actual` se neměří |
| `ibkr_brfg` (Briefing.com General Market Columns) | komentář a souhrny trhu (sloupky) | ano, free provider API IBKR | 28 | 62 | 7,7 min | p90 1,6 h, 24 % nad hodinu |
| `ibkr_djnl` (Dow Jones Newsletters) | newsletter, analýza (ranní briefing) | ano, free provider API IBKR | 1 | 3 | 34 h | jako breaking nepoužitelný |
| `reddit_rss` | sociální | ano | 64 | 346 | 5,5–6 min | každý subreddit à 600 s |
| `rss_news` CNBC top news | články (titulky) | ano | 51 | 288 | 2,8 min | p90 14 min |
| `rss_news` MarketWatch top stories | články, i osobní finance | ano | 44 | 236 | 2,7 min | p90 4,3 min |
| `rss_news` Yahoo `^GSPC` | agregátor článků | ano | 160 | 160 | 12 min | feed až od 8. 10. 10:06 UTC, viz 3.5 |
| `rss_news` Yahoo `^IXIC` | agregátor článků | ano | 47 | 47 | 83 min | feed až od 8. 10. 10:06 UTC, viz 3.5 |
| `rss_user` | vlastní feedy uživatele | — | 0 | 0 | — | v historii žádný záznam |

**Pro breaking news z toho plyne:** skutečně rychlé jsou dnes jen dva zdroje. Alpaca dodává pushem
headliny Benzinga Newsdesku a Bluesky je sociální síť. Oficiální zdroj je jediný, Fed se 2 feedy,
a dodá ~0,4 zprávy denně. Finnhub general nese Reuters a Bloomberg s mediánem zpoždění v hodinách,
takže je to archiv článků, ne breaking zdroj. IBKR pásky jsou komentáře a newslettery, ne headline
feed (cenu a další providery IBKR ověří E-6.22). RSS agentur má medián 2–3 min, k tomu patří
perioda stahování 60 s.

## 2. Naměřená čísla

### 2.1 Zdroje a podskupiny (okno 7 d podle `ts_ingested`)

Sloupce: `> 1 h` je podíl zpráv se zpožděním nad hodinu, `< 0` počet záporných zpoždění,
`čas = příjem` počet zpráv s `ts_event = ts_ingested`: collector čas zdroje nedostal, nebo ho ořízl
z budoucnosti (3.3). `titulek p50` je medián délky titulku ve znacích.

| zdroj | podskupina | n 24 h | n 7 d | zpoždění p50 [s] | p90 [s] | > 1 h | < 0 | čas = příjem | titulek p50 [znaků] | poslední příjem (UTC) |
|---|---|---|---|---|---|---|---|---|---|---|
| alpaca | Benzinga Insights | 136 | 456 | -0,6 | 0,1 | 0% | 387 | 0 | 66 | 2026-10-08 20:30 |
| alpaca | Benzinga Newsdesk | 687 | 3310 | -0,7 | 0,1 | 0% | 2778 | 0 | 86 | 2026-10-08 20:57 |
| alpaca | ostatní autoři | 178 | 909 | -0,6 | 0,2 | 0% | 762 | 0 | 79 | 2026-10-08 20:54 |
| bluesky | kurátorovaní | 361 | 1848 | 2,6 | 961 | 0% | 0 | 75 | 93 | 2026-10-08 20:59 |
| bluesky | nekurátorovaní | 1110 | 7376 | 5,1 | 560 | 0% | 0 | 859 | 194 | 2026-10-08 20:59 |
| fed_rss | federalreserve.gov/feeds/press_monetary.xml | 0 | 1 | 70 | 70 | 0% | 0 | 0 | 67 | 2026-10-07 18:01 |
| fed_rss | federalreserve.gov/feeds/speeches.xml | 1 | 2 | 73 | 74 | 0% | 0 | 0 | 63 | 2026-10-08 08:31 |
| finnhub | Bloomberg | 6 | 20 | 86 889 | 87 467 | 100% | 0 | 0 | 64 | 2026-10-08 15:17 |
| finnhub | CNBC | 0 | 1 | 1 897 | 1 897 | 0% | 0 | 0 | 50 | 2026-10-03 12:31 |
| finnhub | Reuters | 50 | 296 | 29 840 | 147 248 | 90% | 0 | 0 | 82 | 2026-10-08 20:32 |
| forexfactory | — | 0 | 83 | — | — | — | 83 | 0 | 26 | 2026-10-07 04:27 |
| ibkr_brfg | BRFG | 28 | 62 | 463 | 5 894 | 24% | 1 | 0 | 42 | 2026-10-08 20:26 |
| ibkr_djnl | DJNL | 1 | 3 | 122 936 | 125 272 | 100% | 0 | 0 | 31 | 2026-10-08 20:21 |
| reddit_rss | reddit.com/r/stocks/hot/.rss | 10 | 61 | 332 | 540 | 2% | 0 | 0 | 60 | 2026-10-08 17:33 |
| reddit_rss | reddit.com/r/wallstreetbets/hot/.rss | 54 | 285 | 366 | 777 | 2% | 0 | 0 | 38 | 2026-10-08 20:41 |
| rss_news | feeds.content.dowjones.io/public/rss/mw_topstories | 44 | 236 | 159 | 258 | 5% | 0 | 0 | 90 | 2026-10-08 20:57 |
| rss_news | feeds.finance.yahoo.com/rss/2.0/headline ^GSPC | 160 | 160 | 729 | 4 731 | 26% | 0 | 0 | 67 | 2026-10-08 20:54 |
| rss_news | feeds.finance.yahoo.com/rss/2.0/headline ^IXIC | 47 | 47 | 4 958 | 45 676 | 64% | 0 | 0 | 66 | 2026-10-08 20:21 |
| rss_news | cnbc.com/id/100003114/device/rss/rss.html | 51 | 288 | 165 | 818 | 0% | 0 | 0 | 82 | 2026-10-08 20:55 |

U kalendáře (`forexfactory`) jde o počet položek **vložených** v okně. Vkládají se dopředu na celý
týden, proto jsou všechna zpoždění záporná. Objem podle času releasu je v 2.3.

### 2.2 Denní medián zpoždění za 30 dní (UTC den příjmu)

Ukazuje, jestli je okno 7 d typické. Bez kalendáře.

| zdroj | dnů s příjmem | min [s] | medián [s] | max [s] |
|---|---|---|---|---|
| alpaca | 31 | -2,2 | -0,6 | 0,1 |
| bluesky | 31 | 0,1 | 1,6 | 54 |
| fed_rss | 11 | 23 | 51 | 79 |
| finnhub | 30 | 10 647 | 34 027 | 110 453 |
| ibkr_brfg | 24 | 103 | 376 | 168 578 |
| ibkr_djnl | 17 | 39 238 | 112 522 | 183 045 |
| reddit_rss | 31 | 188 | 358 | 622 |
| rss_news | 31 | 125 | 911 | 125 742 |

`rss_news` zde slučuje všechny feedy včetně Yahoo `rssindex` do 23. 9. a dnů, kdy feed doháněl
výpadek. Po feedech je rozpad jen v okně 7 d (2.1).

### 2.3 Kalendář podle času releasu

| kalendář (`kind = scheduled`) | 24 h | 7 d |
|---|---|---|
| položek s časem releasu v okně | 20 | 89 |
| z toho s vyplněným `actual` (7 d) |  | 55 |

### 2.4 Registr `news_sources` × záznamy v `news_events` (celá historie)

| zdroj | tier | očekáváno/den | enabled | záznamů | první příjem | poslední |
|---|---|---|---|---|---|---|
| alpaca | extra | 800 | ano | 107301 | 2026-08-17 12:53 | 2026-10-08 20:57 |
| bluesky | test | 200 | ano | 48818 | 2026-08-27 21:15 | 2026-10-08 20:59 |
| fed_rss | core | 5 | ano | 54 | 2026-07-28 08:19 | 2026-10-08 08:31 |
| finnhub | extra | 200 | ano | 5074 | 2026-07-29 10:19 | 2026-10-08 20:32 |
| forexfactory | core | 40 | ano | 15826 | 2026-07-28 08:19 | 2026-10-07 04:27 |
| ibkr_brfg | extra | 50 | ano | 1170 | 2026-07-28 21:05 | 2026-10-08 20:26 |
| ibkr_djnl | extra | 50 | ano | 45 | 2026-07-28 21:05 | 2026-10-08 20:21 |
| reddit_rss | test | 50 | ano | 2045 | 2026-08-27 21:15 | 2026-10-08 20:41 |
| rss_news | core | 300 | ano | 46413 | 2026-07-28 08:19 | 2026-10-08 20:57 |
| rss_user | test | — | ano | **0** | — | — |

`alpaca` od 17. 8. zahrnuje i backfill historie (`scripts/alpaca_news_backfill.py`, ADMIN-MANUAL).
Zprávy z backfillu mají `ts_ingested` z doby backfillu, proto se celkové počty nesrovnávají s okny 2.1.

### 2.5 Uložené sloučené duplicity

Žádný záznam v celé historii `news_events` nenese `raw.merged_sources` (`MERGED_SQL`).

### 2.6 Ruční doplňkové dotazy (podklad typu obsahu)

Stejné spojení a transakce jen pro čtení, `as_of` 8. 10. 2026 ~21:00 UTC. Slouží jen jako podklad
pro ruční posouzení typu v oddílu 1, titulky se do reportu nepřebírají.

```sql
-- vzorek: poslední 4 titulky na zdroj a feed za 2 dny (bez Bluesky a Redditu)
SELECT * FROM (SELECT source, raw->>'feed' f, title,
    row_number() OVER (PARTITION BY source, raw->>'feed' ORDER BY id DESC) rn
  FROM news_events WHERE ts_ingested >= now() - interval '2 days'
    AND source NOT IN ('bluesky', 'reddit_rss')) s WHERE rn <= 4;
-- odkazy Finnhubu: všech 12 posledních vede na news.google.com/rss/articles/…
SELECT ts_event, ts_ingested, raw->>'url' FROM news_events
  WHERE source = 'finnhub' ORDER BY id DESC LIMIT 12;
```

## 3. Cenzura a výhrady k číslům

1. **Objem po zdroji = první doručení, ne vše, co zdroj dodal.** Deduplikace (`dedup.py`, okno 6 h,
   titulek a Jaccard ≥ 0,9, ADR-0016/0017) i unikátní `dedup_hash` (titulek + den) ponechají jen první
   kopii zprávy. Pozdější kopie z jiného zdroje se zahodí. Zdroj, který bývá druhý, má proto v tabulce
   méně zpráv, než dodal. Rychlost zdrojů vůči sobě se z DB určit nedá.
2. **`raw.merged_sources` se do DB nikdy nedostane.** Za celou historii ho nemá ani jeden záznam
   (2.5). Příčina je v kódu: sloučení se ukládá jen k eventu zapisovanému ve **stejné dávce**
   (`pipeline.py:50-58`). Každý collector ale zapisuje vlastní dávku (`runner.py:78`) a sloučení v rámci
   jednoho zdroje se nepočítá (`dedup.py:162`). Kopie z jiného zdroje proto přijde vždy až po zápisu
   prvního eventu a skončí jen v paměti. Čítače `merged_total` a `duplicates_total`
   (`pipeline.py:32-33`) nikdo nečte. Test `test_dedup.py:308` ověřuje dávku ze dvou zdrojů, jaká
   v provozu nenastane. ADR-0016 (bod 2) a docstring `dedup.py:8-11` proto slibují něco, co se
   neděje. IBKR pásky zapisuje engine mimo tuto deduplikaci (`newsticks.py:344-353`, jen
   `dedup_hash`). **Důsledek:** srovnání zdrojů „kdo byl první“ (E-6.29)
   potřebuje nejdřív záznam všech zdrojů u duplicit (E-6.24, `news_event_sources`).
3. **Zprávy s časem rovným příjmu:** Bluesky má 934 z 9 224 zpráv s `ts_event = ts_ingested`.
   Collector nahrazuje chybějící čas časem příjmu a čas z budoucnosti na něj ořezává
   (`bluesky.py:84-92`). `createdAt` se neukládá (`bluesky.py:106`), takže z DB obě příčiny rozdělit
   nejde. Zpoždění těchto zpráv je nulové z konstrukce a medián Bluesky stahuje dolů. U RSS by totéž
   nastalo bez `pubDate` (`rss.py:266`). V okně se to nestalo.
4. **Alpaca: 84 % záporných zpoždění, medián podskupin −0,6 až −0,7 s.** Naše hodiny jdou o 0,29 s
   napřed, takže chyba na naší straně by zpoždění zvětšila, ne zmenšila. Benzinga `created_at` tedy leží typicky ~0,9–1,0 s
   **po** našem příjmu. Hypotéza (neověřená): je to čas záznamu u Benzingy, ne okamžik publikace.
   Stejný obraz by dal i posun hodin Benzingy nebo Alpacy o ~1 s. Pro breaking news to nevadí: push
   je okamžitý a `created_at` se jen nedá brát s přesností pod sekundu.
5. **Přestavba zdroje Yahoo:** feedy `^GSPC` a `^IXIC` běží od 8. 10. 10:06 UTC (#1451). Mezi 23. 9.
   21:40 a 8. 10. 10:06 UTC Yahoo nedodal nic. Okno 7 d jejich feedů tak pokrývá ~11 h a první stažení
   přineslo i starší položky. Medián `^IXIC` 83 min je proto horní odhad. Přeměřit lze stejným skriptem
   po týdnu provozu, hodnota se neextrapoluje.
6. **IBKR `ibkr_brfg` není živý tick:** medián 7,7 min a 24 % nad hodinu, přestože docstring
   `newsticks.py` uvádí „broker tick chodí živě“. Z dat nejde rozlišit, jestli jde o čas sloupku
   u Briefing.com, nebo o doručení po (re)subskripci. Pro tento audit to nevadí, protože obsahem jsou
   komentáře, ne headliny. Kdyby se páska měla stát breaking zdrojem, patří to k E-6.22.
7. **Objem sociálních zdrojů je výběr, ne tok:** Bluesky ukládá jen posty kurátorovaných autorů,
   nebo posty s cashtagem či klíčovým slovem (`bluesky.py:63-67`), se stropem 30 za minutu
   (`bluesky.py:40`). Reddit ukládá jen nové položky z „hot“ top 25 každého subredditu.
8. **Zpoždění kalendáře** (`actual` po releasu) se neměří: `forexfactory` přepisuje `actual` na místě
   bez časové značky a `ts_ingested` znamená první stažení položky týdne.

## 4. Konfigurace × collectory × registr

Vstup pro E-6.22, aby nevznikl duplicitní adaptér.

| položka | kde | stav |
|---|---|---|
| `fred`, `bls`, `bea` | klíče `NewsSettings.fred_api_key`, `bls_api_key`, `bea_api_key` (`config.py:124-126`) | **nakonfigurované bez collectoru**: klíče čte jen `enabled_sources` (`config.py:132-146`) pro řádek logu „zdroje s konfigurací“ (`__main__.py:592`) a CLI `status`. Žádný kód z nich nestahuje data. Log tak tvrdí, že zdroje BLS a BEA běží, a to neplatí. |
| `gemini`, `reddit`, `cnn_fg` | `enabled_sources` | nejsou zdroje `news_events`: LLM klasifikace (zakonzervovaná, #740), Reddit OAuth a CNN Fear & Greed jdou do crowd metrik (`crowd.py`) |
| `rss` | `enabled_sources` | souhrnné jméno, skutečné zdroje jsou `rss_news`, `reddit_rss` a `rss_user` |
| `fed_rss` | `FED_RSS_URLS` (`config.py:14-17`) | 2 feedy: `press_monetary`, `speeches`. Ostatní feedy Fedu se nestahují (kandidát E-6.22). |
| `rss_user` | registr (`NEWS_SOURCE_SEED`), collector jen s vlastními feedy (`__main__.py:146-150`) | **v registru bez záznamů**: žádný vlastní feed nikdy nic nezapsal |
| zdroje mimo registr | `news_events` × `news_sources` | žádné: všech 9 zapisujících zdrojů je v registru |
| `expected_daily_volume` | registr (čte panel Zdroje v UI přes API) | zastaralé proti měření 2.1, např. `ibkr_djnl` čeká 50/den a dodá ~0,4/den, `bluesky` čeká 200/den a dodá ~1 300/den. Aktualizaci řeší E-6.23 s tiery obsahu. |

## 5. Vstupy pro další úkoly

- **E-6.22 (#1474):** oficiální zdroje mimo Fed dnes chybí. BLS a BEA mají v konfiguraci klíče, ale
  collector ne; adaptér se staví nový (E-6.25), ne „opravuje“. Finnhub general jako breaking zdroj
  nevyhovuje (medián zpoždění v hodinách po celých 30 dní). Pokud test vybere jiný Finnhub endpoint,
  musí to zpoždění změřit znovu. IBKR BRFG a DJNL nejsou headline feedy.
- **E-6.23 (ADR-0059):** typy obsahu v oddílu 1 jsou návrh pro tiery: oficiální, breaking headline,
  článek/analýza, sociální, kalendář. Alpaca se dělí podle autora (Newsdesk vs. Insights/autoři),
  ne podle zdroje. Registr potřebuje přeměřené `expected_daily_volume`.
- **E-6.24:** bez záznamu všech zdrojů u duplicit (3.1, 3.2) nejde měřit, kdo byl první. Mrtvou cestu
  `raw.merged_sources` a nečtené čítače nahradí `news_event_sources`.
- **Návazné opravy kódu** jsou zapsané v zadání úkolů epiky #1406, ne jen tady:
  - E-6.24: odstranit mrtvou cestu `raw.merged_sources`, nečtené čítače a test dávky ze dvou zdrojů
    (3.2). ADR-0059 (E-6.23) nahradí bod 2 ADR-0016.
  - E-6.25: BLS a BEA jako adaptéry místo nepoužitých klíčů. Log „zdroje s konfigurací“ má hlásit
    skutečné collectory (4).
  - E-6.16: FRED (G5) využije klíč `fred_api_key`.
