# ADR-0013: SentimentLens — ForexFactory kalendářní feed

**Stav:** accepted · **Datum ověření:** 2026-07-27 · **Zdroj:** `https://nfs.faireconomy.media/ff_calendar_thisweek.json` (feed ForexFactory kalendářního widgetu).

Řeší ověřovací bod 2 SPEC SentimentLens (`docs/Sentiment/sentiment-SPEC-v1.md`, kap. 1 Tier A). Issue #267.

## Zjištění

- Feed funguje bez klíče a bez speciálních hlaviček (stačí běžný User-Agent); ~12 kB, 90 eventů aktuálního týdne (od neděle).
- Pole eventu: `title`, `country` (kód měny: USD, EUR, …), `date` (ISO 8601 **s explicitním offsetem** US Eastern vč. DST — timezone handling je bezpečný), `impact` ∈ {High, Medium, Low}, `forecast`, `previous` (stringy vč. jednotek: `3.4%`, `86.1`, i prázdné).
- **Feed NEOBSAHUJE pole `actual`** — ani u už proběhlých eventů (ověřeno na eventech z rána 27. 7.). Předpoklad SPEC „refresh 2 min po high-impact eventu kvůli actual" z tohoto feedu **nelze naplnit**.
- K dispozici je jen aktuální týden — `ff_calendar_nextweek.json` vrací 404. Eventy nemají žádné ID.

## Rozhodnutí

- **`actual` se získává z oficiálních API** (BLS/BEA/FRED — Tier A collectory je už mají) mapováním řady na kalendářní event; mapování `series_map` v konfiguraci. U eventů bez oficiálního API (ifo, PMI, …) zůstane `actual` NULL — směr případně dodá headline klasifikace (Tier B o releasu typicky reportuje).
- `source_uid` = hash(`country` + `title` + `date`) — feed nemá vlastní ID.
- Collector je defenzivní (formát negarantovaný): tolerantní pydantic parsování, neznámá pole do `raw`, změna struktury → stav `degraded`, nikdy pád (SPEC 3.2).
- Horizont „upcoming" (UI 9.3/9.5) je max do konce aktuálního týdne — pro countdown 24 h dostatečné.
- Fixture zachyceného payloadu: `docs/Sentiment/fixtures/forexfactory/ff_calendar_thisweek_2026-07-27.json` — základ golden testu normalizace v N1 (#271). Fixtures se při vzniku news-engine přesunou do jeho test suite.

## Důsledky

- **Historický kalendář feed neposkytuje** → zpětný `surprise_z` z FF forecastů nelze stáhnout přímo. Varianty pro backfill v N2 (#277):
  1. jednorázový defenzivní scrape webového kalendáře FF (historické stránky forecast+actual mají),
  2. aproximace forecast ≈ previous (dokumentovaná degradace kvality),
  3. surprise_z počítat až od startu live sběru; historie jen z FRED/BLS hodnot (bez konsensu).
  → rozhodnutí v issue #277 (needs-decision).
- Latence `actual` po releasu je dána BLS/BEA/FRED collectory, ne FF refreshem — požadavek „actual do 3 min" (kap. 10) se měří proti nim.
