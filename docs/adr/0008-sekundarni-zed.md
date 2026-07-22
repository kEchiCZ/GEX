# ADR-0008: Sekundární zeď (call_wall_2 / put_wall_2)

**Stav:** přijato (2026-07-22, rozhodl uživatel — issue #92, varianta 3 s přepínačem)
**Kontext:** Primární call/put wall (argmax NetGEX strany, SPEC 4.2) mezi dvěma
téměř rovnocennými koncentracemi minutu po minutě přeskakuje (17. 7.: put wall
7450 ↔ 7500, 23 přeskoků). Spojitá linie pak kreslí svislé pruhy přes graf.
Datově je vše správně — jedna čára jen neumí vyjádřit „dvě rovnocenné zdi".

## Rozhodnutí

1. **Engine počítá sekundární zeď** (`compute/levels.py`): druhé nejsilnější
   LOKÁLNÍ maximum téže strany profilu se sílou >= `SECONDARY_WALL_RATIO`
   (0,7) × primární. Lokální vrchol vylučuje „rameno" primární koncentrace;
   slabší vrcholy nejsou rovnocenná zeď, jen šum. Bez kandidáta je pole None.
2. **Vlastní odvozená řada `levels2`** (`derived/{sym}/{exp}/levels2/{den}.parquet`,
   sloupce ts_min/call_wall_2/put_wall_2) — do `LEVELS_SCHEMA` se sloupce
   přidat nesmí, rozbilo by to čtení existujících partic
   (`pq.read_table(..., schema=...)`), stejné omezení jako ADR-0005 u barů.
   Retence ji pokrývá automaticky (generický průchod `derived/`).
3. **WS kanál `levels.*` nese `call_wall_2`/`put_wall_2` jako aditivní pole** —
   starší klienti je ignorují. `/replay` bundle přibírá klíč `levels2`
   (chybí-li, frontend se chová jako dosud).
4. **Frontend páruje linie PO ÚROVNÍCH, ne po pořadí síly** (`pairWallSeries`):
   v minutě se dvěma kandidáty jde vyšší strike na horní linii, nižší na dolní;
   jediný kandidát na linii s bližší poslední hodnotou. Každá linie tak sedí
   stabilně na své hladině a svislé pruhy zmizí. Plný styl + cenovku má linie,
   na které primární zeď AKTUÁLNĚ leží; druhá je tečkovaná bez cenovky.
5. **Přepínač `2. zeď`** (Toggles, default zapnuto, persistováno dle ADR-0007).
   Vypnuto = přesně dosavadní chování (jedna, případně přeskakující linie).

## Důsledky

- Setup detektor (ADR-0004) dál pracuje jen s primárními zdmi — sekundární je
  vizualizační/informační vrstva, sémantika `call_wall`/`put_wall` se nemění.
- Daily pohled sekundární zdi nezobrazuje (sloupec = den, `toLines` filtr).
- Historie před nasazením nemá `levels2` partice → starší dny se kreslí
  postaru; žádná migrace se nedělá.

## Ověření

- Engine: rovnocenné koncentrace → sekundární hlášena; slabé (< ratio) a
  ramena primární ne; persistence do `levels2`; WS rámec nese `*_2` pole.
- Frontend: alternující primární 7450↔7500 → dvě konstantní linie
  (`pairWallSeries`), přepínač vypnutý vrací dnešní tvar, decode/append
  s `levels2` i bez něj (starší API/engine).
