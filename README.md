# GEXLens

Vizualizace opčního positioningu (GEX/OI/Vol heatmapa) nad ES futures opcemi. Jediný datový zdroj: **Interactive Brokers TWS/Gateway API**.

- **Zadání / zdroj pravdy:** [docs/SPEC.md](docs/SPEC.md) (v2.0)
- **Instrukce pro vývoj (Claude Code):** [CLAUDE.md](CLAUDE.md)
- **Milestones:** M1 Datová vrstva → M2 Výpočty → M3 API → M4 Frontend → M5 Provozní celek

## Stack

Python 3.12 + ib_async (engine) · FastAPI + WebSocket (API) · PostgreSQL + Parquet (storage) · React + TypeScript + Vite (frontend, canvas/WebGL heatmapa)

## Než začneš

Co je potřeba nainstalovat, nastavit a kam se přihlásit (TWS/IB Gateway, market data subscriptions, Docker, …) popisuje issue **„Setup uživatelského prostředí"** v GitHub Issues.
