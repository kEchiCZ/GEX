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

## Dodatek 2026-10-07 — roll pipeline v settle expirace, jeden helper (#1331)

**Stav:** přijato (uživatel, #1331 varianta A).

Bod 2 platil jen pro kvartální expiraci: `expiry_expired` rolovalo běžnou
expiraci až s novým kalendářním dnem v UTC, takže pipeline po settle 16:00 ET
běžela 3–4 h nad vypršelým řetězem (setupy, sondy T9, tendence, heatmapa).
Hranici settle navíc počítaly tři funkce (`setups.setup_settle_ts`,
`probes.probe_settle` nad `settle_ts`, Dyn profil v `runtime` nad `settle_ts`).

1. **Jeden helper** `compute/settle.expiry_settle(expiry: str)` — settle
   expirace `YYYYMMDD` podle bodu 2 (`expiry_settle_ts`: 16:00 ET, kvartální
   SOQ 9:30 ET), nečitelný formát → None. Ptá se ho roll, discovery i cache,
   hlídka Greeks, setupy (timeout, čas do expirace, `born_after_settle`),
   sondy T9 a Dyn profil. `setup_settle_ts` a `probe_settle` zanikly.
2. **Roll v settle:** `expiry_expired(expiry, now)` = `now ≥ expiry_settle`.
   Orchestrátor zastaví pipeline **po** cyklu minuty settle — ten je poslední
   nad starým řetězem a moduly v něm uzavřou, co k settle patří (sondy T9,
   setupy z živé dávky, paper, agregát stavu mapy); nové setupy ani sondy
   v něm nevzniknou (`born_after_settle`). Příští minutu se pipeline založí
   nad první expirací se settle v budoucnu — discovery i cache vypršelé
   vynechají.
3. **Moduly „jednou po settle“** nesmí potřebovat stav staré pipeline po
   jejím posledním cyklu: zápis ze vzorků v paměti patří do cyklu settle
   (stav mapy), vyhodnocení po settle + odklad běží v nové pipeline nad
   partice/DB a je idempotentní. Seznam a testy v ADMIN-MANUAL kap. 5.

Důsledky: mezera ve sběru 1–2 min v settle místo 3–4 h nad mrtvým řetězem;
nová pipeline začíná s prázdnou historií detektoru setupů; frontend vybírá
výchozí expiraci podle settle a v settle ji přepne (#1367).

**Otevřený nález k bodu 2:** po rollu front kontraktu (bod 1) je aktivní
řetěz na kvartální datum týdenní série nového kontraktu (EW3/QN3), která se
18. 9. 2026 vypořádala odpoledne, ne v SOQ. Settle podle samotného data
expirace (bez trading class) ji tak ukončí v 9:30 ET. Chování je stejné jako
před #1331 a mění se jen samostatným rozhodnutím (settle podle trading class, #1366).

## Dodatek 2026-10-07 — settle kvartálního data podle trading class (#1366)

**Stav:** přijato (uživatel, #1366 varianta A). Řeší otevřený nález výše.

1. **SOQ jen standardní kvartální třída:** `compute/settle.settles_at_soq(day,
   trading_class, symbol)` — 9:30 ET jen pro 3. pátek bře/čvn/zář/pro a třídu
   rovnou kořeni produktu (`ES`, `NQ`, `MES`…; opce na expirující kontrakt).
   Týdenní série na totéž datum (`EW3`, `QN3`) má settle 16:00 ET jako každý
   jiný pátek. `expiry_settle_ts` i `expiry_settle(expiry, trading_class,
   symbol)` třídu berou; bez třídy nebo symbolu rozhoduje datum jako dřív.
2. **Třída v runtime:** `EngineRuntime.trading_class` (z kontraktů řetězu)
   a `EngineRuntime.settle()` — ptá se ho roll (`expiry_expired`), discovery
   i cache (třída z `ExpiryInfo`), hlídka Greeks, Dyn profil, setupy (čas do
   expirace, invariant vzniku) a sondy T9.
3. **Třída v historii:** setup i sonda nesou `context.trading_class`;
   `born_after_settle`, `counts_in_stats` (souhrn, Knihovna, brzdy, brána,
   kalibrace, sebekontrola, kouč, gamma útes) i timeout otevřeného setupu po
   restartu (`StoredSetup.settle()`) čtou settle s ní. Řádky před #1366 třídu
   nemají a čtou se podle data (SOQ).
4. **Offline nástroje bez třídy** (`settle.history_expiry_settle`: přepočet
   setupů, backtest a walk-forward): na kvartální datum se třída dovodí
   z tickeru — kořenový ticker sbíral po rollu front kontraktu (bod 1) týdenní
   sérii nového kontraktu (16:00 ET), pinovaný kontrakt expirující v měsíci
   data (#1191) standardní třídu (SOQ). `recompute_setup_outcomes.py` tak
   kvartální setupy ověří a dovození uvede v důvodu řádku.
5. **Frontend** (`instrument/expiry.settlesAtSoq`, `expirySettleUtc(expiry,
   symbol)`) zná jen datum a ticker — používá totéž pravidlo jako bod 4
   (odpočet v hlavičce, výchozí expirace a její přepnutí v settle, projekce).

Mechanika setupů v5 se nemění (detektory a úrovně stejné, jen hranice
vyhodnocení), historické výsledky srovná přepočet se souhlasem uživatele.
Ověřit na prosincové expiraci 18. 12. 2026: třídy z `reqSecDefOptParams` pro
ESZ6/ESH7 a NQZ6/NQH7 na 20261218 a čas jejich vypořádání.
