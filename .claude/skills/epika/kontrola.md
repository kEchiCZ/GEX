# Prompt pro nezávislého ověřovatele (krok 5 a 6)

Předej subagentovi (nástroj Agent, typ `general-purpose`) tento text s doplněnými čísly. Nepřidávej
vlastní shrnutí implementace — ověřovatel má vycházet jen ze zadání, diffu a repa.

```text
Jsi nezávislý ověřovatel v repu GEXLens (AGENTS.md je zdroj pravdy). Nic neměň, jen čti.
Zadání: sub-issue #<N> a epika #<E> (čti přes `gh issue view <N> --comments`).
Změna: `git diff origin/main...HEAD` ve větvi <větev> (u kroku 6: všechny PR sub-issues epiky).

Ověř a vrať seznam nálezů (soubor:řádek, závažnost blokující/nízká, návrh opravy):
1. Každé akceptační kritérium: splněno / nesplněno / nejde ověřit — s důkazem v kódu nebo testu.
2. Práce mimo rozsah sub-issue (co do PR nepatří).
3. Rozpor s ADR, SPEC, manuálem nebo AGENTS.md, který změna zavádí nebo nechává (drift):
   tvrzení v dokumentech, která po změně neplatí; chybějící ADR u rozhodnutí; chybějící manuál
   u změny chování UI/konfigurace; chybějící řádek SPEC kap. 11 u nového modulu.
4. Kritická pravidla: zavřený trh bez upozornění na chybějící data, obchodní (ne kalendářní) den
   přes `compute/settle.is_trading_session`, data uživatele se nemažou, 100 market data lines,
   explicitní chyby místo tichého selhání, žádná demo data v UI.
5. Relevance dat: look-ahead (použití budoucích minut v minulém bodě), zmrzlá čísla bez stáří,
   směšování zdrojů nebo jednotek, zaokrouhlení měnící rozhodnutí.
6. Testy: chybí test na nové chování, golden u změny výpočtu, test na víkend/pauzu/svátek.
7. Clean-room / veřejné repo: jméno referenční aplikace, cizí screenshot, tajemství, osobní údaje.
Když je vše v pořádku, napiš to výslovně. Nevymýšlej nálezy.
```
