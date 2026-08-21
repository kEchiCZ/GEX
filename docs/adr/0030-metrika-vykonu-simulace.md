# ADR-0030: Metrika výkonu simulace — denní ΣR, USD simulace a anualizovaný Sharpe (#794 fáze 0)

**Stav:** přijato (2026-08-21; rozhodnutí uživatele v #794: metrika „obojí od
začátku, cílit rovnou na produkční řešení")

## Kontext

Epic #794 zavádí kontinuální zlepšování setupů s cílem **Sharpe ratio > 2**.
Sharpe se dosud nikde nepočítal — track record měl hit rate, Wilson LB a Ø R
(`compute/setupstats.py`), žádnou equity ani volatilitu výnosů. Tohle ADR
definuje metriku, nad kterou se cíl vyhodnocuje a proti které bude
optimalizační smyčka (fáze 3+) porovnávat kandidáty.

## Rozhodnutí

### 1. Primární metrika smyčky: denní ΣR

- **Trade výnos** = `outcome_r` uzavřeného setupu (target/stop/timeout), jen
  řádky **aktuální mechaniky**. Aktuální verze se odvozuje dynamicky jako
  nejvyšší `mechanics_version` v datech — engine jinou nevyrábí a natvrdo
  zapsaná konstanta ve frontendu už jednou zastarala (v2 vs. v4, týden
  vyřazených statistik).
- **Portfolio** = všechny obchodované instrumenty (dnes ES + NQ) rovným dílem
  1R na setup, bez stropu souběhů — přesně to, co simulace generuje.
- **Den** = obchodní seance [17:00 CT D−1, 17:00 CT D) dle #512
  (`sessionDateIso`); uzavřený setup patří do seance svého `closed_ts`.
- **Denní výnos** = ΣR přes setupy uzavřené v seanci; seance bez uzavření
  = 0 se do řady NEpočítá (dny bez obchodu nejsou výnos 0 se štěstím, ale
  absence pozorování — nula by uměle snižovala volatilitu).

### 2. Sharpe

- **Anualizovaný**: mean(denní ΣR)/std(denní ΣR, ddof=1) × √252. Bezriziková
  sazba se neodečítá — výnosy jsou v R (bezrozměrné násobky rizika), ne
  kapitálové výnosy.
- Okna: **celá aktuální mechanika** + **klouzavých 30 seancí**.
- **Poctivost vzorku (závazné)**: UI vždy ukazuje počet seancí a do
  ~60 seancí explicitní upozornění, že číslo je orientační. Statistické
  potvrzení „SR > 2 vs. SR 0,5" vyžaduje řádově **400+ seancí** (SE
  anualizovaného SR ≈ √252/√N) — do té doby je Sharpe ukazatel trendu,
  ne splněný cíl. Gate #794 se vyhodnocuje nejdřív na 60 seancích jedné
  mechaniky, definitivně na 400+.

### 3. USD simulace (paralelní větev, produkční realismus)

- **Sizing** = kalkulačka #679: riziko $ = účet × % rizika (Settings →
  Trading, jen v prohlížeči); kontrakty = floor(riziko / (stop v bodech ×
  hodnota bodu)) **micro variant** (MES/MNQ — plný kontrakt je s malým účtem
  0×). Trade s 0 kontrakty se přeskočí a reportuje zvlášť (skipped) — floor
  a přeskoky jsou přesně ta realita, kterou R-řada nevidí.
- **Náklady** per kontrakt round-trip, odečtené z P/L každého obchodu:
  komise 2 × 0,62 $ (IBKR micro futures, fixed tier) + slippage 1 tick na
  stranu. Defaulty: **MES 2,49 $** (tick 1,25 $), **MNQ 1,74 $**
  (tick 0,50 $), jiné 2,50 $. Hodnoty jsou konstanty v
  `frontend/src/setups/performance.ts` — až bude skutečná exekuce, nahradí
  je měření (revize tohoto ADR).
- **Equity** = kumulativní P/L $ od startu aktuální mechaniky nad účtem
  z kalkulačky; denní USD řada → USD Sharpe týmž vzorcem.

### 4. Kde se počítá

Ve **frontendu** (`setups/performance.ts`, čisté funkce + testy) nad
`GET /setups/{symbol}` pro všechny symboly watchlistu. Důvody: sizing i účet
žijí jen v prohlížeči (#679 — na server se neposílají), R-řada je
z existujících dat odvoditelná bez nového endpointu a engine se nemění
(běží shadow sběr M7). Až smyčka fáze 3 bude potřebovat Sharpe na serveru
(porovnávání kandidátů), převezme definici z tohoto ADR — vzorec je záměrně
triviální a testy jsou kontrakt.

## Důsledky

- Stránka Statistiky dostává sekci Výkon setupů: equity křivka (R i USD),
  Sharpe celkem + 30 seancí, max drawdown, počty seancí/obchodů a poznámka
  o vzorku.
- Oprava vedlejšího nálezu: `CURRENT_MECHANICS_VERSION = 2` ve frontendu
  (zastaralá vs. engine v4) → dynamické odvození; statistiky Setupů zase
  vidí aktuální setupy.
- Reset track recordu (#794 ad 3) metriku nezasáhne — řada prostě začne
  znovu od první uzavřené seance nové mechaniky.

Souvisí: #794 (epic, rozhodnutí), #679 (sizing), #512/#748 (seance),
#311 (mechanics_version), ADR-0029 (učicí data).
