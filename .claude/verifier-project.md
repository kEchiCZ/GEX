# Projektový seznam pro verifier a listy (GEXLens)

Doplňuje AGENTS.md o to, co čte `/spec` (švy), implementer (švy, výpočetní jádro) a verifier
(osa Standardy). Pravidla AGENTS.md platí celá, tady se neopakují; tady je, co z nich hledat
v diffu.

## Testovací švy

Šev listu (`Šev:` v `brief.md`) je jeden z těchto. Jiný šev je otázka pro vlastníka.

| Šev | Kdy | Pozor |
|---|---|---|
| čistá funkce v `engine/src/gexlens_engine/compute/` + golden v `engine/tests/golden/` | výpočty (GEX, levels, walls, CumΔ, profil, setupy) | očekávané hodnoty ručně odvozené, ze SPEC nebo ADR; změna vzorce = změna golden ve stejném PR s odůvodněním |
| čistá funkce s časem jako parametrem | hlídače, brány trhu, obchodní den | test na sobotu, neděli, pondělí proti pátku, denní pauzu 16:00–17:00 CT, svátek NYSE a nedělní otevření |
| endpoint API přes `TestClient` (vzor `api/tests/test_api.py`) | REST a tvar odpovědi | API čte storage a nepočítá (výjimky ADR-0045 bod 5, ADR-0059 bod 8) |
| IBKR, tasty a čas přes mock (`engine/src/gexlens_engine/ibkr/mock.py`) | adaptéry, minutový cyklus | IBKR se v testech nikdy nevolá živě |
| vitest nad čistou funkcí ve `frontend/src/` | logika frontendu | ověřuj `npm run build`, ne jen `tsc --noEmit` |
| skript ze `scripts/` jako podproces nad fixture v `tmp_path` (vzor `engine/tests/test_backfill_bars.py`) | provozní skripty | žádný zápis do `data/` ani PG produkce |
| ověření naživo po nasazení | časování, Docker, data produkce | list to uvede a důkaz přijde po nasazení v pauze Globexu |

**Ruční golden × otisk.** Golden s ručně odvozenými hodnotami je důkaz. Otisk přegenerovaný kódem
(např. `engine/tests/golden/setups_regression_577.json`) hlídá jen změnu a za důkaz správnosti
se nepočítá; jeho přegenerování v diffu potřebuje odůvodnění v PR.

**Výpočetní jádro** (`core_paths` v `.claude/harness.json`): list běží na Opusu, oprava a změna
vzorce začíná testem do červené, verifier pouští nové testy proti výchozímu commitu.

## Co hledat v diffu (osa Standardy)

1. **Drift dokumentace:** tvrzení v ADR, SPEC, manuálu nebo AGENTS.md, které po změně neplatí;
   rozhodnutí bez ADR; změna UI nebo konfigurace bez úpravy manuálu (#628); nový modul bez
   řádku SPEC kap. 11.
2. **Kritická pravidla:** hlídač dat bez brány `compute/marketclock.is_market_closed` a testu na
   víkend, pauzu a nedělní otevření; filtr setupů nebo signálů podle svátku či tenkého trhu;
   test obchodního dne přes `weekday() < 5` nebo `getUTCDay()` místo
   `compute/settle.is_trading_session` (frontend `instrument/tz.isTradingSessionIso`); hledání
   minut seance jen v jedné UTC partici; mazání dat uživatele nebo hromadný DELETE; nové market
   data lines nebo vyšší `batch_size`; tiché selhání; demo data v UI; IO delší než sweep
   v minutovém cyklu.
3. **Relevance dat:** look-ahead (budoucí minuty v minulém bodě, replay jen s daty dostupnými
   v čase t); časová pásma a letní čas (ET, CT, SEČ/SELČ, UTC); OI podle času publikace a settle
   podle trading class; zmrzlá čísla bez viditelného stáří; forward-fill nebo interpolace místo
   mezery; odhad neoznačený jako odhad; směšování zdrojů nebo jednotek; reset stavu na hranici
   seance (17:00 CT) smí smazat jen data staré seance (#1460, #1603).
4. **Výkon:** čtení particí bez cache (bind mount ~20 ms na soubor), práce v minutovém cyklu
   úměrná historii, RAM (WSL 6 GB), velikost payloadu WS, dotaz PG bez indexu.
5. **Veřejné repo:** jméno referenční aplikace nebo zdroje inspirace, cizí screenshot, tajemství,
   osobní údaje.

## Jednosměrné změny

Tělo PR je musí označit „jednosměrné“: změna `engine/tests/golden/`, DDL a destruktivní SQL
v přidaných řádcích netestového kódu (`*/src/`, `scripts/`), změna retence
(`engine/src/gexlens_engine/storage/retention.py`, `GEXLENS_RETENTION_DAYS`), změna formátu
Parquet partic nebo OI archivu.

## Cesty UI

Změna ve `frontend/src/` (mimo `*.test.ts(x)`) není „Bez změny UI“.
