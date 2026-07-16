# GEXLens — instrukce pro Claude Code

## Kontext projektu
Aplikace pro vizualizaci opčního positioningu (GEX/OI/Vol heatmapa) nad ES futures opcemi.
**Jediný zdroj pravdy je `docs/SPEC.md` (v2.0)** — před každou prací si přečti relevantní kapitolu.
Závazná rozhodnutí R1–R6 ve SPEC kap. 0 se nesmí porušit (žádná MVP zjednodušení, plná klasifikace
agresora, retence 14 dní s výjimkou věčného OI archivu, jediný datový zdroj IBKR).

## Stack a struktura
- `engine/` — Python 3.12, ib_async, asyncio. Datový engine (SPEC kap. 3, 4, 5).
- `api/` — FastAPI + WebSocket (SPEC kap. 6).
- `frontend/` — React + TypeScript + Vite, heatmapa canvas/WebGL (SPEC kap. 7).
- `docs/` — SPEC.md, ADR záznamy pro odchylky.
- Python: ruff + mypy strict, pytest. TS: eslint + prettier, vitest.

## Pravidla práce
1. Pracuj po jednotlivých GitHub issues; každé issue = jedna feature branch `feat/{issue-number}-slug`, PR odkazuje `Closes #N`.
2. Respektuj milestones M1→M5 (pořadí závislostí). Neimplementuj napřed věci z pozdějších milestones.
3. Výpočty (GEX, levels, walls, CumΔ) musí mít jednotkové testy proti golden datasetu v `engine/tests/golden/`.
4. IBKR volání nikdy netestuj proti live API v CI — použij mock vrstvu `engine/ibkr/mock.py`.
5. Žádné hardcoded credentials; konfigurace přes `.env` (viz `.env.example`).
6. Pokud SPEC něco nepokrývá, založ ADR v `docs/adr/` a označ PR labelem `needs-decision` — nerozhoduj mlčky.
7. Komentáře a dokumentace česky, identifikátory v kódu anglicky.

## Ověřovací issues (nutné před M1 dokončením)
SPEC kap. 10: tradingClass weeklies, OI tick 588 na FOP, limit tick-by-tick streamů,
limit market data lines. Výsledky zapiš do `docs/adr/0001-ibkr-account-limits.md`.
