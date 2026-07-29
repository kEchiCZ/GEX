# ADR-0017: Rolling dedup okno z 10 minut na 6 hodin

**Stav:** navrženo (PR s labelem `needs-decision`)
**Datum:** 2026-07-29
**Issue:** #351 (navazuje na měření z #274, ADR-0016)

## Kontext

SPEC sentiment 3.3 předepisuje rolling okno **10 minut** — dimenzované na
rozdíl rychlosti zdrojů (Finnhub vs pomalejší RSS u téže story). Provozní
data (29. 7. 2026, ~24 h, 1658 zpráv) ale ukázala jiný dominantní vzorec:
zdroje tutéž story **republikují** s Δt 23 minut až hodiny.

- Republikace v týž UTC den zachytí pojistka `dedup_hash` (normalizovaný
  titulek + den) — v DB nejsou vidět, zahazují se při zápisu.
- Republikace **přes půlnoc UTC** nemá co chytit: okno je dávno pryč a
  `dedup_hash` se liší dnem. Změřeno **19 propuštěných duplicit/den**
  (~1,1 % objemu), včetně market-moving stories („Iran launches surprise
  ballistic missile attack…" 23:5x → 00:1x, Δt=24 min).

Každá propuštěná duplicita se v sentimentu (SentIndex, waves, reakce)
počítá dvakrát.

## Rozhodnutí

Výchozí `dedup_window_minutes` **10 → 360** (6 h), strop konfigurace 1080.

Proč právě takto:

- **Intradenní chování se nemění.** Republikaci v týž den už dnes zahazuje
  `dedup_hash`; okno v hodinách jen sjednocuje chování přes půlnoc s tím,
  co přes den platí beztak.
- **Denní rubriky se stejným titulkem** (Market Talk Roundup, DJNL Morning
  Briefing — Δt ≈ 24 h) zůstávají oddělené, dokud okno < ~20 h; strop
  `le=1080` (18 h) to drží i při přeladění konfigurace.
- **Cross-source merge** nově funguje i pro pomalejší potvrzení téže story
  (dřív jen do 10 min) — latence per zdroj se dál měří a loguje.
- Paměť/CPU: 6 h okna ≈ 400 záznamů při současném objemu; lineární sken
  fuzzy vrstvy (ADR-0016) zůstává levný.

## Důsledky

- Odchylka od SPEC 3.3 („posledních 10 minut") — tento ADR ji dokumentuje;
  SPEC hodnotu nepřepisujeme, normou je konfig s tímto defaultem.
- Priming po restartu čte z DB 6 h zpět místo 10 min (řádově stovky řádků,
  jeden dotaz — beze změny logiky).
- Vědomý zbytkový případ: republikace s Δt > oknem přes půlnoc projde dál
  (v datech nejčastěji `Δt 13–19 h` u lifestyle rubrik Yahoo — nízká
  důležitost). Práh přeměřit po týdnu provozu spolu s prahy ADR-0016.
