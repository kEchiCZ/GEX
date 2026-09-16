# ADR-0040: Paper účet uvnitř GEXLens — simulátor filů místo demo účtu brokera

- **Stav:** přijato (uživatel 16. 9. 2026, #1187 varianta A)
- **Datum:** 2026-09-16
- **Souvisí:** #1187 (epic kouč), #1185/ADR-0038 (risk framework), #932/#933 (deník, kouč), #934 (live gate), #737 (druhý IBKR username), #924–#930 (Autopilot), ADR-0004 (setupy)

## Kontext

Kouč (#1187) potřebuje deník plněný z filů, ne z toho, co uživatel klikl.
Paper účet IBKR pod týmž username přetáhne market data živému enginu
(souběh relací, #451/#495) — použitelný bude až s druhým username (#737,
poplatky za data). tastytrade sandbox testuje kód exekuce, ne realističnost
filů, a je to jiný broker než cílový (IBKR).

## Rozhodnutí

1. **Paper účet žije v aplikaci.** Ordery zadává uživatel v GEXLens
   (`POST /paper/orders`), fily simuluje engine (`gexlens_engine.paper`)
   proti 1min barům podkladu: market na open dalšího baru + 1 tick proti,
   limit při protnutí úrovně (gap = open), stop vstup max(úroveň, open)
   + tick, stop-first uvnitř jednoho baru (jako `evaluate_bar` setupů),
   výstup na stop s tickem proti, cíl přesně. Vše denní: v settle seance
   se pozice zavřou na close a čekající ordery zruší.
2. **Risk vrstva blokuje, nevaruje** (rozhodnutí uživatele): order nad
   rozpočtem (equity × `risk_pct`, strop `risk_max_pct` z parametrů setupů,
   ADR-0038), po denní/týdenní brzdě z realizovaných paper obchodů nebo při
   kill switchi → 409 s důvodem a maximem kontraktů. Sizing počítá z živého
   equity účtu (fixed-fractional), ne z konstanty.
3. **Účet** v jednotkách plného kontraktu (ADR-0038: 1 kontrakt = 1 mikro
   reálně): start 50 000 $, vklady/výběry jako události (`paper_events`),
   equity = start + toky + Σ P/L po poplatcích (`fee_per_contract_usd`).
4. **Deník**: uzavřený obchod engine zapíše jako `obchod` s tagem `paper`
   (plán vs. realita, MFE/MAE, P/L, poplatky, kontext orderu) — vstup
   kouče v1 (#933).
5. **Jedna pozice/order na symbol** (fáze 1); kill switch zastaví účet,
   zavře pozice na dalším baru a zruší čekající ordery.
6. Broker vrstva IBKR (skutečné ordery přes relaci enginu, `placeOrder` +
   `execDetails`) až před live gate (#934) po #737; UI order ticketu v grafu
   je fáze 2 (#1187).

## Důsledky

- Fily jsou model (bez fronty, konzervativní slippage) — kouč hodnotí
  rozhodnutí a disciplínu, ne mikrostrukturu; live gate zůstává (#934).
- Nové tabulky `paper_accounts`, `paper_events`, `paper_orders`; alert
  `paper` (push kategorie setup); WS kanál `paper.{symbol}`.
