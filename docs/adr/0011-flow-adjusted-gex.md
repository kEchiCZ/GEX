# ADR-0011: Flow-adjusted GEX — odhad intradenního positioningu z klasifikovaného toku

**Stav:** přijato, fáze 1 (2026-07-23, #222) + fáze 2 (2026-08-06, #232)
**Částečně revidováno ADR-0057** (16. 10. 2026): čistý objem nese jen tisky se stranou agresora
od burzy (midpoint test zrušen, #1018); tisky dostává i sekundární řetěz, kalibrace α bere
expiraci, která byla celou seanci sekundárem, a při bumpu v6 se α resetuje (FA vypnutá do ≥ 5
platných dnů, výchozí α 0). Vzorec odhadu a FA validace platí dál.
**Revidováno ADR-0061** (24. 10. 2026, od bumpu v6): odhad je `OI_ráno + ρ·objem strany za
seanci` místo `α·net` — OI roste i na stranách s převahou prodeje (#1612). ρ = medián ΔOI/objem
řetězu D+1, retro ρ z archivu; pod v5 do bumpu platí tento ADR beze změny.
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
   *(ADR-0057: jen tisky dxFeed se stranou od burzy; bar větev znaménko nedává.
   Net tím nese jen outright agresi — strukturovaný objem, ES ~21 %, mění OI bez
   znaménka a α ho jen globálně rozpustí.)*
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
  *(Platí jen pro mechaniku v5; od v6 bar větev znaménko nedává, ADR-0057 bod 1.)*
- Overnight tok před startem enginu odhad nevidí (backfill barů podkladu
  opce nepokrývá) — odhad začíná od ranního OI + tok od startu sběru.
- Hot zóna (tick větev) zatím není v produkci zapojená — net objem dnes plní
  jen bar větev; až se zapojí, tracker ji započte automaticky.
  *(Neplatí: IBKR tick větev nahrazena tisky dxFeed 3. 9. 2026 (#1006) a kód smazán
  (ADR-0057 bod 10, #1460). Od mechaniky v6 plní net jen tisky dxFeed se stranou od burzy.)*

## Ověření

- Unit: znaménka net objemu (buy/sell/unknown, midpoint nad/pod/na midu),
  denní reset, podlaha 0 v OI odhadu.
- Runtime: řada levelsfa zapsána + WS kanál publikován; α=0 vrstvu vypne.
- Frontend: bundle merge fa_ klíčů, WS append, přepínač viditelnosti.

## Fáze 2 (2026-08-06, #232)

Rozhodnutí uživatele (6. 8.): FA je jeden mechanismus pro ES i NQ; výchozí
zdroj je všude MĚŘENÉ OI a FA je opt-in přepínač persistovaný per symbol;
setupy dál jedou výhradně z měřených úrovní; α se kalibruje per symbol
z ranních potvrzení proti skutečnému ΔOI z věčného archivu.

1. **Řada `netflow`** (`derived/{sym}/{exp}/netflow`): kumulativní čistý
   klasifikovaný objem per strana a minutu — jen strany s nenulovým netem,
   jen aktivní řetěz. Umožňuje zpětnou validaci směru (znaménko net vs. ΔOI),
   kalibraci α a navázání kumulativu po restartu enginu uprostřed dne
   (`CumDeltaTracker.restore_net_volume`, živé měření má přednost).
   *(Opraveno: netflow píše i sekundární řetěz (#1182). Od #1460 (ADR-0057 bod 8)
   nese řádek sloupec `source` se zdrojem znaménka (NULL = partice před #1460
   = midpoint) a navázání bere jen řádky zdroje běžícího trackeru. Uložený
   kumulativ se k toku naměřenému od startu procesu přičítá, místo aby mu
   ustoupil: tisky chodí i před prvním cyklem. Obnovují se i strany mimo
   pásmo, které má nový runtime po rollu i po restartu uprostřed seance užší
   (pásmo je grow-only); platí i pod v5. Od #1582 se zapisují
   i strany s netem 0: explicitní 0 = naměřená nula, chybějící řádek = strana
   se nesleduje; dřív strana s návratem na 0 zmizela a kalibrace α i navázání
   vzaly její poslední nenulovou hodnotu.)*
2. **Jediná definice odhadu** `compute/flowoi.oi_estimate`:
   `OI_est = max(0, OI_ráno + α·net)`. FA levels (fáze 1), FA Dyn GEX
   profil/pole i řada oiest počítají z TÉHOŽ čísla — UI nikdy neukazuje dvě
   různé „flow-adjusted" pravdy.
3. **Řada `oiest`** (per minuta × strike × right; jen strany lišící se od
   měřeného OI) + WS kanál `oiest.*` + klíč `oiest` v /replay bundle.
   Frontend z ní staví FA matici: kopie měřené s přepsanými buňkami.
4. **FA Dyn GEX**: tentýž výpočet `gamma_profile`/`gamma_field`
   parametrizovaný vstupem OI_est → řady `gexprofilefa`/`gexfieldfa`,
   WS kanály a bundle klíče. Jen gamma; charm/vanna FA variantu nemají.
5. **Kalibrace α per symbol** (ranní job po OI archivu, hned za FA validací):
   včerejší konec dne netflow (řez 21:00 UTC) vs. skutečné ΔOI mezi archivy →
   medián poměrů ΔOI/net přes strany s |net| ≥ 25 kontraktů (≥ 5 stran),
   EMA přes dny (λ 0,3), α sevřená do [0, 1]. Historie v PG tabulce
   `fa_alpha_history` (vč. buy/sell mediánů — asymetrická α se zavede až
   pokud ji data jasně ukážou; zatím jedna společná), aktuální stav
   v `fa_alpha`. Engine ji propisuje do `runtime.flow_alpha`; bez
   kalibrovaného bodu platí default `GEXLENS_FLOW_OI_ALPHA` (0,4).
   *(ADR-0057 bod 6: vstupem je tiskový net sekundárního řetězu; kalibruje se
   expirace, která byla celou seanci D sekundárem, den s mezerou trade větve bod
   nedá; při bumpu v6 reset α, FA vypnutá se stavem „nekalibrováno n/5" do ≥ 5
   platných dnů, výchozí α 0.)*
   API `GET /fa/alpha`, frontend badge „FA α=0.34 · 5 dní" ve stavové liště.
6. **UI zdroj OI** (přepínač „OI: Měřené / FA odhad", persist per symbol,
   default měřené): při FA čtou OI módy heatmapy, Dyn GEX podklad i GEX
   křivka pravého profilu FA řady; FA levels linie vychází z téhož odhadu.
   Vol/Δ Flow/Cum Δ a OI Δ složka profilu zůstávají VŽDY měřené. Aktivní FA
   značí tečkovaný chip + badge „FA odhad"; bez dat řady oiest UI poctivě
   padá na měřené a badge nekreslí. Měřený režim je bit-identický s chováním
   před fází 2 (regresní testy v loaderu).

## Průběžná validace (dodatek #232)

Denní validaci dělá engine sám: po úspěšném ranním OI archivu job
(`storage/fa_validation.py`) porovná včerejší kumulativní volume řetězce
(řez 21:00 UTC — konec trade date, counter IBKR se resetuje ve 22:00 UTC)
s ΔOI mezi archivy, spočítá open-ratio (≈ α), Spearmanovu korelaci
volume × |ΔOI| a podíl „tichých" změn, a bod uloží do PG tabulky
`fa_validation` (idempotentní upsert, dedup per symbol × expirace × den).
Výsledek jde jako informační alert `fa_validation` do zvonku. Kalibrace α
(Fáze 2) čte body přímo z tabulky — potřeba je ~5–7 čistých obchodních dní.
