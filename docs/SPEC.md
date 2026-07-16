# GEXLens — Kompletní funkční a technická specifikace
**Verze 2.0 · 16. 7. 2026 · Autor zadání: Roman (ROHOR Studio)**

> Cíl: **plně funkční, spustitelná aplikace** (nikoli MVP) pro vizualizaci opčního positioningu (GEX/OI/Vol heatmapa) nad ES futures opcemi, s jediným datovým zdrojem **Interactive Brokers TWS/Gateway API**. Vzor UX: Moodix v2.7.0.

---

## 0. Klíčová rozhodnutí (závazná)

| # | Rozhodnutí |
|---|---|
| R1 | Žádná MVP zjednodušení — všechny moduly ve finální podobě |
| R2 | Cum Δ s **plnou klasifikací agresora**: tick-by-tick pro hot zónu (ATM ±15 strikes), Lee–Ready midpoint test pro zbytek řetězce |
| R3 | Retence intraday dat a tick dat: **14 dní** (denní Parquet partice, noční purge job) |
| R4 | **Výjimka: EOD snapshot Open Interest se archivuje bez časového limitu** (řádově KB/den, nenahraditelná data) |
| R5 | Datový zdroj výhradně IBKR (účet existuje); žádné placené externí feedy |
| R6 | Stack: Python 3.12 + ib_async (data engine), FastAPI + WebSocket (API), PostgreSQL (metadata + OI archiv), Parquet (snapshoty), React + TypeScript (frontend), canvas/WebGL heatmapa |

---

## 1. Účel a uživatelské scénáře

Aplikace odpovídá intradennímu traderovi ES/0DTE opcí na otázky:

1. **Kde jsou call/put walls** — dominantní koncentrace positioningu působící jako rezistence/support/magnet.
2. **Kde je zero-gamma (flip)** — hranice mezi režimem komprese (dealer long gamma) a expanze (short gamma) volatility.
3. **Jak se positioning vyvíjí v čase** — build-up/unwind během seance, viditelné v heatmapě čas × strike.
4. **Jaký je živý tok** — opční volume a kumulativní delta flow vs. statický OI.
5. **Replay** — přetočení celého dne zpět a přehrání vývoje.

Primární instrument: ES (E-mini S&P 500) futures opce, všechny weekly/EOM expirace. Sekundárně akciové/indexové opce (SPY, SPX, jednotlivé akcie z watchlistu) — architektura musí být instrument-agnostická.

---

## 2. Architektura

```
┌────────────────────────────────────────────────────────┐
│ TWS / IB Gateway (localhost:7496 live / 7497 paper)    │
└──────────────┬─────────────────────────────────────────┘
               │ ib_async (jediné socket připojení, clientId konfig.)
┌──────────────▼─────────────────────────────────────────┐
│ DATA ENGINE (Python, asyncio, samostatný proces)       │
│  • ConnectionManager (watchdog, reconnect, backoff)    │
│  • ChainDiscovery (řetězce, trading classes, expirace) │
│  • SubscriptionScheduler (rotace dávek, repair queue)  │
│  • HotZoneCollector (tick-by-tick ATM±15)              │
│  • SnapshotWriter (1min konsolidace → Parquet)         │
│  • OIArchiver (EOD, PostgreSQL, bez retence)           │
│  • ComputeEngine (GEX, levels, walls, profily, CumΔ)   │
│  • RetentionJob (purge >14 dní, mimo OI archiv)        │
└──────────────┬─────────────────────────────────────────┘
               │ Parquet (snapshoty) + PostgreSQL (meta, OI)
┌──────────────▼─────────────────────────────────────────┐
│ API SERVER (FastAPI)                                   │
│  REST: /instruments /chains /snapshots /levels /replay │
│  WS:   /ws/live (push snapshotů, ceny, stavů)          │
└──────────────┬─────────────────────────────────────────┘
┌──────────────▼─────────────────────────────────────────┐
│ FRONTEND (React + TS, Vite)                            │
│  Heatmapa (canvas/WebGL) · Strike profil · Spodní      │
│  panely · Playback · Watchlist · Settings · Console    │
└────────────────────────────────────────────────────────┘
```

Engine a API server běží jako dva procesy (nebo jeden proces se dvěma asyncio task groupami — rozhodnutí v implementaci), frontend je SPA servírovaná FastAPI nebo samostatně.

---

## 3. IBKR datová vrstva

### 3.1 Připojení
- `ib_async`, host/port/clientId z konfigurace; `reqMarketDataType(1)` (live). Delayed data (typ 3) engine odmítne s chybovým stavem — Greeks z delayed dat nejsou spolehlivé.
- Watchdog: heartbeat, automatický reconnect s exponenciálním backoff (2→60 s), po reconnectu plná resubskripce. UI indikátor `Connected/Reconnecting/Disconnected` + port.
- Všechny API chyby logované do IBKR Console (viz 8.7) s error kódem a kontraktem.

### 3.2 Discovery opčního řetězce
- `reqSecDefOptParams` na podklad (ES futures → FOP, exchange CME/GLOBEX; akcie/indexy → OPT, SMART).
- ES weekly trading classes: `E1A–E5A` (po), … `E1E–E5E`? — **implementační úkol: enumerovat skutečné tradingClass z API**, minimálně denní weeklies (E1C–E5C styl viditelný v Moodixu jako „E3C"), EOM (`EW`), kvartální (`ES`). Selektor expirace v UI zobrazuje tradingClass + datum.
- Rozsah strikes: konfigurovatelné pásmo ±X bodů od spotu (default ±200 pro ES, tj. ~180 strikes), s automatickým rozšířením, pokud se spot přiblíží k okraji na < 25 % pásma.

### 3.3 Rotační scheduler subskripcí (celý řetězec)
- Limit market data lines (default ~100) → řetězec se streamuje **v dávkách** (velikost dávky konfigurovatelná, default 80 kontraktů).
- Cyklus dávky: subskribuj → čekej na kompletní sadu (bid, ask, last, volume, tickOptionComputation s Γ/Δ/IV) nebo timeout (default 4 s) → ulož do in-memory cache → odsubskribuj → další dávka.
- **Repair queue:** kontrakty, které v dávce nedodaly kompletní data, jdou do fronty s retry (max N pokusů/sweep, pak označeny `stale` se stářím). UI zobrazuje `Greeks X/Y` a `Repair: retrying N incomplete strikes`.
- Priorita: ATM ± 30 strikes se sweepují každý cyklus, křídla každý k-tý cyklus (konfig.). Cíl: kompletní sweep řetězce ≤ 90 s.
- Vytížení lines zobrazováno ve stavové liště (`Greeks NN %`).

### 3.4 Hot zóna — tick-by-tick (R2)
- Dynamická množina: ATM ±15 strikes × C/P (≈ 60 kontraktů), přepočítávaná při pohybu spotu o ≥ 1 strike krok.
- Pro každý kontrakt hot zóny: `reqTickByTickData("AllLast")` + průběžné bid/ask (z reqMktData streamu hot zóny, který je trvalý — hot zóna se nerotuje).
- Pozn.: souběžné tick-by-tick streamy jsou limitované účtem — engine musí limit detekovat (error 10190 apod.), degradovat šířku hot zóny a stav reportovat do UI. Skutečný limit ověřit na účtu (issue).
- Každý trade klasifikován ihned při příjmu: **Lee–Ready** — cena ≥ ask → buy, ≤ bid → sell, jinak vs. mid; přesně na midu tick test (směr poslední změny ceny).

### 3.5 Open Interest
- FOP: generic tick `588` (futures OI, tickType 86); akciové opce: generic tick `101` (tickTypes 27/28). Chování 588 na FOP ověřit na účtu (issue) — fallback EOD hodnota z burzy přes reqMktData snapshot ráno.
- OI se mění 1× denně → engine ukládá **EOD/ranní OI snapshot per strike per expirace do PostgreSQL, navždy (R4)**. Intradenní vrstva „změny positioningu" = volume, ne OI.

### 3.6 Podkladová data
- ES kontinuální/aktivní kontrakt: `reqRealTimeBars` (5 s) agregované na 1 min + `reqHistoricalData` backfill 1min barů pro aktuální den a 14 dní zpět.
- Pacing guard: globální rate limiter historical requestů (≤ 60/10 min, identické requesty deduplikované, fronta s prioritou).

### 3.7 Stavové indikátory datového pipeline (UI kontrakt)
- `Greeks X/Y`, `OHLC X/Y` (progres backfillu), `Repair: retrying N…`, `Opts: a/b  All: c/d` (subskripční kanály), `Greeks NN %` (vytížení lines), obsazení disku, `● Live HH:MM:SS` / `Stale`.

---

## 4. Výpočetní jádro

### 4.1 GEX
Pro strike K, stranu s ∈ {C, P}, multiplikátor M (ES=50, MES=5, akciové opce=100), spot S:

```
GEX_1pt(K,s)  = Γ(K,s) · OI(K,s) · M                 [$ delta-hedge / 1 bod]
GEX_1pct(K,s) = Γ(K,s) · OI(K,s) · M · S² · 0.01     [$ / 1 % pohyb]
NetGEX(K)     = GEX(K,C) − GEX(K,P)                  [naivní dealer model]
TotalGEX      = Σ_K NetGEX(K)
```
Naivní model (dealeři long call gamma, short put gamma) je default; rozhraní ComputeEngine musí umožnit výměnu znaménkového modelu (strategy pattern) pro budoucí flow-based odhad.

### 4.2 Levels
- **Flip (zero gamma):** kumulativní NetGEX od nejnižšího striku; nulový průchod lineárně interpolován mezi sousedními strikes.
- **Call Wall:** argmax_K NetGEX(K) pro K nad spotem; **Put Wall:** argmin_K NetGEX(K) pod spotem.
- **Centroid (HVL):** Σ(K·|NetGEX(K)|)/Σ|NetGEX(K)|.
- Levels se přepočítávají každý snapshot a ukládají jako časová řada (pro vykreslení jejich vývoje v heatmapě — „GEX Levels" checkbox).

### 4.3 Heatmap metriky (Mode)
Per snapshot t, strike K (vol = kumulativní denní volume do času t; ΔVol = přírůstek za minutu):

| Mód | Definice |
|---|---|
| OI | OI(K,C), OI(K,P) — dvě barevné vrstvy |
| Vol OTM | vol(K,C) pro K>S; vol(K,P) pro K<S |
| Vol ITM | doplněk OTM |
| Vol ± | vol(K,C) − vol(K,P) (znaménko → barva) |
| OI+OTM | normalizované w₁·OI + w₂·volOTM (w default 0.6/0.4, konfig.) |
| OI−ITM | OI(K,s) − volITM ekvivalent (odečtení uzavíraného ITM toku) |
| OI±All | OI(K,C) − OI(K,P) |

Škály (Scale): `Linear v · √v · ln(1+v) · v^(1/3)`; normalizace na p99 hodnotu viditelného okna (robustní vůči outlierům), přepínatelně na globální max.

### 4.4 Walls módy (linie)
- **Peak:** per t argmax_K metriky, zvlášť call/put vrstva.
- **Center:** vážené těžiště per t a vrstva.
- **Smooth:** EMA(Peak, span 15 min).
- **Flip:** zero-gamma řada z 4.2.
- **Ridge:** lokální maxima profilu per t (prominence filtr), spojená mezi sousedními t nejbližším strikem → více souběžných hřebenů.

### 4.5 Cum Δ — plná klasifikace agresora (R2)
```
Hot zóna (tick-by-tick):
  za každý trade: sign = LeeReady(price, bid, ask, tickTest)
  flowΔ += sign · size · Δ(K,s) · M

Zbytek řetězce (1min):
  ΔVol(t,K,s) = vol(t) − vol(t−1)
  sign = midpoint test posledního last vs. aktuální bid/ask v okně
  flowΔ += sign · ΔVol · Δ(K,s) · M

CumΔ(t) = Σ_{τ≤t} flowΔ(τ)     (reset na začátku obchodního dne, konfig. session start)
```
Δ(K,s) se bere z posledního platného Greeks snapshotu daného kontraktu. Panel Cum Δ vykresluje řadu jako plochu nad/pod nulou.

### 4.6 Strike profil (pravý panel)
```
Profile(K) = [volC(K)·|ΔC(K)| + w·OIC(K)·|ΔC(K)|] − [volP(K)·|ΔP(K)| + w·OIP(K)·|ΔP(K)|]
```
Skládané pruhy: složka Vol a složka OI Δ vizuálně odlišené odstínem; dropdown `Vol + OI Δ` přepíná varianty (jen Vol / jen OI Δ / kombinace). Call doprava (teal), Put doleva (červená), symetrická osa, zoom 1×/2×/4×. Tooltip: strike, OI C/P, vol C/P, NetGEX, vzdálenost od spotu.

---

## 5. Storage a retence

### 5.1 Parquet snapshoty
- Partice `data/snapshots/{symbol}/{expiry}/{YYYY-MM-DD}.parquet`, řádek = (ts_min, strike, right) se sloupci: bid, ask, last, volume, iv, delta, gamma, theta, vega, oi, stale_age.
- Tick data hot zóny: `data/ticks/{symbol}/{YYYY-MM-DD}.parquet` (ts, conId, price, size, side).
- Odvozené řady (levels, walls, CumΔ, flowΔ): `data/derived/…` — počítané při zápisu, replay je nečte znovu z raw dat.

### 5.2 Retence (R3, R4)
- Noční job (konfig. čas po zavření US): smaž partice `snapshots/`, `ticks/`, `derived/` starší **14 kalendářních dní**.
- **OI archiv v PostgreSQL se NIKDY nemaže** (tabulka `oi_eod(symbol, expiry, strike, right, date, oi)`).
- Stavová lišta zobrazuje obsazení disku; konfigurovatelný hard limit s alertem.

### 5.3 PostgreSQL
Tabulky: `instruments`, `expiries`, `oi_eod`, `watchlist`, `alerts`, `annotations`, `settings`, `engine_status_log`.

---

## 6. API (FastAPI)

### REST
- `GET /instruments` · `GET /instruments/{sym}/expiries`
- `GET /snapshots/{sym}/{expiry}?date=&from=&to=&mode=&scale=` — matice pro heatmapu (binárně/Arrow pro výkon)
- `GET /levels/{sym}/{expiry}?date=` — časové řady flip/walls/centroid
- `GET /profile/{sym}/{expiry}?ts=` — strike profil k okamžiku
- `GET /flow/{sym}?date=` — Vol, OptVol, CumΔ řady
- `GET /replay/{sym}/{expiry}/{date}` — kompletní denní balík pro playback
- `GET /status` — stav pipeline (greeks progress, repair, lines, disk)
- CRUD `/watchlist`, `/alerts`, `/annotations`, `/settings`

### WebSocket `/ws/live`
Kanály: `price.{sym}` (tick), `snapshot.{sym}.{expiry}` (1min delta update heatmapy — jen změněné buňky), `levels.*`, `flow.*`, `status`, `news`, `alerts`. Klient subskribuje kanály zprávou; server pushuje.

---

## 7. Frontend

### 7.1 Layout
Levý sbalitelný sidebar (Dashboard, Watchlist s % změnami, IBKR Console, Theme, Settings, Sign out, verze) · hlavička instrumentu (ticker, název, last + změna, selektor expirace, datum, Live indikátor, notifikační zvonek s badge) · řádek timeframe (Intraday/Daily, 1m/5m/15m) · řádek přepínačů (Dyn GEX, GEX Levels, Sessions, Vol/Opt Vol/Delta, Vol+OI Δ, News) · hlavní plocha.

### 7.2 Heatmapa (canvas/WebGL)
- Render ~180 strikes × 1440 min při 60 fps pan/zoom; barevné vrstvy call (zelená) / put (červená), styl **Gradient** (bilineární interpolace) / **Blobs** (gaussovský kernel kolem koncentrací).
- **Contours Off/Major/All:** marching squares nad vyhlazeným polem; Major = 2 izolinie na p75/p90, All = 5 úrovní; bílé přerušované.
- Overlay: 1m cenová křivka (up/down tick barevně), sessions markery (konfig. seznam světových seancí s popisky), GEX Levels linie, Walls linie dle módu, značka aktuální ceny na pravé ose, timestamp dat.
- Crosshair synchronizovaný se spodními panely a strike profilem; tooltip buňky (čas, strike, hodnoty metrik).

### 7.3 Strike profil, spodní panely, playback
- Dle 4.6; spodní panely Vol / Opt Vol (C/P barevně) / Cum Δ, individuálně vypínatelné, sdílená osa X.
- Playback: slider přes celý den + ▶ s rychlostmi 1×/5×/20×; tažení synchronně přetáčí heatmapu, profil i panely; doraz vpravo = návrat na live stream.

### 7.4 Anotace
Nástroje šipka / linie / freehand, výběr barvy, mazání; persistence per instrument+den (API `/annotations`).

### 7.5 Ostatní obrazovky
- **Dashboard:** karty watchlistu (cena, %, mini NetGEX profil, vzdálenost k walls, stav dat).
- **IBKR Console:** log API událostí a chyb, správa připojení (host/port/clientId, reconnect tlačítko), přehled subskripcí a repair fronty.
- **Settings:** IBKR parametry, rozsah strikes, velikost dávky, šířka hot zóny, retence/disk limit, výchozí módy vizualizace, seznam seancí + časová zóna, definice alertů, téma Dark/Light, jazyk (CZ/EN).
- **Notifikace/News:** alert engine (cena × flip/wall cross, změna dominantního striku, skok CumΔ o konfigurovatelný práh, výpadek spojení, disk limit); news headline feed (zdroj: IBKR news subscription, je-li na účtu — jinak modul skrytý).

---

## 8. Nefunkční požadavky

| Oblast | Požadavek |
|---|---|
| Latence | Změna módu/škály < 100 ms (data v paměti klienta); live tick < 250 ms end-to-end |
| Sweep | Kompletní řetězec ≤ 90 s; hot zóna real-time |
| Integrita | Každá buňka heatmapy nese stáří dat; stale > 5 min vizuálně odlišeno |
| Odolnost | Reconnect bez ztráty intraday dat; pacing chyby nikdy neshodí engine |
| Objem | ~30–60 MB/den; 14denní okno < 1 GB; OI archiv růst ~KB/den |
| Bezpečnost | Vše lokální; žádná telemetrie; API bind na localhost (konfig.) |
| Testy | Jednotkové testy výpočtů proti golden datasetu; integrační test enginu proti ib_async mocku; e2e smoke |
| Provoz | Jeden příkaz start (docker compose nebo `make run`); engine schopný headless běhu u IB Gateway |

---

## 9. Fáze dodávky (milestones)

1. **M1 Datová vrstva** — připojení, discovery, scheduler+repair, hot zóna, OI archiv, storage+retence. Výstup: data tečou a ukládají se, CLI status.
2. **M2 Výpočty** — GEX, levels, walls, metriky, CumΔ s klasifikací, profily. Výstup: testovaný ComputeEngine + golden testy.
3. **M3 API** — REST+WS, replay endpoint.
4. **M4 Frontend** — layout, heatmapa, profil, panely, playback, sessions.
5. **M5 Provozní celek** — dashboard, console, settings, alerty, anotace, packaging, dokumentace.

## 10. Otevřené technické ověřovací body (řešené jako první issues)
1. Skutečné tradingClass ES weeklies z `reqSecDefOptParams`.
2. Chování generic ticku 588 (OI) na FOP na Romanově účtu.
3. Limit souběžných tick-by-tick streamů na účtu (šířka hot zóny).
4. Limit market data lines na účtu (velikost dávky).
