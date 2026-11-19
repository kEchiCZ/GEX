# ADR-0014: SentimentLens — Reddit API a CNN Fear & Greed

**Stav:** accepted · **Datum ověření:** 2026-07-27.

Řeší ověřovací bod 3 SPEC SentimentLens (`docs/Sentiment/sentiment-SPEC-v1.md`, kap. 1 Tier C). Issue #268.

## Zjištění

### Reddit

- **Neautentizovaný přístup je mrtvý:** `www.reddit.com/r/…/hot.json` i `api.reddit.com` vrací 403 bot-blok (HTML stránka), i s deskriptivním User-Agentem. Bez OAuth nelze číst.
- Free tier Data API: **100 dotazů/min per OAuth client** (průměrováno přes 10min okno, dle Reddit Data API Terms). Potřeba modulu: 2 subreddity à 15 min ≈ 0,13 QPM — hluboko pod limitem, zdarma.
- Application-only grant (`client_credentials`) stačí pro čtení veřejného obsahu (hot posts) — není potřeba uživatelský login flow.

### CNN Fear & Greed

- Endpoint `https://production.dataviz.cnn.io/index/fearandgreed/graphdata`: holý request → **418 „I'm a teapot. You're a bot."**; s browser hlavičkami (`User-Agent` Chrome + `Origin`/`Referer: https://edition.cnn.com`) → **200**, ~177 kB JSON.
- Struktura: `fear_and_greed` {score, rating, timestamp, previous_close/1_week/1_month/1_year}; `fear_and_greed_historical.data` = **250 denních bodů (~1 obchodní rok zpět)** {x: epoch ms, y: score, rating}; + 7 sub-indexů se stejnou historií: `market_momentum_sp500/sp125`, `stock_price_strength`, `stock_price_breadth`, `put_call_options`, `market_volatility_vix/vix_50`, `junk_bond_demand`, `safe_haven_demand`.
- Bonus: roční denní historie → **backfill crowd řady zdarma** při zřízení collectoru.

## Rozhodnutí

- **Akce uživatele před N6 (#290): založit Reddit „script" app** na `reddit.com/prefs/apps` → `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` do lokálního `.env` (S10, do repa jen placeholdery v `.env.example`). Collector použije `client_credentials` grant, polling à 15 min, jen titulky + skóre.
- CNN collector (N6): à 1 h; hlavičky (UA/Origin/Referer) v konfiguraci; defenzivní parsování — endpoint je neoficiální, 418/změna struktury → `degraded`, nikdy pád (SPEC 3.2). Při zřízení jednorázový backfill roční historie score. Ukládá se score + rating + sub-indexy (`metric` per řádek `crowd_sentiment`).
- Sub-index `put_call_options` **nenahrazuje** vlastní PCR řadu z GEXLens (jiný podklad — akciové opce vs. ES FOP) — obě řady vedle sebe.
- Fixture: `docs/Sentiment/fixtures/cnn/fearandgreed_graphdata_2026-07-27.json` (plná struktura, historie zkrácena na 2+2 body per sekce) — základ golden testu normalizace. Fixtures se při vzniku news-engine přesunou do jeho test suite.

## Důsledky

- Bez registrace Reddit app se #290 nasadí jen s CNN F&G + PCR; Reddit část za config flagem (prázdné credentials → zdroj vypnutý, ne degraded).
- CNN endpoint se může kdykoli změnit nebo přitvrdit bot-detekci — crowd vrstva je doplňková (mimo SentIndex, SPEC 5.8), výpadek nemá dopad na core modul.

## Dodatek 2026-11-19: Reddit ukončil RSS, veřejné Data API končí (#1845)

**Zjištění (ověřeno 19. 11. 2026):**
- Zdroj `reddit_rss` (nativní RSS, #578) přestal chodit **17. 11. 2026 17:39 UTC**. Každá adresa
  `.rss` na `www.reddit.com` vrací **404** s prázdným Atomem `<title>Not Found</title>`
  z `rss.reddit.com`, i s User-Agentem prohlížeče; `old.reddit.com/…/.rss` přesměruje na přihlášení
  a `hot.json` vrací 403. Reddit to oznámil 30. 9. 2026 s termínem konce RSS **13. 11. 2026**.
- Veřejné Data API: nové žádosti o přístup **uzavřené od 31. 10. 2026**, od **12. 1. 2027** odpojování
  aplikací, které se nezaregistrovaly, a v **březnu 2027** konec zbytku veřejného přístupu (aplikace
  mají přejít na Developer Platform). Placený přístup k datům jen individuální smlouvou, ceník není
  veřejný. Bod „Free tier Data API“ výše tím pro novou aplikaci neplatí. Údaje jsou ze sekundárních
  zdrojů (TechCrunch 30. 9. 2026, redditwatch.org); původní příspěvky v r/redditdev a r/modnews Reddit
  anonymně nezobrazí.
- Dopad: `reddit_rss` dával 0,4 % zpráv k ES a relevantních z nich bylo 2 % (audit #1473); tier
  obsahu NULL (ADR-0059), strop importance 1 (ADR-0045).

**Rozhodnutí vlastníka (19. 11. 2026):** zdroj se **vypíná přepínačem v registru** `news_sources.enabled`
(záložka News → Zdroje zpráv), ne změnou výchozího `GEXLENS_NEWS_REDDIT_RSS_ENABLED` — dva vypínače
na totéž by se v UI rozcházely. Kód collectoru i seznam subredditů zůstávají kvůli možnému placenému
zapnutí v budoucnu.

**Důsledky:**
- Placený přístup vrátí jen OAuth API, ne RSS. Opětovné zapnutí tedy znamená collector nad API
  (základ OAuth `client_credentials` a hot listingu je v `crowd.py`), ne přepnutí `reddit_rss`.
- Crowd collector Reddit z tohoto ADR se bez přihlašovacích údajů nespouští a novou aplikaci už
  zaregistrovat nejde; řady `wsb_hot_avg` a `stocks_hot_avg` tedy nevznikají.
