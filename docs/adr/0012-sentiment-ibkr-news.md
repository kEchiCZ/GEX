# ADR-0012: SentimentLens — IBKR news providery a hloubka historie

**Stav:** accepted · **Datum měření:** 2026-07-27 · **Prostředí:** TWS live účet, port 7496, read-only sonda (clientId 93) spuštěná v engine kontejneru; front kontrakty ESU6 (conId 649180671), NQU6 (conId 770561204), SPY (SMART).

Řeší ověřovací body 1 a 4 SPEC SentimentLens (`docs/Sentiment/sentiment-SPEC-v1.md`, kap. 1). Issue #266.

## Naměřené hodnoty

### 1. Aktivní news providery (`reqNewsProviders()`)

Na účtu je **8 providerů** — očekávané free trio (BRFG, BRFUPDN, DJNL) plus celá rodina Dow Jones:

| Kód | Název |
|---|---|
| BRFG | Briefing.com General Market Columns |
| BRFUPDN | Briefing.com Analyst Actions |
| DJ-N | Dow Jones Global Equity Trader |
| DJ-RT | Dow Jones Trader News |
| DJ-RTA | Dow Jones Top Stories Asia Pacific |
| DJ-RTE | Dow Jones Top Stories Europe |
| DJ-RTG | Dow Jones Top Stories Global |
| DJNL | Dow Jones Newsletters |

### 2. Hloubka `reqHistoricalNews` per provider × kontrakt

Stránkování po 300 headlines, strop 30 stránek — **nikde nedosažen, každý zdroj se vyčerpal už v 1. stránce** (vrátil < 300 položek):

| Symbol | Provider | Headlines | Rozsah |
|---|---|---|---|
| ES (ESU6) | DJ-N / DJ-RT / DJ-RTG | 7 (identické) | 2026-02-12 → 2026-05-06 |
| ES (ESU6) | ostatní | 0 | — |
| NQ (NQU6) | všechny | 0 | — |
| SPY | DJ-N / DJ-RT / DJ-RTG | 112–113 | 2026-01-28 → 2026-07-22 (~6 měsíců) |
| SPY | BRFG | 2 | 2024-11-06 → 2025-07-08 |
| SPY | DJ-RTA / DJ-RTE | 1 | 2026-07-08 |
| SPY | BRFUPDN / DJNL | 0 | — |

Headlines jsou tagované per conId konkrétního kontraktu — rolované futures kontrakty proto historii prakticky nenesou (ESU6 existuje jako front krátce, NQU6 nemá nic).

## Rozhodnutí

- **Potvrzeno rozhodnutí SPEC v1.3 (Tier D, kap. 3.4):** `reqHistoricalNews` je best-effort doplněk. Na tomto účtu dodá řádově ~100 headlines (SPY, ~6 měsíců); primární počáteční trénovací dataset je historický kalendář ForexFactory + FRED/BLS řady.
- Backfill v N2 (#278) poběží **primárně přes SPY conId** jako proxy indexových headlines; ES/NQ conId jen pro úplnost (jednotky položek). DJ-N/DJ-RT/DJ-RTG vrací identické sady → stahovat jen jeden z nich (DJ-RTG) a dedupovat.
- Live headlines (tick 292, N6 #291) mají k dispozici všech 8 providerů vč. DJ Top Stories Global/Europe/Asia — dobré live pokrytí macro headlines.

## Důsledky

- Počáteční headline dataset bude malý — gate Signal enginu (SPEC 6.2) pro headline buckety naběhne až z live sběru; hlubší historii mají jen scheduled eventy (FF/FRED/BLS).
- Při změně news subskripcí na účtu přeměřit: sonda `news_probe.py` (reqNewsProviders + stránkovaná hloubka per provider, postup dle tohoto ADR) — jednorázová, běží v engine kontejneru.
