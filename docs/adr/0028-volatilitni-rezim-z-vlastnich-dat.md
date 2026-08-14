# ADR-0028: Volatilitní režim z vlastních dat (náhrada za VIX)

**Stav:** přijato (2026-08-14, rozhodl uživatel — issue #713, epic #708)
**Kontext:** Deník rev. 2 (#708) potřebuje pro futures profil **volatilitní
bucket**. Bez něj se statistiky R průměrují přes nesouměřitelné dny: ADR ES je
~30–40 bodů při VIX 12–15, ale 100+ nad VIX 30, takže **stejný stop v bodech je
v jiném režimu úplně jiný obchod**. SPEC v2.0 pojem volatilitního režimu vůbec
nezná (existuje jen `gex_regime` a měřicí `band_sharpness` z #575).

Zvažovaly se dvě cesty: externí VIX feed, nebo odvození z vlastních dat.
**Uživatel rozhodl pro odvození z vlastních dat.**

## Co průzkum zjistil (14. 8. 2026)

| Zdroj | Živě | Zpětně | Reálná hloubka |
|---|---|---|---|
| **1m bary** (`derived/{sym}/bars`) | ano | **ano** | **~2 roky ES i NQ** (od 2024-07-28), **nikdy se nemažou** |
| Snapshoty (`iv` per minuta/strike) | ano | ano | 90 d konfigurace, **25 d reálně** (sběr od 19. 7. 2026) |
| Expected move (ATM straddle) | ano (klient) | přepočtem | = snapshoty (25 d) |
| `oi_eod.iv/und_price` (#519) | ano (ráno) | **ne** | **1–2 dny** — sloupce vznikly 13. 8. 2026 |
| ~30denní ATM IV (analog VIX) | **ne** | **ne** | archiv drží 5 expirací ≈ 1 týden |

Tři zjištění, která rozhodla o konstrukci:

1. **Bary jsou jediný zdroj se statistickou silou.** ~500 obchodních dnů ES/NQ,
   vyňaté z purge (`keep_bars_forever`, `retention.py:7-11`). Všechno ostatní má
   ≤ 25 vzorků a bude je mít až do konce října 2026.
2. **Analog VIX z vlastního řetězce sestavit nejde.** ES/NQ mají denní expirace
   a archiv pokrývá `OI_ARCHIVE_EXPIRIES=5`, tedy ~1 týden. Term structure pro
   30denní konstrukci neexistuje. Zvyšovat pokrytí by platilo až od dne změny,
   ne zpětně — a naráží na strop 100 market data lines (viz paměť k ADR-0001).
3. **Skutečný VIX už v naší databázi je.** CNN Fear & Greed kolektor ukládá
   sub-index `market_volatility_vix`, jehož `y` je **uzavírací hodnota VIX**
   (ne skóre 0–100), plus `market_volatility_vix_50` (50denní MA). Tabulka
   `crowd_sentiment` v PostgreSQL, **mimo retenci**, každý fetch je zároveň
   backfill ~250 denních bodů ≈ 1 obchodní rok (ADR-0014).

## Rozhodnutí

1. **Volatilitní režim se počítá z 1m barů, ne z opčního řetězce ani z externího
   feedu.** Metrika je denní rozsah seance a realizovaná volatilita.

2. **Bucket je percentil vlastní historie, ne absolutní práh.** Kategorie
   (`low` / `normal` / `elevated` / `crisis`) se určují z klouzavého okna
   (návrh 252 obchodních dnů, minimálně 60) percentilovými hranicemi, ne
   pevnými čísly. Důvod: absolutní hodnoty VIX nejsou přenositelné na ADR ES
   v bodech a mezi instrumenty (ES medián ATR 1,57 b vs. NQ 11,52 b).
   Percentil je navíc jediné, co dává smysl i pro NQ bez vlastní VIX obdoby.

3. **Skutečný VIX z `crowd_sentiment` slouží jako validační a kalibrační osa,
   nikdy jako běhová závislost.** Bucket musí být plně funkční, i když CNN
   endpoint vypadne (je neoficiální, hrozí 418 — ADR-0014). Korelace vlastní
   metriky proti ~ročnímu VIX se změří jednou při zavedení a pak periodicky;
   rozpor je signál k rekalibraci, ne k pádu.
   Tímto se **neporušuje rozhodnutí „bez externího VIX feedu"** — jde o data,
   která už do naší DB tečou z jiného důvodu a jejichž výpadek nic neshodí.

4. **Historie se dopočítá zpětně.** Backfill nad `session_ranges()` dá ~500
   historických dnů okamžitě, takže percentily jsou od prvního dne smysluplné.
   Tohle je hlavní důvod, proč metrika stojí na barech a ne na IV.

5. **Uloží se hodnota, ne jen kategorie.** K záznamu deníku se ukládá i syrová
   metrika a verze definice — hranice bucketů se budou kalibrovat a přeřazení
   starých záznamů by falšovalo historii (stejná logika jako u snapshotu
   kontextu v #711 a jako `SETUP_MECHANICS_VERSION`).

6. **Rozsah:** prakticky použitelné jen pro **ES a NQ**. RTY/MES/MNQ mají 2–10
   dnů barů; pro ně se bucket nepočítá a pole zůstane prázdné (nikdy se
   nedosazuje `normal` jako „bezpečný default" — to je tiché selhání).

## Konstrukce

- **Čistý výpočet:** nový `engine/src/gexlens_engine/compute/volregime.py` —
  bezstavové funkce nad `[(session_date, high, low, close), …]`, golden testy.
- **Kolektor:** `engine/src/gexlens_engine/volregime.py` po vzoru
  `GammaCliffCollector` (#576): `on_minute()` + práh `settle_ts(session) +
  SETTLE_GRACE_MINUTES`, jednorázový `_run_backfill()` přeskakující
  `existing_dates()`.
- **Úložiště:** nová tabulka po vzoru `gammacliff_store.py`, PK
  `(session_date, symbol)`, PostgreSQL navždy (mimo retenci).
- **Zdroj denních rozsahů:** **použít hotovou `session_ranges()`**
  (`engine/src/gexlens_engine/gammacliff.py:103-131`) — čte všechny bars
  partice, správně zařazuje do Globex seance (#512) a ořezává na settle.
  Nepsat znovu.
- **Živý (intraday) bucket:** `average_true_range()` z `compute/setups.py:305`
  už teče minutovou smyčkou — zařadit tutéž hodnotu do percentilového koše
  z historie, nezavádět druhý výpočet.

## Past na pojmenování

V repu jsou dnes **dvě různé věci pod jménem „ATR"**:

| Funkce | Co počítá |
|---|---|
| `compute/setups.py:305` `average_true_range()` | **skutečný ATR** — true range vč. gapu, SMA |
| `compute/gammacliff.py:99-113` `range_in_atr()` | **ADR** — SMA(high − low) bez gap složky |

Nová metrika musí buď jednu z definic převzít beze změny, nebo se jmenovat
jednoznačně. **Nezavádět třetí význam slova ATR.**

## Důsledky

- Žádná nová externí závislost, žádný nový poplatek za data.
- Bucket je od prvního dne postavený na ~500 vzorcích místo na 25.
- Metrika je **realizovaná, ne implikovaná** — nevidí dopředu. Pro deníkový
  řez (klasifikace už proběhlého obchodu) to nevadí; pro dopřednou bránu
  setupů by to bylo omezení, a proto se tato ADR na brány nevztahuje.
- Term structure ani IV rank/percentile z toho neplynou. Pokud se ukážou jako
  potřebné, jsou v #618 (tastytrade market metrics) — to je samostatné
  rozhodnutí, tato ADR ho nepředjímá.
- Pro RTY/MES/MNQ pole chybí; UI to musí umět zobrazit jako „neměřeno".

## Souvisí

#713 (futures vrstva deníku), #708 (epic), #576 (`gamma_cliff` jako vzor),
#575 (`band_sharpness` — jiná osa režimu, nezaměňovat), ADR-0014 (CNN F&G),
ADR-0022 (retence 90 dní; barů se netýká), ADR-0023 (obchodní den a settle).
