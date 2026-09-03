# ADR-0027: tastytrade/dxFeed feed — naměřené limity a provozní hodnoty

- **Stav:** přijato
- **Datum:** 2026-08-14
- **Souvisí:** #612 (spike), #610 (epic M7), ADR-0025 (přístup a bezpečnost),
  ADR-0001 (limity IBKR — protějšek tohoto dokumentu)

## Kontext

Fáze 0 epicu M7: před stavbou tastytrade větve bylo nutné změřit, co feed
reálně umí — entitlementy, eventy, limity, provozní chování. Vzor ADR-0001.
Měřeno 13.–14. 8. 2026 živě (RTH i Globex) sondou `scripts/tasty_probe.py`
(OAuth2 refresh flow výhradně, dev grant) a prvním provozem shadow módu.

## Naměřené hodnoty

| co | hodnota | poznámka |
|---|---|---|
| OAuth2 access token | platnost 900 s, refresh flow funguje | obnova s předstihem 3 min (`tasty/session.py`) |
| Quote token | level `api`, DXLink `tasty-openapi-dxlink-md-ws.dxfeed.com` | z `/api-quote-tokens` |
| Účet | non-professional | data bez příplatku |
| ES řetěz | 55 expirací, 0DTE ~389 striků | IBKR obálka: 80 kontraktů |
| Streamer symbol | `./E2DQ26C7975:XCME` | VÝHRADNĚ z chain endpointu — viz past níže |
| Quote event | ✅ medián mezery 0,2–0,75 s (ATM) | bid/ask/size plné |
| Greeks event | ✅ IV+delta+gamma+theta+vega+theo | model dxFeed |
| Summary.openInterest | ✅ plní se pro FOP | **křížová kontrola: 50/50 kontraktů identických s IBKR tick 101** |
| TimeAndSale.aggressorSide | ✅ BUY/SELL/UNDEFINED | základ R2 (#615); pokrytí doplní shadow report |
| Candle historie FOP | k zalistování kontraktu (~2 měsíce u weekly) | pro #617 stačí (intraday týž den) |
| Symboly na 1 subskripci | **6 008 bez degradace** (8 expirací, jen Quote) | strop tehdy nenalezen; 60× IBKR limit |
| **Strop velikosti subskripce** (doplněno 2. 9. 2026, #982) | **25 000 položek symbol × event na spojení** — shodně pro samotné Quote i pro 4 eventy produkce (= 6 250 symbolů) | `tasty_probe.py sizecap`; nad stropem `ERROR BAD_ACTION 'Your subscription size is too big'` a odmítnuté symboly **tiše mlčí** |
| REST rate limit | ≥ 6,2 req/s sekvenčně bez 429 | strop nenalezen (185 req/30 s) |
| DXLink keepalive | klient MUSÍ posílat à ≤ 60 s | posíláme à 25 s |
| Reconnect | SDK neexistuje → vlastní backoff 1→60 s | `tasty/stream.py` |
| Souběh s IBKR na stanici | ✅ bez interference | nezávislé kanály |
| Mobilní tastytrade app souběžně | ✅ **feed nepřetahuje**: pokrytí 99,81 %, 0 reconnectů, 0 chyb | opak IBKR 10197 |
| Shadow zátěž | CPU enginu ~3,5 % klid, ~2 560 řádků/min (ES+NQ) | RAM +90 MB |

## Pasti (doložené měřením)

1. **Dekádová kolize futures symbolů:** `/ESU6:XCME{=1d}` s hlubokým
   `fromTime` vrací candle z roku **2016**. Backfill (#617) musí používat
   symboly s plným rokem (`/ESU26`); mapování kontraktů se NIKDY neskládá
   ručně — jen z chain endpointu.
2. **Bez klientského KEEPALIVE server odpojuje po ~60 s** (kód 1000 „Bye",
   bez chybové hlášky).

## Rozhodnutí — provozní hodnoty (limity na maximum, ne konzervativně)

Smysl druhého zdroje je odstranit strop, ne přinést nový (#612 zadání):

- **Šířka subskripce:** celé sledované řetězce všech expirací týdne
  (~1 000–6 000 symbolů) v JEDNÉ subskripci; dávkování zpráv po 500.
  Vstup pro #616: pokrytí přestává být limit feedu.
- **REST kadence:** 4 req/s provozně (změřený strop ≥ 6,2; 429 handling
  přesto povinný — strop nebyl nalezen, ne vyvrácen).
- **Spojení:** 1 trvalé DXLink spojení per engine; reconnect exponenciálně.
- **OI:** tasty Summary je plnohodnotný zdroj OI pro FOP (dokázaná shoda
  s tick 101) — fallback i validátor pro #614; navíc dostupný průběžně,
  bez ranního snapshot průchodu.

## Doplněk 2. 9. 2026 — rozpočet subskripce (#982)

Strop 25 000 položek platí per spojení a počítá se `symbol × typ eventu`.
Produkce jela na 24 944 položkách (6 236 symbolů × 4 eventy) — vešla se o
56 položek; ad-hoc pohled z vyhledávání (#521 C, +307 symbolů) přetekl a
nikdy nedostal data. Rozhodnutí (`tasty/budget.py`):

- **Eventy per účel:** podklad a aktivní řetěz všechny 4; ad-hoc a extended
  Quote + Greeks + Summary (printy z nich nikdo nečte); wide jen Quote +
  Summary (jde o OI, Quote drží symbol „živý" pro heal #936). Produkce tím
  klesá na ~18 400 položek bez ztráty pokrytí.
- **Rezerva pro ad-hoc** `GEXLENS_TASTY_ADHOC_RESERVE_ENTRIES=2000`
  (~2 pohledy): wide/extended smí zabrat jen `strop − rezerva`.
- **Ořez deterministický:** když se plán přesto nevejde, ubírá se extended od
  nejvzdálenější expirace a striku (v % ceny napříč produkty), pak wide od
  okraje pásma; řetěz, podklad a ad-hoc se neořezávají. Ořez i přetečení
  hlásí `/status.tasty_budget` a log.

## Doplněk 3. 9. 2026 — spread legy nejsou rozlišitelné (#615)

**Měření (23. 8. – 1. 9., 500 080 TimeAndSale printů ES+NQ):** `spreadLeg`
false ve 100 % případů, nikdy null, `aggressorSide` určen v 99,95 %.
**Podpora tastytrade po ověření u dxFeed potvrdila:** příznak spread legu
**není pro CME futures opce podporovaný — je to omezení CME Market Data —
a žádné alternativní pole neexistuje.** Ani IBKR tick-by-tick takový příznak
nenese.

**Rozhodnutí (uživatel, 3. 9. 2026):**

1. Fáze 3 (#615, plná klasifikace agresora přes TimeAndSale) pokračuje
   **bez rozlišování spread legů**. Nohy spreadů jsou v CumΔ stejně jako
   v dnešní IBKR tick-by-tick řadě — není to regrese, je to vlastnost venue.
   Track record rozlišuje období před/po změnou vstupu (`mechanics_version`).
2. Paralelní řada „bez spreadů" (`cum_ring_outright`) a podíl
   `spread_volume_share` **zrušeny** ze stínového CumΔ, `/status` i schématu
   `cumdelta_dx` — hodnota 0,0 % byla výstupem mrtvého čidla, ne měřením.
   Starší partice sloupce mají a čtou se dál; `trades_recorder` surový
   příznak dál ukládá (záznam feedu, ne odvozená veličina).
3. Heuristika (shodný čas a velikost napříč striky) **zamítnuta** — jen
   odhad bez možnosti ověření; nic se na ní nestaví.

## Doplní shadow report (~21. 8., 5 čistých seancí)

- párovaná latence IBKR × tasty per pole (z `feed_comparison` age sloupců),
- pokrytí `aggressorSide` (% tradů se stranou) a chování `spread_leg`,
- prahy hystereze pro přepínání zdrojů (#614).

## Verdikt

**GO pro fáze 1–5.** Všechny klíčové eventy chodí (včetně OI a agresora,
které IBKR nedává), šířka i kadence bez nalezeného stropu, mobilní souběh
bez konfliktu. Žádná fáze epicu nepadá.
