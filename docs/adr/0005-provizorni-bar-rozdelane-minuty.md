# ADR-0005: Provizorní bar rozdělané minuty na kanálu `price`

**Stav:** přijato (2026-07-21, rozhodl uživatel)
**Kontext:** SPEC v2.0 nepokrývá, co má engine publikovat pro minutu, jejíž
snapshot řetězce už poslal, ale jejíž 1min bar podkladu ještě není uzavřený.

## Problém

Minutový cyklus enginu (`runtime.run_cycle`) běží ~6 s před koncem minuty a
v jednom průchodu publikuje dvě věci o **různých minutách**:

| hodiny | `snapshot.*` `ts_min` | `price.*` `ts` |
|---|---|---|
| 14:11:54 | 14:11 | 14:10 |
| 14:12:54 | 14:12 | 14:11 |
| 14:13:54 | 14:13 | 14:12 |

Snapshot popisuje **probíhající** minutu, zatímco bar je poslední **uzavřená**
(`RealTimeBarAggregator` emituje minutu až při jejím přelomu). Mřížka heatmapy
je tím systematicky o jednu minutu napřed před svíčkami: nejnovější sloupec
mřížky nikdy nemá vlastní svíčku.

Za běhu to frontend zakrývá záložní svíčkou odvozenou ze `spot.*` kanálu
(#128, #143). Po znovunačtení stránky ale klient žádnou historii ticků nemá,
takže poslední sloupec zůstane bez svíčky, dokud nedoběhne další cyklus.
Naměřeno: **24 sekund souvislé díry** po refreshi uprostřed minuty
(14:23:31–14:23:54), zahojeno přesně v okamžiku dalšího cyklu.

Stejná díra vzniká i v REST balíku `/replay`, protože `derived/{sym}/bars`
obsahuje jen uzavřené minuty.

## Zvažované varianty

1. **Klient nezaloží sloupec mřížky pro minutu bez baru.** Čistě frontendová
   změna, ale nejnovější sloupec heatmapy by se objevil o minutu později —
   ztráta okamžitého pohledu na aktuální stav řetězce, což je hlavní účel
   nástroje. Zamítnuto.
2. **Posunout celý cyklus za konec minuty.** Snapshot by pak popisoval
   uzavřenou minutu a oba kanály by seděly. Znamená ale změnu významu `ts_min`
   v už uložených Parquet particích a zpoždění celé pipeline o minutu.
   Zamítnuto.
3. **Engine publikuje provizorní bar rozdělané minuty.** Přijato — viz níže.

## Rozhodnutí

Engine v každém cyklu publikuje na `price.{symbol}` navíc **provizorní bar
minuty `ts_min`**, sestavený z rozdělané agregace 5s barů, a příštím cyklem ho
nahradí finálním barem téže minuty.

1. **Zdroj dat.** `RealTimeBarAggregator` už rozdělanou minutu drží v
   `_current`; nově ji vystavuje jako `current`. Nic se nedopočítává ani
   nesyntetizuje — je-li `current` prázdný nebo patří jiné minutě než `ts_min`,
   provizorní bar se **nepublikuje** (raději žádná svíčka než vymyšlená).
2. **Rozlišení na kanálu.** Rámec `price.*` nese nové pole `final: bool`.
   `false` = rozdělaná minuta, `true` = uzavřený bar. Existující pole se nemění,
   takže starší konzumenti fungují dál.
3. **Persistence.** Provizorní bar se zapisuje do `derived/{sym}/bars` stejně
   jako finální, aby ho dostal i REST balík `/replay` po refreshi.
   `SnapshotWriter.write_bars` proto nově dělá **upsert podle `ts_min`** místo
   slepého append — jinak by po nahrazení vznikly dva řádky téže minuty a
   frontend by svíčku vykreslil dvakrát.
4. **Schéma Parquet se nemění.** `final` se posílá jen po WS, do `BARS_SCHEMA`
   nepřibývá — přidání sloupce by rozbilo čtení už existujících denních partic
   (`pq.read_table(..., schema=...)`). Bar v REST balíku se považuje za finální
   — s jednou výjimkou (#158): bar **aktuální wall-clock minuty** klient označí
   za provizorní sám, protože je s jistotou rozdělaný (engine ho upsertuje
   v :54 probíhající minuty) a spot ticky po znovunačtení stránky tečou hned;
   bez označení by svíčka zmrzla až do dalšího cyklu. Pro starší minuty žádná
   lepší informace neexistuje a berou se jako finální.
5. **Přednost živého spotu na klientovi.** Frontend zahazuje záložní spot
   svíčku minuty až ve chvíli, kdy pro ni dorazí **finální** bar. Provizorní bar
   ji nezruší, takže rozdělaná svíčka zůstává živá (aktualizuje se 5×/s) i
   posledních ~6 sekund minuty, kdy už provizorní bar dorazil.

## Důsledky

- Každý sloupec mřížky má od svého vzniku svíčku, včetně stavu po refreshi.
- Kanál `price.*` už neznamená výhradně „uzavřená minuta"; konzumenti, které to
  zajímá, mají `final`.
- `derived/{sym}/bars` může krátkodobě (do dalšího cyklu) obsahovat neuzavřený
  bar poslední minuty. Retence ani formát se nemění.
- Hodinová rekonciliace i tak zůstává pojistkou pro zmeškané minuty (#127).

## Ověření

- Engine: provizorní bar se publikuje s `final=false` a zapíše; následující
  cyklus ho nahradí finálním, v partici zůstane jeden řádek na minutu.
- Provizorní bar se nepublikuje, chybí-li rozdělaná agregace nebo patří-li
  jiné minutě.
- Frontend: záložní spot svíčka přežije provizorní bar a zanikne až s finálním.
