# ADR-0039: Kvartální expirační týden — front kontrakt podle roll date, SOQ settle kvartální expirace, kalendář v UI

- **Stav:** přijato (uživatel 16. 9. 2026, #1189 body B + C-lite + D)
- **Datum:** 2026-09-16
- **Souvisí:** #1189, #1018 (CumΔ verdikt — data roll týdne nepoužitelná), #576/#1114 (gamma útes), #519 (Forward GEX), #1173 (scénář dne), ADR-0001/ADR-0003 (front future), ADR-0023 (hranice seance)

## Kontext

Engine volil front futures kontrakt jako **nejbližší nepropadlý** (sort podle
`lastTradeDateOrContractMonth`). CME roll date je ale čtvrtek 8 dní před
expirací a od něj jsou objem i likvidita v dalším kontraktu. Změřeno 15. 9.
2026: RTH objem ES na sledovaném ESU6 11. 9. 1,07 M → 14. 9. 538 k → 15. 9.
**176 k**, tisky `/ESU26` 1/min; TradingView už ukazoval Z6 (rozdíl NQ ~275 b).
Bary, CumΔ, spot pro setupy, tendence i scénář dne tak 4 dny v kvartálu
vznikaly z kontraktu, který nikdo neobchoduje, v ceně, kterou trader
neobchoduje.

Kvartální opce a futures se navíc vypořádají v **SOQ 9:30 ET** (Special
Opening Quotation), ne v 16:00 ET — engine by v expirační pátek celé
odpoledne počítal nad mrtvým řetězem i kontraktem a na další expiraci (už na
novém kontraktu) přepnul až sobotní discovery.

Článek „Trojitá expirace v týdnu s Fedem" (15. 9. 2026) popisuje mechaniku
týdne (hedging dealerů, VIX expirace, SOQ, rebalance, pondělí po); uživatel
chce být na expiraci/roll vizuálně upozorněn jako v TradingView.

## Rozhodnutí

1. **Front kontrakt podle roll date** (`front_contract_eligible`): kontrakt
   je front, dokud má do expirace víc než `GEXLENS_FRONT_ROLL_DAYS` (8) dní.
   Platí pro IBKR pipeline (`_resolve_front_future`), tasty streamer
   (`SymbolMap.front_future`), IV rank i discovery cache (front po rollu se
   zahodí). Celá pipeline — bary, CumΔ, spot, opční řetěz — jede od roll
   date na novém kontraktu; aktivní řetěz je nejbližší expirace nového
   kontraktu (v roll týdnu tedy pondělní, 0DTE dobíhajícího kontraktu se
   nepoužije — jeho úrovně by byly v neobchodované ceně). **C-lite**; plný
   frame-shift (0DTE starého kontraktu posunuté o spread) = samostatné issue
   před prosincovým rollem.
2. **SOQ jako settle kvartální expirace**: `compute.settle.expiry_settle_ts`
   (9:30 ET pro 3. pátek bře/čvn/zář/pro, jinak 16:00 ET) tam, kde jde o
   expiraci (čas do expirace, timeout setupu, Greeks hlídka, tasty fallback,
   Forward GEX); hranice seance zůstává `settle_ts`. `expiry_expired` je
   časově citlivé — po SOQ se pipeline překlopí hned. Frontend
   `expirySettleUtc` totéž (odpočet v hlavičce).
3. **Kalendář expirací** (`compute.expiry_calendar`, `GET /calendar/expiry`):
   fáze `normal/roll/opex_week/expiry_day/post_opex`, roll date, SOQ, VIX
   expirace, značky. UI: chip ⌛ v hlavičce (jen mimo `normal`), ⌛ v ose
   grafu (intraday ze dnů osy, Daily přes celou osu), karta **Expirační
   týden** v Briefingu se scénáři A/B; alerty `expiry_calendar` (roll date,
   pondělí OPEX týdne, po SOQ), push kategorie news.
4. Odloženo (rozhodnutí uživatele): váha sentimentu v OPEX týdnu, post-OPEX
   režim, scénář dne z Forward GEX (#1189 E/F/G).

## Důsledky

- Data z roll týdne 14.–18. 9. 2026 jsou pro srovnání CumΔ (#1018) nepoužitelná;
  verdikt posunut po OPEXu.
- Nasazením v roll týdnu (16. 9. večer) engine přepne na ESZ6/NQZ6 s řetězem
  21. 9.; páteční kvartální 0DTE U6 (18. 9.) se v aplikaci nezobrazí.
- Gamma útes a Forward GEX se nemění; kalendář jim dává kontext v UI.
