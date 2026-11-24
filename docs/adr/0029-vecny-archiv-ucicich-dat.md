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

## Dodatek 2026-11-24 (#1855): přeměřený růst a komprese partic

**Stav dodatku:** návrh, rozhoduje vlastník v #1855 (`needs-decision`). Do
rozhodnutí platí původní znění: engine zapisuje výchozím pyarrow (snappy).

**Růst je desetkrát vyšší, než počítal Kontext.** Přeměřeno 24. 11. na
produkci PC: říjen 5,1 GB, posledních 31 dní 4,5 GB, z toho snapshots 92 %.
To dává ~5 GB/měsíc, tedy ~55–60 GB/rok místo ~6 GB/rok z 20. 8. Příčinou
je víc expirací ES+NQ, greeks ve snapshotech a nové řady `derived/`.
`data/` má dnes 16,3 GB (parquety 13,2 GB, logy 2,3 GB). Alert `disk_limit_gb`
(20 GB) tak přijde kolem konce prosince 2026.

**Měření** (skript `scripts/measure_parquet_compression.py`, čísla a postup
v komentáři „Měření komprese“ v #1855). Vzorek je kopie 15 UTC dní
9.–23. 11. ES+NQ (10 seancí, 2 078 souborů, 2,19 GB). Zápis se měřil jako
součet všech partic dne, protože engine je v cyklu přepisuje celé
(`runtime.py`, `await asyncio.to_thread`). Součet je horní mez jedné minuty
na konci dne.

| varianta | velikost | restic (odhad) | zápis arm64 | zápis PC v kontejneru |
|---|---|---|---|---|
| snappy (dnes) | 1,000 | 0,853 | 1,27 s | 10,7 s (15,3 s) |
| zstd 1 | 0,874 | — | 1,58 s | 11,0 s |
| zstd 3 | 0,857 | — | 1,82 s | 11,3 s (17,9 s) |
| zstd 9 | 0,837 | — | 2,69 s | 13,0 s |
| zstd 1 + BSS heuristikou | 0,774 | — | 2,00 s | 11,3 s |
| **zstd 1 + BSS všech float sloupců** | **0,754** | **0,740** | **0,75 s** | **8,7 s (12,9 s)** |
| zstd 3 + BSS všech float sloupců | 0,740 | 0,728 | 0,85 s | 9,0 s (13,4 s) |

- **Velikost** je poměr k dnešním souborům celého vzorku. Ve sloupci restic
  je poměr repozitáře k dnešním souborům, odhadnutý zstd úrovně 3 nad celými
  soubory; restic komprimuje sám, takže úspora v repozitáři je menší než na
  disku.
- **Zápis arm64** je den 24. 11. na serveru Oracle (139 partic, 86 MB, 1 CPU,
  image enginu).
- **Zápis PC** je tentýž den v image enginu přes bind mount jako na produkci;
  v závorce je největší den vzorku (16. 11., 235 MB).
- **BSS** je kódování BYTE_STREAM_SPLIT u float sloupců bez slovníku. Ostatní
  sloupce si slovník nechávají.

Všechny varianty jsou bezztrátové: 2 078 z 2 078 souborů se shoduje ve schématu
včetně metadat, v typech, pořadí řádků i bitově, a NaN na stejném místě se
počítá jako shoda. Čtení dopadlo stejně v rámci šumu měření:
`read_table`, pandas, `pyarrow.dataset`, endpointy snapshots, replay
a profile v kontejneru. `scripts/backtest_setups.py` dává nad originálem,
zstd 1 + BSS a smíšeným adresářem bajtově shodný výsledek (254 setupů) za
stejný čas. Ve smíšeném adresáři jsou snappy a zstd v téže složce
(496 z 613 složek) a čte se beze změny kódu.

**Proč BSS podle schématu, a ne heuristikou:** výběr sloupců podle počtu
různých hodnot stojí při každém zápisu `count_distinct`, takže zápis na arm64
je o 58 % pomalejší než dnes. Pravidlo „všechny float sloupce“ je zadarmo,
deterministické a zároveň menší. Horší je jen u `trades` (`price` má málo různých hodnot),
které tvoří 0,4 % objemu.

**Navrhované rozhodnutí** (doporučená varianta A v #1855):
1. Engine zapisuje partice se zstd úrovně 1 a BYTE_STREAM_SPLIT pro všechny
   float sloupce bez slovníku. Mění se jediné místo zápisu,
   `parquet_store._write`. Čtení se nemění, protože smíšený adresář funguje.
2. Staré partice se jednorázově překomprimují **před F7a #1804**, aby první
   snapshot restic nenesl data dvakrát. Před rekompresí je záloha. Originál se
   smaže až po ověření obsahu (bitová shoda jako v měření). Dnešní partice
   se vynechá a běží se mimo US RTH.
3. `disk_limit_gb` 20 → 40 GB. Alert zůstává spouštěčem revize, nikdy
   nemaže.

**Dopad na úložiště** (odhad z ~5 GB/měsíc surového růstu):
- **D: (PC, 260 GB, ~50 GB volných):**
  - bez komprese zbývá ~10 měsíců;
  - s variantou A je to ~14 měsíců, protože rekomprese uvolní ~3,2 GB a růst
    klesne na ~3,8 GB/měsíc.
- **T7 (E:, 1,2 TB volných):** repozitář PC roste ~4,3 GB/měsíc (s A
  ~3,7 GB/měsíc). Úložiště tak vydrží desítky let a omezením není.
- **Oracle (200 GB, 181 GB volných):**
  - Až bude server aktivním strojem s celým archivem, ponese data i vlastní
    repozitář restic. Dohromady to je ~1,85× dat bez komprese a ~1,49× dat s A.
  - Volné místo vydrží ~16 měsíců bez komprese a ~21 měsíců s A, pak
    rozhoduje revize (větší disk, cloud #1811).
