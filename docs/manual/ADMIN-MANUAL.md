# GEXLens — Manuál pro správce a vývojáře

*Verze 1.0 · červenec 2026 · interní dokumentace — není dostupná v aplikaci*

Technický popis architektury, provozu, konfigurace a vývoje aplikace GEXLens. Uživatelská příručka: `UZIVATELSKY-MANUAL.md`. Zdroj pravdy funkčních požadavků: [`docs/SPEC.md`](../SPEC.md) (v2.0); architektonická rozhodnutí v [`docs/adr/`](../adr/).

---

## Obsah

1. [Architektura](#1-architektura)
2. [Struktura repozitáře](#2-struktura-repozitáře)
3. [Provoz (docker compose)](#3-provoz-docker-compose)
4. [Konfigurace — kompletní reference](#4-konfigurace--kompletní-reference)
5. [Engine — datová pipeline](#5-engine--datová-pipeline)
6. [Datové formáty a persistence](#6-datové-formáty-a-persistence)
7. [API reference](#7-api-reference)
8. [Frontend](#8-frontend)
9. [Vývojové prostředí](#9-vývojové-prostředí)
10. [Testy a CI](#10-testy-a-ci)
11. [Známé limity účtu a otevřené body](#11-známé-limity-účtu-a-otevřené-body)
12. [Diagnostika a údržba](#12-diagnostika-a-údržba)
13. [Zprovoznění od nuly — IBKR účet, TWS/Gateway](#13-zprovoznění-od-nuly--ibkr-účet-twsgateway)

---

## 1. Architektura

```
┌────────────────────────────────────────────────────────┐
│ TWS / IB Gateway (host, port 7496/7497)                │
└──────────────┬─────────────────────────────────────────┘
               │ ib_async (jediné socket spojení)
┌──────────────▼─────────────────────────────────────────┐
│ ENGINE (kontejner, python -m gexlens_engine)           │
│  ConnectionManager · ChainDiscovery · Scheduler        │
│  HotZoneCollector · ComputeEngine · Writers · Jobs     │
└───────┬──────────────────────────┬─────────────────────┘
        │ Parquet (./data volume)  │ PostgreSQL (kontejner)
        │                          │
        │      HTTP push /internal/* (status, kanály)
┌───────▼──────────────────────────▼─────────────────────┐
│ API (kontejner, FastAPI :8000)                         │
│  REST + WebSocket /ws/live + interní ingest            │
└──────────────┬─────────────────────────────────────────┘
┌──────────────▼─────────────────────────────────────────┐
│ FRONTEND (kontejner, nginx :8080, React SPA)           │
└────────────────────────────────────────────────────────┘
```

Klíčové vlastnosti:

- **Engine a API jsou oddělené procesy.** Engine počítá a zapisuje; API jen čte storage a přeposílá live push z enginu (interní HTTP ingest → StatusStore + LiveHub → WebSocket klientům).
- **Vše lokální** — API CORS povoluje jen `localhost`/`127.0.0.1`; žádná telemetrie.
- Engine se z kontejneru připojuje na TWS na hostiteli přes `host.docker.internal`.

## 2. Struktura repozitáře

```
GEX/
├─ engine/                  Python 3.12 balík gexlens_engine
│  └─ src/gexlens_engine/
│     ├─ config.py          Pydantic Settings (.env, GEXLENS_*)
│     ├─ ibkr/              connection, discovery, scheduler, hotzone,
│     │                     underlying (bary+pacing), mock (pro testy)
│     ├─ compute/           gex, levels, heatmap, walls, cumdelta, profile
│     ├─ storage/           parquet_store, oi_archive, retention, meta
│     ├─ adapters.py        produkční ib_async adaptéry + HttpPublisher
│     ├─ runtime.py         EngineRuntime — minutový cyklus (testovatelný)
│     └─ __main__.py        vstupní bod: discovery→archiv→smyčka
├─ api/                     Python balík gexlens_api (FastAPI)
│  └─ src/gexlens_api/      main (routy+WS), data, heatmap (vektorizace),
│                           live (hub), status, crud, alerts, meta_repo
├─ frontend/                React + TypeScript + Vite
│  └─ src/                  components/, heatmap/, replay/, panels/,
│                           profile/, annotations/, state/, api/
├─ docs/                    SPEC.md, adr/, manual/
├─ docker/                  Dockerfiles + nginx.conf
├─ compose.yml              celý stack
├─ scripts/                 bootstrap, start skript pro plochu
└─ Makefile                 test / run / run-api / run-frontend / run-engine
```

Pravidla vývoje jsou v [`CLAUDE.md`](../../CLAUDE.md): práce po GitHub issues, golden testy výpočtů, IBKR se v CI nikdy nevolá živě (mock vrstva `engine/ibkr/mock.py`), komentáře česky / identifikátory anglicky.

## 3. Provoz (docker compose)

```powershell
docker compose up -d --build     # start / rebuild
docker compose ps                # stav služeb
docker compose logs -f engine    # živé logy enginu
docker compose stop              # zastavení (data zůstávají)
docker compose down              # odstranění kontejnerů (volume pgdata zůstává)
```

| Služba | Port (host) | Poznámka |
|---|---|---|
| frontend | **8080** | nginx, SPA + `/manual/` wiki |
| api | **8000** | FastAPI, OpenAPI na `/docs` |
| postgres | **55432** | ⚠️ záměrně ne 5432/5433 — na vývojovém PC běží nativní PostgreSQL na obou |
| engine | — | bez portu; TWS přes `host.docker.internal:7496` |

Data: Parquet v `./data` (bind mount, sdílené engine↔API), PostgreSQL ve volume `pgdata`. Zálohovat stačí `./data` + `pg_dump` (hlavně tabulku `oi_eod`, která se nikdy nemaže).

## 4. Konfigurace — kompletní reference

Zdroj: proměnné prostředí `GEXLENS_*` a `.env` (viz `.env.example`). Validuje se při startu — nevalidní hodnota = engine odmítne nastartovat se srozumitelnou chybou.

| Proměnná | Default | Význam |
|---|---|---|
| `GEXLENS_IBKR_HOST` | 127.0.0.1 | V compose přepsáno na `host.docker.internal` |
| `GEXLENS_IBKR_PORT` | 7496 | 7496 live / 7497 paper (TWS); 4001/4002 (Gateway) |
| `GEXLENS_IBKR_CLIENT_ID` | 1 | |
| `GEXLENS_MARKET_DATA_TYPE` | 1 | 1=live; delayed engine odmítá |
| `GEXLENS_CONNECT_TIMEOUT_S` | 10 | |
| `GEXLENS_RECONNECT_BACKOFF_BASE_S` / `_MAX_S` | 2 / 60 | Exponenciální reconnect |
| `GEXLENS_HEARTBEAT_INTERVAL_S` / `_TIMEOUT_S` | 30 / 15 | Heartbeat spojení; agresivnější hodnoty vedly k falešným reconnectům během sweep dávek |
| `GEXLENS_SYMBOLS` | ES | Základní sada futures podkladů (čárkami); watchlist z DB se přidává za běhu (ADR-0003) |
| `GEXLENS_MAX_INSTRUMENTS` | 3 | Strop souběžných instrumentů (rozpočet market data lines) |
| `GEXLENS_WATCHLIST_POLL_CYCLES` | 5 | Watchlist + runtime nastavení (strike_range_points) se čtou z DB každý k-tý cyklus |
| `GEXLENS_OI_ARCHIVE_EXPIRIES` | 5 | Ranní OI archiv pokrývá N nejbližších expirací (základ ΔOI vs. včera) |
| `GEXLENS_SWEEP_NEXT_EXPIRY` | true | Sekundární sweep následující expirace (positioning příští seance) |
| `GEXLENS_NEXT_EXPIRY_SWEEP_EVERY` | 3 | Kadence sekundárního sweepu (každá k-tá minuta) |
| `GEXLENS_STRIKE_RANGE_POINTS` | 200 | Výchozí denní obálka spot ± X (ADR-0002) |
| `GEXLENS_STRIKE_RANGE_EXPAND_THRESHOLD` | 0.25 | Rozšíření při přiblížení k okraji |
| `GEXLENS_STRIKE_RANGE_MAX_POINTS` | 800 | Strop šířky obálky (≥ 2× base) |
| `GEXLENS_BATCH_SIZE` | 80 | Dávka rotačních subskripcí |
| `GEXLENS_BATCH_TIMEOUT_S` | 4 | Čekání na kompletní data kontraktu |
| `GEXLENS_WINGS_SWEEP_EVERY` | 3 | Křídla každý k-tý cyklus |
| `GEXLENS_ATM_SWEEP_WIDTH` | 30 | ATM ± N strikes každý cyklus |
| `GEXLENS_REPAIR_MAX_ATTEMPTS` | 3 | Retry repair fronty za sweep |
| `GEXLENS_MARKET_DATA_LINES` | 100 | Kapacita lines (účet ≥ 150, ADR-0001) |
| `GEXLENS_HOT_ZONE_WIDTH` | 15 | Cílová šířka hot zóny (degraduje dle účtu) |
| `GEXLENS_TICK_BY_TICK_MAX_STREAMS` | 5 | Naměřený limit účtu (ADR-0001) |
| `GEXLENS_DATABASE_URL` | postgres localhost | V compose směřuje na službu `postgres` |
| `GEXLENS_DATA_DIR` | data | Kořen Parquet partic |
| `GEXLENS_RETENTION_DAYS` | 14 | Purge okno (oi_eod se nikdy nemaže) |
| `GEXLENS_DISK_LIMIT_GB` | 2 | Alert při překročení |
| `GEXLENS_RETENTION_PURGE_TIME_UTC` | 21:30 | Čas nočního purge |
| `GEXLENS_API_BASE` | http://127.0.0.1:8000 | Kam engine pushuje (v compose `http://api:8000`) |

Frontend build-time: `VITE_API_BASE` (nginx build arg, default `http://127.0.0.1:8000`).

## 5. Engine — datová pipeline

Minutový cyklus (`runtime.EngineRuntime.run_cycle`):

1. **Sweep** — `SubscriptionScheduler` projede řetězec v dávkách (ATM±30 každý cyklus, křídla každý 3.), nekompletní kontrakty přes repair frontu, výsledek do in-memory cache.
2. **Snapshot** — cache → `SnapshotRow` (OI z ranního archivu) → atomický zápis Parquet.
3. **Výpočty** — GEX per strike (naivní dealer model, vyměnitelná strategie) → levels (flip interpolovaně, walls, centroid) → zápis do `derived/levels`.
4. **Cum Δ** — bar větev (ΔVol × midpoint test × Δ × M); hot zóna tick-by-tick přispívá průběžně. `close_minute` → `derived/flow`.
5. **Bary podkladu** — 5s reqRealTimeBars agregované na 1min → `derived/bars`.
6. **Push do API** — `/internal/status` + kanály `levels.*`, `flow.*`, `price.*`.

### Multi-instrument orchestrátor (ADR-0003)

`__main__` řídí **pipeline per podklad** (`instruments.InstrumentPipeline`): cílová sada = `GEXLENS_SYMBOLS` ∪ watchlist z DB — změny chodí okamžitě přes PostgreSQL `LISTEN/NOTIFY` (kanál `gexlens_watchlist`, #207: API po zápisu notifikuje, orchestrátor se probudí ze sleep a nový symbol startuje do sekund; svíčky dne doplní backfill z #221), poll à `WATCHLIST_POLL_CYCLES` zůstává jako fallback pro backendy bez NOTIFY. Probuzení uprostřed minuty spustí plný cyklus jen pro nové pipeline — běžící by duplikovaly zápisy. Sweepy instrumentů běží **sekvenčně** — špička market data lines je vždy jedna dávka. Multiplikátor a burza se čtou z contract details. Ne-futures symbol → alert `instrument_error` + cooldown 30 cyklů. Pád cyklu jednoho instrumentu neshodí ostatní; status se agreguje (součty Greeks/repair, pole `symbols`).

Každá pipeline navíc drží **sekundární runtime následující expirace** (`secondary=True`): sweep v kadenci `NEXT_EXPIRY_SWEEP_EVERY`, zapisuje jen snapshots + levels své expirace (flow/bary patří výhradně aktivnímu řetězu — soubory jsou per symbol).

Další joby: **OI archiv** při startu + retry à 30 min dokud den nemá data (alert `oi_missing`); pokrývá `OI_ARCHIVE_EXPIRIES` nejbližších expirací — základ ΔOI vs. včera. **POZOR: OI se čte přes generic tick 101 i pro FOP** (tick 588 na FOP nedodává nikdy — ADR-0001 v3; hodnota se čte podle strany kontraktu, opačná strana je validní 0.0). **Auto-rozšíření obálky strikes** (grow-only, capped → alert) + runtime změna `strike_range_points` ze Settings UI (překlopí pipeline). **Denní roll expirace**: vypršelá pipeline se zastaví a další cyklus založí novou s čerstvou discovery (bezobslužný přechod přes víkend). **Noční retention purge** po `RETENTION_PURGE_TIME_UTC`.

Bary podkladu (#221): **Backfill 1min barů** při startu pipeline (aktuální den + retention okno, reqHistoricalData pod pacing guardem, upsert podle ts_min — živý stream a backfill se nedublují). **Hlídání tiché ztráty barů** (`BarsStallDetector`): když ≥ `BARS_STALL_ALERT_MINUTES` (default 3) nedorazí žádný 5s bar, ale spot se hýbe, odejde alert `bars_stalled` (typicky mrtvé TWS farmy po noční přestávce — pomáhá restart TWS); po návratu streamu alert `bars_recovered` + automatický re-backfill dnešního dne doplní díru. Bez pohybu spotu (zavřený trh) se nehlásí nic.

Odolnost: ConnectionManager watchdog (heartbeat 30/15 s + exponenciální reconnect + plná resubskripce — **vč. spot tickeru a realtime barů podkladu** přes `on_resubscribe`), spot fallback last → marketPrice → close (start i o víkendu), discovery s timeoutem a retry (sec-def farm výpadky), výjimka v cyklu nikdy neshodí smyčku, pacing guard historical requestů (≤60/10 min, dedup, priorita).

## 6. Datové formáty a persistence

### Parquet (`GEXLENS_DATA_DIR`, retence 14 dní)

| Partice | Schéma |
|---|---|
| `snapshots/{sym}/{expiry}/{YYYY-MM-DD}.parquet` | ts_min, strike, right, bid, ask, last, volume, iv, delta, gamma, theta, vega, oi, stale_age |
| `ticks/{sym}/{YYYY-MM-DD}.parquet` | ts, conId, price, size, side |
| `derived/{sym}/{expiry}/levels/{date}.parquet` | ts_min, flip, call_wall, put_wall, centroid, total_gex |
| `derived/{sym}/flow/{date}.parquet` | ts_min, flow_delta, cum_delta |
| `derived/{sym}/bars/{date}.parquet` | ts_min, open, high, low, close, volume |

Zápis je **atomický** (temp + rename) — po pádu procesu nikdy nezůstane částečný soubor; osiřelé `.tmp` se uklízí při dalším zápisu. Writer po restartu navazuje na rozepsaný den.

### PostgreSQL

| Tabulka | Účel |
|---|---|
| `oi_eod(symbol, expiry, strike, right, date, oi)` | **Věčný** OI archiv — žádná retence, žádné delete API |
| `watchlist`, `alerts`, `annotations`, `settings` | CRUD přes API |

## 7. API reference

Interaktivní dokumentace: `http://127.0.0.1:8000/docs` (OpenAPI).

### REST

| Endpoint | Popis |
|---|---|
| `GET /health`, `GET /status` | Liveness; agregovaný stav pipeline |
| `GET /instruments`, `GET /instruments/{sym}/expiries` | Dostupné symboly/expirace (ze storage) |
| `GET /instruments/{sym}/days` | Uložené dny s expirací per den (Daily pohled) |
| `GET /profile/{sym}/aggregate?date` | Σ profil: OI/volume sečtené přes všechny expirace dne per strike (registrováno PŘED /profile/{sym}/{expiry}) |
| `GET /snapshots/{sym}/{expiry}?date&mode&scale&norm&from&to&raw` | Heatmap matice jako **Arrow IPC stream**; `raw=true` = surová partice |
| `GET /levels/{sym}/{expiry}?date` | Časová řada flip/walls/centroid |
| `GET /profile/{sym}/{expiry}?date&ts&variant&oi_weight&spot` | Strike profil k okamžiku |
| `GET /flow/{sym}?date` | CumΔ + OptVol + Vol řady |
| `GET /replay/{sym}/{expiry}/{date}` | Kompletní denní balík (levels/flow/bars JSON + snapshoty base64 Arrow + `oi_prev` pro ΔOI vs. včera) |
| CRUD `/watchlist`, `/alerts`, `/annotations?symbol&date`, `/settings` | PostgreSQL persistence |
| `POST /internal/status`, `POST /internal/publish` | **Ingest z enginu** (nechráněné — API bindí jen na localhost) |

### WebSocket `/ws/live`

Protokol: klient pošle `{"action":"subscribe","channels":["status","price.ES","levels.*"]}` (podpora trailing wildcard), server vrací ack a pushuje `{"channel":..., "data":...}`. Backpressure: fronta 100 zpráv per klient, při zaplnění se zahazují nejstarší framy. Kanály: `status`, `price.{sym}`, `snapshot.{sym}.{expiry}`, `levels.*`, `flow.*`, `alerts`, `news`.

## 8. Frontend

- **Heatmapa**: data → offscreen bitmapa (překreslení jen při změně dat/módu), pan/zoom = GPU `drawImage` → 60 fps nezávisle na objemu; overlay canvas kreslí vektory (cena/svíčky, levels, walls, sessions, crosshair, anotace).
- **Replay**: `/replay` se stáhne jednou, `apache-arrow` dekóduje snapshoty, celý den se předpočítá v paměti (vč. profilu per minuta) — přetáčení je čisté krájení typed arrays. Timestampy se normalizují (`canonicalTs` — Arrow epoch vs. JSON ISO).
- **Stav**: React kontexty `AppState` (status z WS + REST initial fetch, view, téma, alerty) a `Crosshair` (sdílený všemi panely).
- **OI fallback**: při nulovém OI staví heatmapu z volume (engine mezitím posílá alert `oi_missing`).
- Wiki/manuál: statické HTML v `frontend/public/manual/` (generované z MD, viz níže) — servíruje ho vite dev i nginx.

## 9. Vývojové prostředí

Prerekvizity: [uv](https://docs.astral.sh/uv/) (stáhne Python 3.12 sám), Node.js ≥ 20, Docker (pro PG integrační test lokálně volitelně).

```powershell
uv sync --all-packages                      # Python workspace (engine + api)
uv run ruff check .; uv run ruff format .   # lint/format
uv run mypy engine/src engine/tests api/src api/tests
uv run pytest                               # PG integrační test se přeskočí bez GEXLENS_TEST_PG_DSN

cd frontend; npm ci; npm run lint; npm test; npm run build
```

Dev servery: `make run-api` (uvicorn :8000), `make run-frontend` (vite :5173), `make run-engine` (vyžaduje TWS). CORS povoluje i :5173.

Regenerace manuálů (MD → HTML pro in-app wiki → PDF): `powershell scripts/build-manual.ps1` (vyžaduje Edge; PDF vzniká headless tiskem).

Konvence: feature branch `feat/{issue}-slug` / `fix/...`, PR s `Closes #N`, merge po zeleném CI. Výpočty vždy s golden testy v `engine/tests/golden/` (ručně spočtené hodnoty, výpočet dokumentovaný v `description`).

## 10. Testy a CI

- **Python** (~160): jednotkové + golden (GEX, levels, heatmap módy, walls, CumΔ, profil), mock-based integrační (scheduler, hot zóna, runtime), PG integrační (v CI přes service kontejner), **e2e smoke** — deterministický referenční den přes celou pipeline engine→storage→API proti golden hodnotám.
- **Frontend** (~58): jednotkové (geometrie, barvy, contours, slice), komponentové (jsdom + testing-library, PointerEvent polyfill), Arrow round-trip loaderu, **e2e render smoke** (App nad /replay balíkem), vizuální regresní snapshoty renderu.
- **CI** (GitHub Actions, na každý PR): python job (ruff, mypy strict, pytest + PostgreSQL service), frontend job (eslint, prettier, vitest, build). Výkonnostní testy s tvrdým limitem běží jen lokálně (`CI` env skip).

## 11. Známé limity účtu a otevřené body

Z [ADR-0001](../adr/0001-ibkr-account-limits.md) (měřeno živě na účtu):

| Limit | Hodnota | Dopad |
|---|---|---|
| Tick-by-tick streamy | **5** | Hot zóna degraduje z ATM±15 na ~ATM±1; zbytek klasifikuje midpoint test. Rozšíření = IBKR Quote Booster. |
| Market data lines | ≥ 150 | Dávka 80 má rezervu |
| **FOP OI** | **tick 588 nedodává nikdy; tick 101 funguje** | **VYŘEŠENO (issue #65, ADR-0001 v3):** `IbOIFetcher` používá generic tick 101 pro OPT i FOP a čte hodnotu podle strany kontraktu (opačná strana = validní 0.0). Retry à 30 min + volume fallback zůstávají jako pojistka. |

[ADR-0002](../adr/0002-strike-band-expansion.md): obálka strikes je grow-only (křídla se neztrácejí), strop šířky s alertem. [ADR-0003](../adr/0003-multi-instrument.md): multi-instrument orchestrace řízená watchlistem.

## 12. Diagnostika a údržba

| Situace | Postup |
|---|---|
| Engine offline | `docker compose logs engine` — hledej stav ConnectionManageru; ověř TWS (API zapnuté, port, Trusted IP). Warning 2110/2103 = výpadek TWS↔IB, vyřeší se sám. |
| Prázdné GEX/walls | Zkontroluj `oi_eod` pro dnešek: `docker compose exec postgres psql -U gexlens -c "select date, count(*) from oi_eod group by 1 order by 1 desc limit 5"` — pokud dnešek chybí, engine archiv opakuje à 30 min (CME publikuje OI ráno). |
| Ticker z watchlistu nesbírá | `docker compose logs engine | grep Setup` — ne-futures symbol nebo chybějící subskripce burzy (NYMEX/COMEX pro CL/GC); cooldown 30 min mezi pokusy. |
| Vysoké `Repair` / `Stale` | Konkrétní kontrakty bez dat — často nelikvidní křídla; zvyš `BATCH_TIMEOUT_S` nebo zmenši obálku. |
| Disk roste | Retention běží nočně; ručně: smaž staré partice v `./data` (nikdy `oi_eod`). |
| Reset prostředí | `docker compose down`, smaž `./data` (přijdeš o 14denní okno, ne o OI archiv ve volume `pgdata`), `docker compose up -d --build`. |
| Málo dat po restartu | Writer navazuje na rozepsaný den — mezera zůstane jen za dobu výpadku. |
| Změna portu TWS | Settings v aplikaci, nebo `.env` + `docker compose up -d engine`. |

## 13. Zprovoznění od nuly — IBKR účet, TWS/Gateway

Jednorázový onboarding pro nové prostředí (převzato z issue #1, kde vznikl a byl
odškrtán při prvním zprovoznění 16. 7. 2026). Bez těchto kroků se engine
nepřipojí, nebo dostane jen delayed data, která záměrně odmítá (SPEC 3.1 —
Greeks z delayed dat nejsou spolehlivé).

### 13.1 Market data subskripce (Client Portal)

1. <https://www.interactivebrokers.com> → **Log In → Portal** (IBKR login + IB Key).
2. **Settings → User Settings → Market Data Subscriptions** → Configure (ozubené kolo).
3. **North America → Futures → CME Real-Time (NP,L2)** — pokrývá ES/NQ futures
   i futures opce (FOP). Levná subskripce (~1,55 USD/měs.) prokazatelně stačí
   (ověřeno živě, ADR-0001).
4. Zkontroluj status **Non-Professional** (jinak výrazně vyšší poplatky).
5. *(Až pro SPY/SPX — sekundární scope)*: **OPRA (US Options Exchanges)**,
   pro SPX index navíc **Cboe Streaming Market Indexes**.
6. Vývoj proti **paper účtu**: Settings → Account Settings → Paper Trading
   Account → *Share real-time market data with paper account* — jinak paper
   účet subskripce nevidí.

### 13.2 TWS nebo IB Gateway (musí běžet lokálně)

Engine se připojuje socketem na lokální TWS/Gateway (z kontejneru přes
`host.docker.internal`), ne přímo na servery IBKR.

**Varianta A — stávající TWS (nejrychlejší):**
Edit → Global Configuration → API → Settings → ✅ *Enable ActiveX and Socket
Clients*, port **7496** live / **7497** paper, Trusted IPs `127.0.0.1`
(vypne potvrzovací dialog), *Read-Only API* nechat **zapnuté** — GEXLens jen
čte, nic neobchoduje.

**Varianta B — dedikovaný IB Gateway (doporučeno pro trvalý provoz):**

1. Stable Windows 64-bit: <https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>
2. Login obrazovka: režim **IB API**, Live/Paper, přihlášení s IB Key.
3. Configure → Settings → API → Settings: port přepsat z 4001/4002 na
   **7496/7497** (nebo nechat a upravit `GEXLENS_IBKR_PORT` v `.env`),
   Trusted IPs `127.0.0.1`, Read-Only API zapnuté.
4. Configure → Settings → Lock and Exit → **Auto restart**, čas mimo seanci
   (např. 23:00) — jinak se TWS/GW jednou denně sám odhlásí a engine ztratí
   spojení přes noc. Jednou týdně (neděle) je i tak nutné plné ruční
   přihlášení — omezení IBKR.

### 13.3 Konflikt jednoho přihlášení ⚠️

IBKR povoluje jedno přihlášení na username: Gateway + TWS (či mobil s trading
permission, druhé PC) se stejným loginem současně = Gateway spadne. Řešení:
druhý user v Client Portal (Settings → Account Settings → Users & Access
Rights) — pozor, market data subskripce se platí per user. Pro start stačí
varianta A.

### 13.4 Software na PC

- **Docker Desktop** s WSL2 backendem — celý stack (PostgreSQL, api, engine,
  frontend) běží přes `docker compose up -d` (kap. 3). Bez Dockeru: Python
  3.12, Node.js ≥20, PostgreSQL 16 + `make run` (kap. 9).
- Volné místo: ~1 GB pro 14denní datové okno; WSL2 limit paměti viz
  `C:\Users\<user>\.wslconfig` (`[wsl2] memory=6GB` — pojistka proti
  nafouknutí vmmem).

### 13.5 Ověření a denní provoz

```powershell
Test-NetConnection 127.0.0.1 -Port 7496   # TcpTestSucceeded: True = API poslouchá
```

Každý obchodní den: TWS/Gateway běží a je přihlášený **před startem enginu**;
stavová lišta aplikace ukazuje `connected :7496` a `● Live` (ne Offline).
Diagnostika problémů: kap. 12.

---

*Interní dokument. Uživatelská příručka: `UZIVATELSKY-MANUAL.md` (dostupná i v aplikaci jako Wiki).*
