# ADR-0002: Politika auto-rozšíření pásma strikes

**Stav:** accepted (v2, schváleno Romanem 2026-07-16) · **Souvisí:** SPEC 3.2, issue #6

## Kontext

SPEC 3.2 požaduje pásmo strikes ±X bodů od spotu „s automatickým rozšířením, pokud se
spot přiblíží k okraji na < 25 % pásma", ale nedefinuje JAK. První verze tohoto ADR
(recentrování na spot + růst 1.5×) měla slabinu: při trendovém dni pásmo „odjelo"
se spotem a vzdálené křídlo (např. put wall, který trader sleduje) z pásma vypadlo.

## Rozhodnutí (v2 — grow-only obálka)

Pásmo je **denní obálka `[low, high]`, která se nikdy nezužuje ani neposouvá — jen roste**:

1. **Výchozí obálka** na začátku obchodního dne: spot ± `strike_range_points` (±200 b).
2. Když se spot přiblíží k okraji na < `strike_range_expand_threshold` × šířka (25 %),
   **prodlouží se jen ten okraj, ke kterému se spot blíží**: na `spot ± strike_range_points`.
   Druhá strana zůstává — **křídla se nikdy neztrácejí**.
3. **Reset na session start**: obálka roste jen v rámci dne (odpovídá dennímu zobrazení
   heatmapy); nový den začíná znovu od spot ± 200.
4. **Strop šířky** `strike_range_max_points` (default 800 b, tj. 2× výchozí šířka)
   jako pojistka proti runaway trendu: při dosažení se obálka posouvá za spotem
   a vzdálený okraj se obětuje — jediný moment ztráty křídla, reportovaný jako
   `capped=True` (alert do UI).

## Proč je to prakticky zadarmo

Scheduler (#7) sweepuje ATM ± 30 každý cyklus a křídla jen každý k-tý cyklus —
širší obálka prodlužuje pouze občasný sweep křídel. Market data lines se nedotkne
(dávka zůstává 80; účet má ověřeno ≥ 150, ADR-0001). Sweep čas hlídá metrika
`sweep_duration`.

## Alternativy zamítnuté

- **Recenter + růst 1.5× (v1)** — ztrácí vzdálené křídlo při trendu.
- **Posun beze změny šířky** — opakované posuny, křídlo mizí průběžně.
- **Recenter + zamrzlá data vypadlých strikes** — křídlo by zůstalo viditelné, ale stale;
  horší než ho poctivě sweepovat.

## Důsledky

- API: `StrikeBand` je `[low, high]` obálka; `maybe_expand` vrací `BandExpansion`
  (band, expanded, capped) — capped je vstup pro alert engine.
- Politika je izolovaná v `ChainDiscovery.maybe_expand`; revize nemění volající kód.
