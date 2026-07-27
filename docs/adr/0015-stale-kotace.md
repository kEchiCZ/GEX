# ADR-0015: Zmrzlé kotace se nesmí počítat jako čerstvá data

**Stav:** accepted · **Datum:** 2026-07-27 · Řeší issue #306.

## Kontext

27. 7. si uživatel všiml, že pravý panel nemá hodnoty mezi 7450 a 7585 (ES, spot
7492). Diagnóza odhalila vážnější problém: **TWS přestala 26. 7. ve 22:32–22:34
UTC počítat `modelGreeks` pro ATM striky.** Ceny, objem i OI chodily dál —
ověřeno čerstvým spojením mimo engine:

```
7400C [křídlo]: bid=78.0  ask=78.75  iv=0.2616  gamma=0.00239   ← OK
7500C [ATM]:    bid=5.1   ask=5.3    iv=None    gamma=None      ← bez Greeks
7600C [křídlo]: bid=0.05  ask=0.15   iv=0.2559  gamma=0.00040   ← OK
```

Engine se choval podle návrhu: kontrakty bez kompletních dat prošly repair
frontou, skončily jako `stale` a snapshoty je poctivě značily `stale_age = 999`.
**Nikdo to ale nespotřeboval.** Cache dál vracela poslední známou kotaci, takže:

- do parquet snapshotů se 15 hodin zapisovala identická čísla z 22:34
  (7500C mělo bid 17,9 / IV 0,12288 v každé minutě až do 13:53, zatímco reálná
  cena té opce byla 1,70 a IV 0,244),
- **GEX, zdi, flip, Max Pain, Dyn GEX profil i setupy se z těch fosilií celý den
  počítaly** — bez jakéhokoli náznaku, že vstup je 15 hodin starý,
- `/status` problém hlásil pořád (`greeks_complete 161 / greeks_total 222`,
  `repair_count 61` = 30 ATM striků × 2 strany), jen na to nebyl alert.

Teprve restart enginu kvůli #302 cache vyprázdnil a díra se zviditelnila.

## Rozhodnutí

1. **`stale_age` nese skutečné stáří kotace**, ne sentinel 0/999. Heatmapa
   (`STALE_THRESHOLD_S = 300`) i řetěz stale odlišit umí — dosud ale dostávaly
   binární příznak posledního sweepu, ne stáří dat.
2. **Kotace starší než `quote_max_age_s` (default 900 s) nevstupuje do výpočtů.**
   Vypadává z `GexInput`, Dyn GEX profilu i CumΔ bar větve. Řádek snapshotu
   zůstává, se svým stářím — chybějící strike v grafu je poctivější než tiše
   zkažený výpočet a uživatel si ho okamžitě všimne (což se 27. 7. potvrdilo).
3. **`GreeksStallDetector`** (obdoba `BarsStallDetector` z #221): podíl stale
   kontraktů nad `greeks_stall_share` (10 %) po `greeks_stall_cycles` (3)
   sweepech → alert `greeks_stalled` s hintem na restart TWS; návrat pod práh →
   `greeks_recovered`. Obojí právě jednou, prázdný sweep se nehodnotí.

Env: `GEXLENS_QUOTE_MAX_AGE_S`, `GEXLENS_GREEKS_STALL_SHARE`,
`GEXLENS_GREEKS_STALL_CYCLES`.

## Důsledky

- Řady `levels`, `levels2`, `walldom`, `ladder`, `gexprofile` a `gexfield`
  přeskočí striky se zmrzlou kotací. Přepočet GEX ze surových snapshotů proto
  musí použít stejný filtr (`stale_age > quote_max_age_s`), jinak vyjde jinak
  než živý výpočet.
- Default 900 s je velkorysý vůči běžnému provozu: křídla se sweepují každý
  `wings_sweep_every` (3.) cyklus a repair má 3 pokusy, takže legitimní stáří
  kotace je jednotky minut. Zachytí se tím fosilie, ne běžné výpadky.
- Data z 27. 7. mezi 22:34 (26. 7.) a 14:36 jsou znehodnocená — GEX i setupy
  z nich stavěly na zmrzlých ATM Greeks. Pro kalibraci (Fáze 2 setupů, #232)
  je nepoužívat.
- **Zbývá dořešit (frontend):** `StrikeProfile` (pravý panel) staleness nijak
  nevizualizuje, na rozdíl od heatmapy a řetězu. Protože řádky snapshotu
  zůstávají, uvidí uživatel dál pruhy objemu ze zmrzlé kotace — jen se
  neprojeví v Dyn GEX křivce a v úrovních. `stale_age` teď nese skutečné
  stáří, takže panel má z čeho ztlumit; samotné ztlumení je otevřený úkol.
