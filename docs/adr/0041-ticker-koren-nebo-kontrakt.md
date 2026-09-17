# ADR-0041: Ticker instrumentu = kořen produktu, nebo pinovaný kontrakt

- Stav: přijato (17. 9. 2026, rozhodnutí uživatele — #1191 varianta 1)
- Souvisí: ADR-0001 (market data lines), ADR-0003 (instrumenty), ADR-0039 (roll pravidlo)

## Kontext

Ticker `ES` znamená „front kontrakt podle roll pravidla“ (ADR-0039, 8 dnů před
expirací). V roll týdnu tak `ES` sleduje Z6, zatímco páteční kvartální 0DTE
opce mají podklad U6 — kdo chce vidět přesně tento řetěz, neměl jak.
Druhá pipeline téhož kořenu přes IBKR by stála dalších ~50–60 market data
lines (strop 100, ADR-0001).

## Rozhodnutí

1. **Gramatika tickeru** (`engine/ticker.py`, zrcadlo `frontend/instrument/ticker.ts`):
   `ES` = kořen (automatický front), `ESU6` = pinovaný kontrakt
   (`kořen + kód měsíce F–Z + poslední číslice roku`). Kořeny končící kódem
   měsíce (`6J`, `M2K`) zůstávají kořeny — rozhoduje číslice roku na konci.
   API watchlist validuje (`422`), ukládá uppercase.
2. **Pinovaný kontrakt NAHRAZUJE automatický** — stejný počet pipeline a lines.
   Chce-li uživatel U6 v roll týdnu, přidá `ESU6` a případně odebere `ES`
   (strop `GEXLENS_MAX_INSTRUMENTS` platí dál).
3. **Ticker je klíč všeho, co pipeline ukládá a publikuje** (partice, OI archiv,
   setupy, deník, WS kanály). Na kořen se převádí **jen na hranicích**:
   IBKR (`Future(root)`, `FuturesOption(root, …)`, `reqSecDefOptParams(root, conId)`),
   tastytrade (`product-code=root`, `/futures-option-chains/root`, market metrics)
   a UI (lidský název produktu, profil deníku, hodnota bodu paper účtu).
   Jedno místo převodu, žádná další větev v enginu.
4. **Pinovaný kontrakt nemá roll pravidlo**: platí do expirace (discovery cache,
   tasty front future, IV rank). Expirovaný/neznámý kontrakt = chyba setupu
   (`InstrumentSetupError`), ne tichý fallback na jiný kontrakt.
5. Opční řetěz pinovaného kontraktu určuje `conId` podkladu — IBKR vrací
   expirace opcí právě na tento futures (U6: 0DTE do 18. 9., Z6: od 21. 9.).

## Důsledky

- `ESU6` má vlastní historii (partice, setupy, statistiky) oddělenou od `ES`;
  po expiraci zůstane v datech jako uzavřená řada — watchlist ho odebere
  uživatel (engine hlásí chybu setupu s důvodem).
- Rámec ceny pinovaného řetězu je cena toho kontraktu (v roll týdnu o spread
  jinde než front) — záměr, hlavička to označuje štítkem `📌 kontrakt U6`.
- Sidebar/deník: u kořene ukazují odhad front kódu podle roll pravidla
  (`ES (ESZ6)`), u pinovaného je kontrakt ticker sám.
