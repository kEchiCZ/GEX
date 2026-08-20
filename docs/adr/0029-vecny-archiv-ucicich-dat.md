# ADR-0029: Věčný archiv učicích dat — snapshots/ a derived/ se nemažou (revize ADR-0022)

**Stav:** přijato (2026-08-20, rozhodl uživatel v #794, implementace #762)
**Odchylka:** R3 ve SPEC kap. 0 stanoví retenci intraday dat (ADR-0022 ji
prodloužil na 90 dní). Tímto rozhodnutím se `snapshots/` a `derived/`
z retence **vyjímají úplně** — mažou se už jen dopočitatelné řady (`ticks/`).
R4 (věčný OI archiv) a výjimka barů (S4, #275) platí beze změny.

## Kontext

ADR-0022 stálo na závěru, že „retence se učení netýká", protože výsledková
data žijí v PostgreSQL. Ten závěr přestal platit dvakrát:

1. **Precedens ztráty (#575):** při backfillu band_regime šlo doplnit jen 49
   z 544 historických setupů — profily zbylých 495 už byly za retencí.
   Přesně případ 1 a 3 z ADR-0022 (přehrání s jinými parametry, zpětný
   dopočet), který se z hypotézy stal skutečnou škodou.
2. **Samoučící smyčka (#794):** kontinuální zlepšování parametrů detektoru
   se učí replayem nad surovými vstupy (`scripts/backtest_setups.py`
   rekonstruuje `MinuteInputs` z bars + levels + walldom + flow + snapshots).
   Statistické potvrzení cíle (Sharpe > 2) vyžaduje řádově stovky seancí —
   tedy víc, než jakékoli klouzavé okno kdy udrží. Po rozhodnutí, že track
   record v PG je resetovatelný (odvozená data), se surové parquety staly
   **jediným nenahraditelným zdrojem učení**: IBKR historii řetězce zpětně
   nedá.

Ekonomika (změřeno 20. 8. na produkci): snapshots 312 MB + derived 179 MB za
měsíc provozu ES+NQ ≈ **~6 GB/rok**. Na D: je 20,7 GB volných; disk alert
(#773) hlídá skutečné volné místo. Náklad většího disku za pár let je proti
hodnotě nenahraditelných dat zanedbatelný.

## Rozhodnutí

1. Nový flag `keep_learning_data_forever` (default **true**): purge job
   přeskakuje vše pod `snapshots/` a `derived/`. Maže se už jen `ticks/`
   a budoucí dopočitatelné adresáře.
2. Výjimka je nezávislá na `keep_bars_forever` (S4) — vypnutí jedné nestrhne
   druhou.
3. `disk_limit_gb` default 5 → 20 GB: limit je nově alert na revizi (komprese
   starých partic, větší disk), ne strop, ke kterému se maže.
4. `retention_days` zůstává 90 (ADR-0022) pro řady, které pod výjimku
   nespadají.

## Důsledky

- Stejná nesymetrie jako v ADR-0022, dotažená do konce: smazané se nedá
  vrátit, ušetřené místo ano.
- Prohlížení libovolně starého dne v aplikaci (bod 2 z ADR-0022) přestane
  narážet na retenci.
- Roční růst ~6 GB → revize (komprese/HW) až alert #773 nebo `disk_limit_gb`
  řekne; žádná automatika nemaže.
- Reset track recordu (#794 ad 3) je bezpečný: setupy jsou dopočitatelné
  replayem, zůstávají jen řádky s ručním hodnocením nebo vazbou na deník.

Souvisí: #762, #794, #575 (precedens), ADR-0022 (revidované), S4/#275 (bary),
R4/ADR-0001 (OI archiv).
