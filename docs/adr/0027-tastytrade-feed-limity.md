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
| Symboly na 1 subskripci | **6 008 bez degradace** (8 expirací) | strop nenalezen; 60× IBKR limit |
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

## Doplní shadow report (~21. 8., 5 čistých seancí)

- párovaná latence IBKR × tasty per pole (z `feed_comparison` age sloupců),
- pokrytí `aggressorSide` (% tradů se stranou) a chování `spread_leg`,
- prahy hystereze pro přepínání zdrojů (#614).

## Verdikt

**GO pro fáze 1–5.** Všechny klíčové eventy chodí (včetně OI a agresora,
které IBKR nedává), šířka i kadence bez nalezeného stropu, mobilní souběh
bez konfliktu. Žádná fáze epicu nepadá.
