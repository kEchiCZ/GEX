# ADR-0003: Multi-instrument podpora enginu

**Stav:** přijato (2026-07-17, na přímý požadavek product ownera)
**Kontext:** SPEC v2.0 popisuje pipeline nad ES futures opcemi a v UI počítá
s watchlistem a přepínáním tickerů (kap. 7.1, 7.5), ale engine-side chování
pro více instrumentů nespecifikuje.

## Rozhodnutí

1. **Cílová sada instrumentů = `GEXLENS_SYMBOLS` (základ, default `ES`) ∪ watchlist z DB.**
   Watchlist edituje uživatel v sidebaru (CRUD /watchlist); engine ho čte každý
   `watchlist_poll_cycles`-tý minutový cyklus a pipeline startuje/zastavuje za běhu.
   Odebrání z watchlistu zastaví sběr; základ z konfigurace běží vždy.

2. **Pipeline per instrument, sweepy sekvenčně.** Každý podklad má vlastní
   `InstrumentPipeline` (discovery, denní obálka, scheduler cache, CumΔ tracker,
   OI archiv, 1m bary). Minutové cykly běží za sebou, takže špička market data
   lines zůstává jedna dávka (`batch_size`) bez ohledu na počet instrumentů —
   limit účtu (ADR-0001) se nedělí. Cena: cyklus trvá ~0.5 s × počet instrumentů,
   což se do minutového rytmu vejde i při stropu.

3. **Strop `max_instruments` (default 3).** Persistentní subskripce podkladu
   (mkt data + realtime bary) a rotační dávky rostou s počtem instrumentů;
   strop chrání lines rozpočet. Symboly nad strop se logují a neběží
   (priorita = pořadí: základ z konfigurace, pak watchlist).

4. **Podporované podklady: futures s FOP řetězcem** (ES, NQ, RTY, CL, GC, …).
   Multiplikátor a burza se čtou z contract details (žádná statická mapa).
   Akcie/indexy (STK/IND → OPT) zatím nepodporujeme — discovery podkladu hledá
   jen futures; nepodporovaný symbol vyvolá alert `instrument_error` a cooldown
   30 cyklů. Rozšíření na akciové opce by vyžadovalo vlastní resolver podkladu
   a validaci subskripcí dat (samostatné ADR, až bude potřeba).

5. **Selhání jednoho instrumentu neshazuje ostatní.** Výjimka v cyklu jednoho
   symbolu se zaloguje a pokračuje se dalším; setup chyby mají cooldown.
   Status pipeline se agreguje (Greeks/repair součty, lines max, pole `symbols`).

## Důsledky

- API a frontend se nemění (cesty i kanály jsou už per symbol).
- Retence a noční purge zůstávají globální; `oi_eod` se dál nikdy nemaže (R4).
- Alert `oi_missing` chodí per symbol.
- Retention disk limit platí souhrnně — více instrumentů = rychlejší růst dat;
  případné navýšení `disk_limit_gb` je na uživateli.
