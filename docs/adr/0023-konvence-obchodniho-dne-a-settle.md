# ADR-0023: Konvence obchodního dne a settle

**Stav:** přijato (2026-08-06, rozhodl uživatel — issues #498, #511, #512)
**Kontext:** „Den" byl v aplikaci na řadě míst UTC kalendářní den a klíčové časy
(settle, publikace OI, hranice T6) fixní UTC konstanty. Trh ale žije v seancích
definovaných burzovním časem (Globex 17:00 CT → 16:00 CT), který se vůči UTC
posouvá s DST. Důsledky: T6 měřil close-to-close přes půlnoc UTC (#498),
settle 20:00 UTC platí jen v létě (#511), nedělní večerní bary visí v nedělní
partici místo pondělní seance (#512).

## Rozhodnutí

1. **Jedna sdílená definice hranic.** Settle (a další denní hranice) definuje
   jediný sdílený helper v compute vrstvě; žádné další lokální konstanty.
   Zavedeno v #498 (T6 přepnut na sdílenou settle hranici, historičtí kandidáti
   přepočteni a verzováni).
2. **#511 přepne helper z fixní UTC hodiny na odvození z burzovní timezone**
   (IANA `America/Chicago` / `America/New_York`, vzor `compute/marketclock.py`).
   Tím se settle, publikace OI, T6 i session markery opraví na jednom místě
   pro letní i zimní čas.
3. **Obchodní den pro osu a replay = Globex seance** `[open 17:00 CT dne D−1,
   settle 16:00 CT dne D]`. Implementuje se **ve čtecí vrstvě** (#512): /replay
   pro den D sešije partici D s večerní částí partice D−1. **Úložiště zůstává
   klíčované UTC kalendářním dnem** — přepis partic (fáze 2) se nedělá, dokud
   pro něj nevznikne konkrétní důvod.
4. **Svátky neřeší kalendář.** Rozhoduje existence barů (stejný princip jako
   dosavadní `marketclock.py` — odhad, konečné slovo mají data).
5. **Pořadí realizace: #498 → #511 → #512.** Hranice seance potřebuje
   DST-korektní převod, proto #512 až po #511.

## Důsledky

- Retence/purge a OI archiv beze změny (partice zůstávají, OI je klíčované dnem
  publikace).
- Sešití v #512 musí projít testem kontinuity: CumΔ a kumulativní volume přes
  hranici partic bez dvojího započtení.
- T6 kandidáti sbíraní před #498 jsou přepočtení podle nové konvence; verze
  konvence se ukládá (obdoba `SETUP_MECHANICS_VERSION`), aby kalibrace nikdy
  nemíchala dva režimy.

## Dodatek 2026-08-06 (#511)

Bod 2 realizován: settle a další denní hranice odvozuje `compute/settle.py`
z burzovní timezone (`settle_ts` = 16:00 `America/New_York`,
`session_time_utc` pro obecný burzovní čas); frontend má protějšek
`instrument/tz.ts` (`Intl.DateTimeFormat` s `timeZone`, bez závislostí)
a session markery jsou definované v lokálním čase burzy + IANA zóně.

## Dodatek 2026-08-12 (#512)

Bod 3 realizován: `session_bounds`/`session_frame` v API (`gexlens_api/data.py`)
sešívají osu obchodního dne `[open 17:00 CT D−1, open 17:00 CT D)` pro všechny
denní čtecí endpointy (/replay, /flow, /heatmap, /profile, /chain, /levels,
/gexplane profily); `gexfield*` se nesešívá (partice drží jen poslední stav).
Frontend odvozuje živý den přes `sessionDateIso` (`instrument/tz.ts`) — po
17:00 CT běží osa následujícího dne. Polouzavřený interval = žádné dvojí
započtení z konstrukce; kontinuitu CumΔ/volume přes hranici partic kryje
`api/tests/test_session_day.py`. Vedlejší nález: CumΔ se dnes resetuje jen
restartem enginu (SPEC 4.5 reset nezapojen) — řeší #638.

## Dodatek 2026-08-12 (#638)

CumΔ i čistý klasifikovaný objem (FA odhad) jsou kotvené na **open Globex
seance**: `CumDeltaTracker.roll_session` resetuje na hranici (tentýž okamžik,
kdy se překlápí osa dne z #512), restart uprostřed seance kumulativy NEnuluje —
navazují se z partic (`read_last_cum_delta` pro flow, session-aware
`read_netflow_latest` pro netflow, obě přes okno `session_bounds`). Sdílená
definice hranic přesunuta do `compute/settle.py` (`session_bounds`,
`trading_session_date`); API je re-exportuje. Kotva net objemu je správně
tatáž: ranní OI archiv odráží pozice k předchozímu settle, takže tok od open
seance je přesně to, co v něm chybí. `SETUP_MECHANICS_VERSION` 3 → 4 —
hodnoty `cum_delta` v setup contextu před/po nejsou srovnatelné.

**Config migrace:** `GEXLENS_OI_PUBLICATION_HOUR_UTC` (fixní UTC hodina) je
nahrazeno dvojicí `GEXLENS_OI_PUBLICATION_TIME_LOCAL` +
`GEXLENS_OI_PUBLICATION_TZ` (default `07:00` `America/Chicago`, což odpovídá
dřívějším 12:00 UTC v letním čase). Starý klíč funguje dál a má přednost
(zpětná kompatibilita .env), ale při startu loguje deprecation warning.

Uložená data (parquet, DB) se nemigrují — konvence je výpočetní. Verze T6
konvence zůstává 2: letní settle hranice se nemění a všichni existující
kandidáti jsou letní, přepočet by tedy nic nezměnil.
