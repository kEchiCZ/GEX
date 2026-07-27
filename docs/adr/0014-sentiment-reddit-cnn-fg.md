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
