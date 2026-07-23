# ADR-0011: Flow-adjusted GEX — odhad intradenního positioningu z klasifikovaného toku

**Stav:** přijato, fáze 1 (2026-07-23, scope zadal uživatel v issue #222)
**Kontext:** OI se aktualizuje 1× denně (SPEC 3.5) — všechny GEX výpočty stojí
na ranním snapshotu a dnešní nově postavený positioning nevidí. U 0DTE, které
tvoří většinu objemu, je to díra přesně tam, kde se odehrává většina gammy.
Engine přitom klasifikuje agresora každého obchodu (R2: Lee–Ready tick-by-tick
v hot zóně, midpoint test zbytku řetězce), ale klasifikovaný tok se používal
jen pro CumΔ panel.

## Zpětná validace (2026-07-23, věčný OI archiv × denní volume)

Metodika: volume dne D per kontrakt sekundární expirace (snapshots) vs.
ΔOI téže expirace z archivu (D → D+1). Jediný ČISTÝ den okna (20. 7., ES,
expirace 20260721, 86k kontraktů volume):

- **open-ratio |ΔOI|/volume ≈ 0,39** — zhruba 40 % obchodovaného objemu se
  propíše do čistě nového OI,
- **Spearman korelace volume × |ΔOI| přes strikes ≈ 0,59** — volume slušně
  predikuje MÍSTO změny positioningu,
- jen **5 % |ΔOI| na strikech bez zachyceného volume**.

Dny 21.–22. 7. jsou znehodnocené výpadky enginu (chybějící overnight seance,
opraveno #221) — open-ratio tam vychází nesmyslně (až 106×), protože ΔOI
obsahuje objem, který sběr neviděl. Validaci průběžně opakovat na čistých
dnech (skript ve scratchpadu, výsledky v issue #222).

## Rozhodnutí (fáze 1)

1. **Per-kontrakt čistý klasifikovaný objem** (buy − sell, v kontraktech)
   akumuluje `CumDeltaTracker` vedle CumΔ — obě větve klasifikace (tick i bar),
   denní reset. Nová data se NEpersistují (odvozená veličina, levels ano).
2. **OI odhad:** `OI_est(K,s) = max(0, OI_ranní + α·net_klasifikovaný_objem)`.
   α = `GEXLENS_FLOW_OI_ALPHA` (default **0,4** z validace open-ratio; 0 =
   vrstva vypnutá). Podlaha 0 — pozice nemůže být záporná.
3. **Flow-adjusted levels:** z OI_est se počítá druhá sada flip/walls/centroid
   (stejný `NaiveDealerModel`), jen pro AKTIVNÍ řetěz (tok se měří jen tam).
   Persistence: řada `derived/{sym}/{exp}/levelsfa` (LEVELS_SCHEMA), WS kanál
   `levelsfa.{sym}.{exp}`, bundle klíč `levelsfa` — vše aditivní.
4. **UI:** přepínač „FA levels" (default off, persistováno) kreslí fa_flip /
   fa_call_wall / fa_put_wall jako ČÁRKOVANÉ linie vedle měřených — vizuální
   signál „odhad, ne měření" (konvence projekce ADR-0006). Souběžné zobrazení
   je záměr: rozdíl měřené vs. FA linie UKAZUJE, kam intradenní tok positioning
   posunul.

## Fáze 2 (později, podle zkušenosti s fází 1)

- Dyn GEX pole z OI_est (přepínač zdroje modelu).
- Heatmap OI vrstvy z OI_est.
- Kalibrace α per symbol/expirace z průběžné validace; případně asymetrická
  α pro buy/sell stranu.

## Vědomé limity

- Klasifikace agresora ≠ open/close: net buy může být closing sell longu
  protistrany; α je hrubý kalibrační faktor, ne účetnictví pozic.
- Bar větev klasifikuje midpoint testem POSLEDNÍHO trade minuty — celý
  minutový přírůstek dostane jedno znaménko.
- Overnight tok před startem enginu odhad nevidí (backfill barů podkladu
  opce nepokrývá) — odhad začíná od ranního OI + tok od startu sběru.
- Hot zóna (tick větev) zatím není v produkci zapojená — net objem dnes plní
  jen bar větev; až se zapojí, tracker ji započte automaticky.

## Ověření

- Unit: znaménka net objemu (buy/sell/unknown, midpoint nad/pod/na midu),
  denní reset, podlaha 0 v OI odhadu.
- Runtime: řada levelsfa zapsána + WS kanál publikován; α=0 vrstvu vypne.
- Frontend: bundle merge fa_ klíčů, WS append, přepínač viditelnosti.
