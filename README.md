# GEXLens

Vizualizace opčního positioningu (GEX/OI/Vol heatmapa) nad ES futures opcemi. Jediný datový zdroj: **Interactive Brokers TWS/Gateway API**.

- **Zadání / zdroj pravdy:** [docs/SPEC.md](docs/SPEC.md) (v2.0) · ADR v [docs/adr/](docs/adr/)
- **Instrukce pro vývoj (Claude Code):** [CLAUDE.md](CLAUDE.md)

## Architektura

`TWS/IB Gateway → engine (Python, ib_async) → Parquet + PostgreSQL → API (FastAPI, REST + WS) → frontend (React, canvas heatmapa)`

## Od čistého stroje k běžící aplikaci

### 1. Prerekvizity (jednorázově)

1. **IBKR účet + market data**: v Client Portal aktivní subskripce *CME Real-Time – North America* (stačí levná L1 varianta — ověřeno, viz ADR-0001). Detailní postup: issue #1.
2. **TWS nebo IB Gateway běží na tomto PC** a je přihlášené:
   - Enable ActiveX and Socket Clients, Trusted IP `127.0.0.1`
   - port `7496` live / `7497` paper (nastav v `.env`, viz níže)
   - u IB Gateway zapni Auto restart (headless provoz — SPEC kap. 8)
3. **Docker Desktop** (WSL2 backend na Windows).

### 2. Konfigurace

```powershell
Copy-Item .env.example .env
# uprav minimálně GEXLENS_IBKR_PORT (7496 live / 7497 paper)
```

### 3. Start

```powershell
docker compose up --build     # nebo: make run
```

Vytvoří a spustí: PostgreSQL (host port **55432** — záměrně ne 5432, aby nekolidoval s případným nativním PostgreSQL), API server (`http://127.0.0.1:8000`), engine (připojuje se na TWS na hostiteli přes `host.docker.internal`) a frontend (**http://127.0.0.1:8080**).

Otevři **http://127.0.0.1:8080** — stavová lišta dole musí do minuty ukázat `IBKR: connected` a `Greeks X/Y`. Data (Parquet partice) se ukládají do `./data`.

### Řešení potíží

| Symptom | Příčina |
|---|---|
| Stavová lišta `offline` | Engine se nepřipojil k TWS — zkontroluj, že TWS běží, API je povolené a port v `.env` sedí |
| `delayed market data` alert | Chybí live subskripce CME (Client Portal) |
| Prázdná heatmapa | Mimo obchodní hodiny ES nejsou nové snapshoty; použij playback/replay |

## Vývoj

Prerekvizity: [uv](https://docs.astral.sh/uv/) (stáhne si Python 3.12 sám), Node.js ≥ 20.

Linux/macOS/Git Bash: `make test` (lint + testy všeho), `make run-api`, `make run-frontend`, `make run-engine`.

Windows (PowerShell) ekvivalenty:

```powershell
# Python (engine + api)
uv sync --all-packages
uv run ruff check .
uv run mypy engine/src engine/tests api/src api/tests
uv run pytest

# Frontend
cd frontend; npm ci; npm run lint; npm test; npm run build

# Dev servery
uv run --package gexlens-api uvicorn gexlens_api.main:app --reload --port 8000
cd frontend; npm run dev
uv run python -m gexlens_engine   # vyžaduje běžící TWS
```

CI (GitHub Actions) spouští lint + testy obou částí na každý PR; Python job má PostgreSQL service pro integrační testy.
