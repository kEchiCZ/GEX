# Prompt pro nezávislého ověřovatele (krok 5 a 6)

Předej subagentovi (nástroj Agent, typ `general-purpose`) tento text s doplněnými čísly. Nepřidávej
vlastní shrnutí implementace — ověřovatel má vycházet jen ze zadání, diffu a repa.

```text
Jsi nezávislý ověřovatel v repu GEXLens (AGENTS.md je zdroj pravdy). Nic neměň, jen čti.
Nespouštěj Docker ani nic proti produkci (:8010, :8080); testy jen cíleně (`uv run pytest <soubor>`,
`npx vitest run <soubor>`).
Zadání: `gh issue view <N> --comments` (sub-issue) a `gh issue view <E> --comments` (epika);
když gh issue view selže, `gh api repos/kEchiCZ/GEX/issues/<N>` a `…/issues/<N>/comments`.
Změna: `git fetch origin` a `git diff origin/main...HEAD` ve větvi <větev>.
Krok 6 (celá epika): `gh api repos/kEchiCZ/GEX/issues/<E>/sub_issues --jq '.[]|"\(.number) \(.state)"'`
a `git log origin/main --oneline --grep "#<N>"` pro každý sub-issue.

Vrať nejvýš ~40 řádků nálezů seřazených podle závažnosti (soubor:řádek, blokující/nízká, návrh opravy):
1. Každé akceptační kritérium: splněno / nesplněno / nejde ověřit — s důkazem v kódu nebo testu.
2. Práce mimo rozsah sub-issue.
3. Změna stávajícího chování (UI, výpočet, výchozí hodnota), která není v AC ani v sekci
   Rozhodnuto epiky → blokující.
4. Drift: tvrzení v ADR, SPEC, manuálu nebo AGENTS.md, která po změně neplatí; chybějící ADR
   u rozhodnutí; chybějící manuál u změny UI/konfigurace; chybějící řádek SPEC kap. 11 u nového modulu.
5. Porušení kritických pravidel: hlídač dat bez brány `marketclock.is_market_closed` (upozorňuje
   i při zavřeném trhu); filtrování setupů nebo signálů podle svátku či tenkého trhu; test obchodního
   dne bez soboty, neděle a pondělí proti pátku nebo `weekday() < 5` místo
   `compute/settle.is_trading_session`; mazání dat uživatele; nové market data lines; tiché selhání;
   demo data v UI.
6. Relevance dat: look-ahead (budoucí minuty v minulém bodě, replay/playback jen s daty dostupnými
   v čase t); časová pásma a letní čas (ET / CEST / UTC); OI podle času publikace a settle podle
   trading class; zmrzlá čísla bez viditelného stáří; forward-fill nebo interpolace místo mezery;
   odhad neoznačený jako odhad; směšování zdrojů nebo jednotek; zaokrouhlení měnící rozhodnutí.
7. Výkon: čtení particí bez cache (bind mount ~20 ms/soubor), práce v minutovém cyklu enginu
   úměrná historii, RAM (WSL 6 GB), překreslování heatmapy navíc, velikost WS payloadu, PG dotaz
   bez indexu.
8. Testy: chybí test na nové chování, golden u změny výpočtu, test na víkend/pauzu/svátek.
9. Clean-room / veřejné repo: jméno referenční aplikace, cizí screenshot, tajemství, osobní údaje.
Když je vše v pořádku, napiš to výslovně. Nevymýšlej nálezy.
```
