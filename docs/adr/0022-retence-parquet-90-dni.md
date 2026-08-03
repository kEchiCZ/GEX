# ADR-0022: Retence Parquet partic 90 dní (odchylka od R3)

**Stav:** přijato (2026-08-03, rozhodl uživatel po analýze v issue #394/#434)
**Odchylka:** R3 ve SPEC kap. 0 stanoví retenci intraday a tick dat na **14 dní**.
Tímto rozhodnutím se okno prodlužuje na **90 dní**; ostatní části R3 (denní
Parquet partice, noční purge job) i R4 (věčný OI archiv) platí beze změny.

## Kontext

Při diagnostice #434 („proč na NQ nevznikají long odrazy od zdi") jsem potřeboval
přehrát historii přes detektor se změněnými prahy. Šlo to jen na **12 dnech** —
starší partice už purge job smazal. Vzorky jsou přitom malé: NQ `wall_bounce`
long má za dva týdny 29 setupů, což na rozlišení 20% a 30% úspěšnosti nestačí
(interval spolehlivosti ≈ ±15 p. b.).

Zásadní zjištění analýzy ale je, že **retence se učení netýká**. Všechna data
o výsledcích jsou v PostgreSQL, který purge job nemaže: `setups`, `signals`,
`signal_outcomes`, `news_model_stats`, `tendency`, `fa_validation`,
`t6_occurrences`, `track_record`. Kalibrace vah (#394), režimové statistiky
(#402), kalibrace α (#232) i sběr T6 (#256) tedy vzorek neztrácejí a delší
retence jim nepřidá nic.

Retence omezuje jen tři věci, které potřebují surové vstupy:

1. **přehrání historie se změněnými parametry** (`scripts/backtest_setups.py`),
2. **prohlížení staršího dne v aplikaci** — seznam dostupných dnů se sestavuje
   z partic na disku (`api/.../data.py`), takže dnes nelze otevřít den starší
   14 dnů a nikdy to nepůjde,
3. **zpětný přepočet odvozených řad** po opravě algoritmu (precedens: oprava
   flipu #197/#198 se do historických partic nepromítla).

## Rozhodnutí

**Prodloužit retenci na 90 dní.** Motivace není „potřebujeme delší historii" —
žádná dnešní funkce ji nevyžaduje. Důvodem je **nesymetrie rozhodnutí**:
nesmazaná data lze kdykoli smazat snížením hodnoty, smazaná data se vrátit
nedají. Za ~1,5 GB (měřeno 17 MB/den pro ES+NQ) se kupuje možnost neztratit
vzorek dřív, než se ukáže, jestli je potřeba.

### Nutná úprava: rozpojení retence a backfillu

`retention_days` byl **přetížený** — kromě okna purge řídil i rozsah startovního
backfillu barů (`UnderlyingBackfiller.backfill`: `range(retention_days + 1)`,
bez přeskakování dnů, které už na disku jsou). Prosté zvýšení na 90 by proto
při **každém startu enginu** vyžádalo 91 historických dotazů na symbol; přes
PacingGuard by se to táhlo desítky minut a zbytečně zatěžovalo IBKR.

Zavádí se proto samostatné `bars_backfill_days` (default 14) pro backfill,
zatímco `retention_days` řídí už jen purge. Rozsah backfillu se tímto
rozhodnutím **nemění**.

### Disk limit

`disk_limit_gb` se zvedá z 2 na 5 GB — při 90 dnech vychází obsazení ~1,5 GB,
což by se starým limitem hlásilo falešné alerty při přidání dalšího instrumentu.

## Důsledky

**Co se nemění:** engine počítá i zapisuje stejně, UI se pro aktuální dny chová
stejně, učení a kalibrace nad PostgreSQL běží beze změny, backfill zůstává na
14 dnech. Držení dat samo o sobě nemůže žádnou běžící funkci zhoršit — purge
job pouze maže soubory.

**Co se mění:** noční purge maže až po 90 dnech; obsazení disku poroste na
~1,5 GB (ES+NQ); scan purge job projde víc souborů (zanedbatelné).

**Riziko:** delší vzorek svádí k ladění prahů na týchž datech, na kterých se
pak validují. Precedens v projektu existuje — šablona T5 `divergence_spring`
vznikla z jediného živého případu, po změření měla 8,7 % úspěšnost a je vypnutá
(`disabled_templates`). Kalibrace v #394 proto musí držet část dnů stranou jako
out-of-sample; samotné delší okno kvalitu prahů negarantuje.

**Revize:** pokud se k datům starším 14 dnů do konce roku 2026 nikdo nevrátí,
snížit hodnotu zpět — purge je uklidí při nejbližším nočním běhu.
