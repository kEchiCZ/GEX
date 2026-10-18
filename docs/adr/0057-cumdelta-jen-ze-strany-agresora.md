# ADR-0057: CumΔ a čistý objem jen ze strany agresora od burzy — midpoint test se ruší

- **Stav:** přijato 16. 10. 2026 (E-1.14c #1459, PR #1587). Body 1, 2 a 11 zapisují rozhodnutí
  vlastníka z 8. 10. 2026 (1A, 2A), body 6–9 jeho volby ze 16. 10. 2026 (doporučené varianty,
  Rozhodnuto v epice #1385). Body 3, 4 a 10 a věty označené „odvozeno" v bodech 6–8 vyplývají
  z 1A a z pravidel AGENTS.md; vlastník je potvrdil 16. 10. (varianty ve Zvažovaných variantách).
- **Datum:** 2026-10-16
- **Mění:** SPEC kap. 0 R2, 3.4, 4.5 · ADR-0032 bod 1, věta bodu 2 o typech trade větve, bod 3
  a Důsledky · ADR-0011 (zdroj znaménka čistého objemu, kalibrace α) · ADR-0034 bod 6 (výjimka
  pro stínovou řadu)
- **Implementace:** E-1.14d #1460 (rozdělená podle velikosti, viz Důsledky); nasazení s dávkovým
  bumpem v6 E-1.14b #1403.

## Kontext

**Verdikt #1018 (8. 10. 2026, `docs/research/1018-cumdelta-zdroj.md`).** Živá CumΔ z midpoint
testu (SPEC 4.5: znaménko `snapshot.last` proti aktuálnímu bid/ask × přírůstek objemu × delta ×
multiplikátor) jde proti pohybu ceny futures ve všech 26 seancích (13 na ES i NQ, okna 1 a 5 min)
a proti CVD futures. Tisková řada dxFeed `TimeAndSale` se stranou agresora od burzy jde s cenou.
Mechanismus: `last` je poslední obchod kontraktu, starší než snímek o libovolnou mezeru; když
futures rostou, mid callu vyjede nad starý `last` („prodej", −1 × kladná delta) a mid putu pod
něj („nákup", +1 × záporná delta) — záporný tok právě při růstu ceny. Test neměří agresora, ale
obráceně pohyb ceny od posledního obchodu.

**ADR-0032 ponechal midpoint jako fallback** per kontrakt a minutu (tisk bez strany, kontrakt bez
tisku v minutě s přírůstkem objemu) a pro celý řetěz bez tastytrade. Režim `dxfeed`, jak ho
napsal #615 fáze 3, je proto hybrid. Měření 16. 10. (doplněk protokolu #1018, skript
`scripts/compare_cumdelta_sources.py --hybrid`, rekonstrukce živé řady z uložených partic sedí
přesně) ve stejných 13 seancích na symbol:

| Medián r s cenou futures (1 / 5 / 15 min) | ES | NQ |
|---|---|---|
| tisky se stranou | +0,10 / +0,27 / +0,16 | +0,11 / +0,30 / +0,42 |
| hybrid (tisky + midpoint fallback) | +0,04 / −0,06 / −0,08 | −0,04 / −0,16 / −0,34 |
| jen fallback (kontrakt-minuty bez tisku) | −0,09 / −0,28 / −0,40 | −0,13 / −0,34 / −0,45 |
| midpoint (dnešní živá řada) | −0,16 / −0,24 / −0,33 | −0,21 / −0,36 / −0,46 |

Fallback jde proti ceně v každé seanci a v hybridu převáží: kontrakt-minuty bez tisku nesou medián
46 % (ES) a 63 % (NQ) přírůstku objemu a 49 % / 68 % absolutního toku hybridu. Pořadí A (jen
tisky) > B (hybrid) z #1018 tím platí čísly.

**Rozhodnutí vlastníka 8. 10.:** 1A — CumΔ jen z tisků od burzy, midpoint nikde, objem bez tisku
mimo tok, výpadek tasty = viditelná mezera, ne odhad; 2A — nasazení s bumpem v6. Midpoint
znaménko ale nečte jen CumΔ: inventura #1459 našla čistý objem (`_net_volume`) a z něj FA odhad
OI a kalibraci α, tendenci, verdikt dne, setupy, potvrzení zpráv v news-engine, replay
backtestu a walk-forwardu a panel Cum Δ. Co dostanou po přepnutí, shrnuje bod 5.

## Rozhodnutí

1. **Znaménko toku jen ze strany agresora od burzy.** flowΔ, CumΔ i čistý objem kontraktu
   (`_net_volume`, buy − sell v kontraktech, vstup FA odhadu OI) se plní jen z tisků dxFeed
   `TimeAndSale` se stranou (`aggressorSide` BUY → +1, SELL → −1):
   `flowΔ += sign · size · Δ(K,s) · M`, `net(K,s) += sign · size`. Midpoint test se ruší všude —
   v aktivním i sekundárním řetězu a v replayi. Bar větev (přírůstek kumulativního objemu ze
   snímku) dál rozkládá objem na tisky a zbytek (`printvol`, pokrytí), znaménko nedává.
2. **Mimo tok:** tisk bez strany, tisk bez delty (počítá se v `dropped_no_delta`), objem bez
   tisku (nohy spreadů a bloky — CME je jako trade nevysílá, ADR-0032 bod 4 — i kontrakt-minuta
   bez jediného tisku). Minuta bez tisků za běžící trade větve je tok 0 (měřený klid), ne
   mezera; totéž platí pro minutu, ve které by žádný tisk neměl deltu (dnes `dropped_no_delta`
   0). Objem mimo tok se dál měří (`printvol`, pokrytí v `flow` a `/status`).
3. **Výpadek trade větve = viditelná mezera** (odvozeno z 1A a „Fail fast, viditelně"):
   - Minuta, ve které trade větev celou dobu neběžela, má `flow_delta` i `cum_delta` NULL — ne 0,
     ne poslední hodnotu, ne odhad; sloupce pokrytí také NULL. Částečná minuta je NULL celá
     (cenzurovaná data se neextrapolují). Mezera cestuje s hodnotou do WS, `/replay`, feature
     logu i news-engine; všichni čtenáři jsou na NULL připravení v tomtéž nasazení.
   - Po mezeře CumΔ pokračuje od poslední měřené úrovně (kotva seance #638, precedens ADR-0024
     bod 3); UI ukazuje mezeru, ne spojnici. Tisky z doby výpadku se nedoplňují.
   - Trade větev „běží" = připojený stream ∧ čerstvé eventy symbolů aktivního řetězu (prahy
     stávajícího hlídače 180 s RTH / 600 s Globex, bez nové konstanty) ∧ univerzum patří aktuální
     pipeline (po rollu v settle až po obnově plánu). Při zavřeném trhu
     (`marketclock.is_market_closed`) se mezera nezapisuje a nic se neupozorňuje. Upozornění
     zůstávají stávající (výpadek tasty, mlčící feed) s upraveným textem, nový druh nevzniká.
     Vědomý limit: mlčící stream je do prahu hlídače (3 min RTH, 10 min Globex) „běžící" a jeho
     minuty jsou tok 0; zpětně se neoznačují (point-in-time).
   - Výpadek IBKR řetězu tok z tisků nezastaví (delta je z tasty greeks) — otevřený trh = vidět
     vše.
4. **Spotřebitelé při mezeře** (odvozeno): chybějící vstup není filtr podle stavu trhu, jen
   nepotvrzená podmínka.
   - Setup, jehož podmínka čte CumΔ (T1 divergence, T4 kvantilová brána, kontra-režim T1/T2,
     T5 `divergence_spring` — dnes vypnutá, ve store jde zapnout), nevznikne, je-li mezera
     v aktuální minutě nebo kdekoli v okně podmínky (T1 10 min, T4 a kontra-režim 30 min, T5
     okno spring) — rozdíl přes mezeru je neúplný. T2 mimo kontra-režim, T3 a T7 běží.
   - Tendence: hlasy sklon CumΔ a divergence při neúplném okně nehlasují. Sklon počítá jedna
     čistá funkce „Σ flowΔ za 10 min v seanci" v `compute/`; volá ji tendence i news-engine
     (DRY; precedens ADR-0045 bod 5). Okno nepřekročí začátek seance — dnešní sklon přes reset
     v 17:00 CT je nález #1581.
   - news-engine: chybějící sklon = noha COMBINED nepodporuje (flip může), cyklus nespadne.
   - FA vrstva (oiest, levelsfa, gexprofilefa, gexfieldfa) se v mezeře nepíše; frontend
     poslední FA profil nedrží a OI matici nepřepíná potichu na měřené.
5. **Co spotřebitelé dostanou po přepnutí:**

   | Spotřebitel | Po přepnutí | Při mezeře |
   |---|---|---|
   | Panel Cum Δ (plocha, TF, Daily, chip okna) | tisky | mezera (bod 3) |
   | Čistý objem → FA odhad OI | tisky (outright agrese) | FA se nepíše |
   | Kalibrace α (sekundární řetěz) | tisky (bod 6) | den bez bodu |
   | Tendence: sklon CumΔ a divergence | tisky | nehlasují |
   | Verdikt dne, automatický scénář (přes tendenci) | tisky | jako tendence |
   | Setupy T1, T4, kontra-režim, T5 (vypnutá); feature log | tisky | setup nevznikne |
   | Setupy T2 mimo kontra-režim, T3, T7; Δ Flow C/P, CVD futures, FA validace, paper, kouč | beze změny | — |
   | news-engine: potvrzení COMBINED sklonem | tisky | noha nepodporuje |
   | Replay backtestu, walk-forward, parita | tisky (bod 7) | mezera |

   Měřítko tiskové řady je 0,2–0,4× midpointové. Prahy čtenářů jsou směrová a kvantilová
   srovnání, na měřítko necitlivá — mění se obsah (směr), takže se přeměřuje: parametry setupů
   čtoucí CumΔ (`momentum_cum_quantile`, `counter_flow_lookback`, `divergence_lookback`),
   všechny vzorky v6 (vzorky v5 se nepřenášejí), váhy tendence (sklon a divergence společně,
   bod 9), verdikt dne (nová verze), vzorek COMBINED v news-engine a prahy kalibrace α
   (`MIN_ABS_NET`, `MIN_SAMPLES`, `ALPHA_MAX`, výchozí α). Absolutní čísla (Daily, detail
   tendence) se mezi dny před a po v6 nesrovnávají.
6. **FA odhad OI a α (vlastník 16. 10., varianta A).** Tisky dostává i tracker sekundárního
   řetězu — jeho `TimeAndSale` už odebírá účel `chain`, takže 0 položek tasty navíc a 0 ze 100
   IBKR lines. Kalibrace bere expiraci, která byla celou seanci D sekundárem (oprava pasti D+2,
   nález #1580). Při bumpu je FA vypnutá se stavem „nekalibrováno n/5", dokud nemá ≥ 5 platných
   dnů; zpětná kalibrace z recorderu `data/trades` běží jen na sucho jako podklad prahů.
   (*odvozeno:* α se při bumpu resetuje — jiná veličina, historie zůstává a nese zdroj; den
   s mezerou trade větve za otevřeného trhu bod nedá; výchozí α 0 místo 0,4, jinak by FA po
   resetu jela na nekalibrovaném defaultu.) Vědomý limit: net nese jen
   outright agresi, strukturovaný objem (ES ~21 %, NQ ~10 %) mění OI bez znaménka a α ho jen
   globálně rozpustí.
7. **Replay a walk-forward pod v6 (vlastník 16. 10., varianta A).** Replay backtestu čte pro
   seance od 21. 9. 2026 do nasazení stín `derived/{sym}/cumdelta_dx` — tentýž model jako 1A
   (univerzum aktivního řetězu, delta z tasty greeks, strana od burzy), přeskočená značka minuty
   = poslední kumulativ, posun značek ~1 min. Stín píše řádek i při odpojeném tasty (tok 0),
   mezera ve stínové historii se proto určí z `flow.printed_volume` NULL nebo `printvol.printed`
   NULL (#1071), aby replay i brána parity měly tutéž sémantiku jako živá 1A. Od nasazení čte
   řadu `flow` se zdrojem 1A; starší seance jsou mezera a walk-forward v6 je nepoužije. **Výjimka z ADR-0034 bod 6** (učení přes
   změnu zdroje): stín a 1A jsou táž mechanika. Brána: parita stín ↔ živá 1A ≥ 5 seancí po
   bumpu s číselným prahem (stanoví #1460); když neprojde, walk-forward v6 jede jen nad živou 1A
   (první návrh koncem prosince místo poloviny listopadu). (*odvozeno:* před bumpem se přehraje
   v5 × v6 nad 21. 9.–15. 10. pro všechny šablony — dopad přepnutí na množinu setupů je vidět
   dřív, než se nuluje vzorek.)
8. **Zlom midpoint → tisky v datech a v UI (vlastník 16. 10., varianta A).** Zlom nesou řádky,
   ne datum (přežije návrat pinem image): nová hodnota `flow.source` (ne `dxfeed`, ten značil
   hybrid), `source` v řadě `netflow`, `TENDENCY_WEIGHTS_VERSION` 5, `VERDICT_RULES_VERSION` 4
   (statistiky podle verze), zdroj CumΔ ve vstupech signálu COMBINED a v `fa_alpha_history`;
   zdroj CumΔ je dohledatelný i z feature logu. Panel Cum Δ dny před v6 nekreslí — mezera
   s důvodem „midpoint, obrácené znaménko (#1018)", linka CVD futures zůstává. Data v `derived/`
   se nemažou ani nepřepisují. (*odvozeno:* engine a frontend se nasazují a vracejí pinem
   společně — verze verdiktu a skrytí historie stojí na obou.) Od #1595 verdikt počítá
   a zapisuje jen engine (ADR-0035 dodatek 2026-10-17): `VERDICT_RULES_VERSION` 4 se bumpuje
   v enginu, frontend verzi verdiktu nenese.
9. **Tendence v E-0.12 (vlastník 16. 10., varianta A).** Sklon CumΔ a divergence se z revize vah
   nad midpointem (#1113) vyřazují a měří se offline ze stínu (21. 9.–dnes, RTH, bootstrap po
   dnech). „Signál" NQ RTH z #394 (sklon +0,108, divergence +0,096) stál na midpointu, tedy na
   obráceném pohybu ceny; jeho ztráta se přijímá a nový hlas by šel jen samostatným návrhem.
10. **Konfigurace a kód** (odvozeno): přepínač `GEXLENS_CUMDELTA_SOURCE` a režim midpoint končí,
    návrat jen pinem image. Mrtvá tick větev (`add_trade`, `ClassifiedTrade`, `TradeSide`)
    se maže — strana z Lee–Ready nad bid/ask není strana od burzy. `/status` hlásí zdroj a stav
    mezery per symbol (jen API, bez diagnostického UI).
11. **Nasazení (2A):** s bumpem v6 (E-1.14b #1403). Čtenáři NULL (API, frontend, news-engine)
    se nasazují nejpozději s enginem; do `main` smí dřív jen kód, který je pod režimem midpoint
    netečný.

## Zvažované varianty

- **Opravit midpoint** (kotace v čase obchodu, tick test) — zamítnuto 8. 10.: snímek kotaci
  z doby obchodu nedá (rotační sweep, `last` starší o libovolnou mezeru) a strukturovaný přírůstek
  obchod nemá vůbec; tick test by byl tentýž artefakt s opačným znaménkem (klasifikuje pohyb ceny,
  ne agresora).
- **Hybrid = dnešní režim `dxfeed`** (ADR-0032 bod 1) — zamítnuto měřením 16. 10. (tabulka
  v Kontextu): horší než čisté tisky ve všech oknech, u NQ proti ceně.
- **Midpoint jen pro čistý objem, FA nebo sekundární řetěz** — odporuje 1A a dává dvě definice
  téže veličiny (α kalibrovaná na jiném netu, než který násobí).
- **Mezera jako držená úroveň s příznakem, nebo řádek vůbec nepsat** (k bodu 3) — zmrzlé číslo
  pro každého, kdo příznak nečte, resp. výpadek tasty k nerozeznání od výpadku enginu.
- **Po mezeře nový segment CumΔ od nuly** (k bodu 3) — ztratí kotvu seance a srovnatelnost
  s CVD futures; pokračování od poslední úrovně s viditelnou mezerou je méně matoucí.
- **Částečná minuta se součtem tisků** (k bodu 3) — cenzurovaná minuta by vypadala jako měřená.
- **Detekce mezery jen podle připojení streamu, nebo vlastní kratší práh mlčení** (k bodu 3) —
  první nepozná mlčící socket (#1228) a zastaralé univerzum po rollu, druhý přidává konstantu;
  prahy stávajícího hlídače za cenu 3–10 min toku 0 na začátku výpadku.
- **Nový druh upozornění `cumdelta_gap`** (k bodu 3) — dubluje stávající hlídání výpadku tasty
  a potřebuje vlastní bránu zavřeného trhu a testy.
- **Setup s mezerou jen v kraji okna** (k bodu 4: stačí aktuální minuta a začátek okna) —
  rozdíl přes mezeru by neobsahoval tok z výpadku, tedy neúplné číslo jako měřené; varianta
  „podmínku při mezeře přeskočit" by vydala setup s textem o divergenci, která se neměřila.
- **Hlas tendence 0 při mezeře** (k bodu 4) — „Cum Δ +0" by tvrdil měřený klid a ředil skóre.
- **Ponechat přepínač `GEXLENS_CUMDELTA_SOURCE`** (k bodu 10) — návrat k midpointu je zamítnutý
  1A; návrat celé verze řeší pin image, další konfigurační klíč nikdo nepotřebuje (YAGNI).
- **Doplnit výpadek odhadem** (např. přeškálovanou CVD futures) — odhad místo měření.
- **FA:** α zmrazit (tiše chybná čísla), α z open-ratio validace (jiná veličina), α neresetovat
  (1–2 týdny špatně škálovaná FA), FA vrstvu odložit (vlastník ji chce, varianta A).
- **Walk-forward jen nad živou 1A** — návrh až koncem prosince; zůstává jako záloha, když brána
  parity neprojde.
- **Midpointová historie v panelu s popiskem** „midpoint (před v6)" — kreslila by obrácenou řadu
  vedle správné.
- **Tendence: čekat 4–5 týdnů** na živá data po v6 — E-0.12 by kalibrovalo obrácenou řadu.

## Důsledky

- **ADR-0032:** bod 1 (midpoint fallback per kontrakt a minutu a bez tasty), bod 3 (znění R2)
  a Důsledky 1–3 nahrazuje tento ADR; věta bodu 2 o typech `ClassifiedTrade`/`TradeSide`
  neplatí (bod 10); bod 4 a doplněk 4. 9. platí dál. Bod 1 tvrdil „všechny sbírané expirace",
  kód ale tisky vedl jen do aktivního řetězu; po bodu 6 je dostává aktivní i sekundární řetěz.
- **ADR-0011:** čistý objem = jen outright tisky se stranou; kalibrace α podle bodu 6.
- **ADR-0034 bod 6:** výjimka pro stínovou řadu podle bodu 7.
- **SPEC** kap. 0 R2, 3.4 a 4.5 přepisuje tento PR. Manuály, texty v UI a `.env.example` se mění
  s implementací (#1460), stejně jako docstringy `compute/cumdelta.py` a `runtime.py`.
- **Zdroje dat se nemění:** počet IBKR market data lines ani rozpočet tasty (TimeAndSale obou
  řetězců už odebírá účel `chain`); R5 platí.
- **Plánovaní spotřebitelé (MCP, Fáze 3 epiky #1385):** nástroje toku (E-3.9 #1439, E-3.22
  #1450) a FA varianta znaménka dealerů (E-3.8 #1438) čtou jen tiskovou řadu a FA až po #1460 —
  brána epiky „MCP nástroje toku na tiskové řadě (#1018)" platí i pro FA.
- **Historie se nemaže:** `derived/` (midpoint `flow`, `netflow`, FA řady) i `fa_alpha_history`
  zůstávají jako audit; čtenáři je rozliší podle zdroje.
- **Implementace** zasahuje engine, API, frontend, news-engine a skripty, je proto rozdělená
  podle velikosti (vlastník 16. 10.): E-1.14d #1460 engine a datový kontrakt, E-1.14e #1588
  API a frontend, E-1.14f #1589 news-engine, E-1.14g #1590 replay, walk-forward a přehrání
  v5 × v6. Podrobný seznam míst je v #1460.
- **Otevřené otázky** (neblokují ADR): stáří delty tisku z tasty greeks, na které ADR-0015
  nedosahuje; zda DXLink umí historii `TimeAndSale` (`fromTime`) pro doplnění výpadku — nejdřív
  rešerše.
- **Tisky bez strany** jdou mimo tok (bod 2). Počtem jsou ~0,05 % tisků, objemem ale ES 1–15 %
  objemu tisků za den (medián ~6 %, 21. 9.–16. 10.), NQ < 1 %: téměř vše bloky ≥ 100 lotů daleko
  OTM s malou deltou. Tok tím téměř nezmění, z čistého objemu per strike (FA odhad OI) ale
  vypadnou celé bloky po 2 000–3 500 lotech. Docstring `compute/cumdelta.py` („~0,05 %") opraví
  #1460.

## Doplněk (18. 10. 2026, #1460 část 1a)

Zpřesnění, která zavádí implementace části 1a a body 3–4 je výslovně nejmenují. Nové rozhodnutí
nepřinášejí, jen popisují, jak kód mezeru měří.

- **Netflow v minutě s mezerou se nepíše** — vedle FA vrstvy (bod 4) ani řada `netflow`: net
  v té chvíli nese tisky částečné minuty, které vrátí až uzávěrka minuty. Chybějící řádek znamená
  „nesleduje se", čtenáři berou poslední měřený řádek strany (`runtime.py`, zápis netflow pod
  `tracker.minute_measured`).
- **Sekundární řetěz měří vlastní univerzum:** verdikt jeho minuty plyne z eventů jeho části tras
  (expirace musí sedět na běžící řetěz), ne z aktivního řetězu; s cyklem po N minutách drží AND
  přes vzorky celého okna (`tasty/trade_branch.py`, `TradeBranchMonitor.sample`;
  `compute/cumdelta.py`, `observe`).
- **Reconnect mezi vzorky je mezera:** vzrostlo-li od minulého vzorku počítadlo reconnectů
  streamu, okno dostane vzorek „neběží", i když stream v okamžiku vzorku zase běží — tisky z doby
  pádu chybí (`TradeBranchMonitor.sample`, `dropped`).
- **Ticho univerza se počítá nejdřív od openu seance:** poslední event z pauzy, víkendu nebo
  svátku nedá mezeru hned v první minutě seance (`trade_branch_running`, `session_open`).
- **První minuta po startu procesu za otevřeného trhu je mezera:** hranice prvního okna známá
  není, tracker neviděl tisky před svým vznikem; za zavřeného trhu nic nechybí a minuta je měřená
  (`compute/cumdelta.py`, `observe`).
- **Bar po nekrmeném cyklu jen založí stav:** kontrakt, který v cyklu nedostal bar (fallback
  řetězu s `volume=None`, kotace nad `quote_max_age_s`, pád cyklu po `begin_cycle` mezi verdiktem
  a bary), ztratí stav objemu. Jeho další bar nese přírůstek celé mezery, proto do `printvol` ani
  pokrytí nejde a jen znovu založí stav jako po restartu; tok i čistý objem za mezeru nesou tisky
  (`compute/cumdelta.py`, `begin_cycle`; doplněk AC v #1460 ze 16. 10.). Cyklus, který spadl
  před `begin_cycle` (sweep), řádek nezapíše a stav nemaže: další řádek i jeho bar nesou tentýž
  delší interval.
- **Bar bez přírůstku tisky nespotřebuje:** bar s přírůstkem 0 (kotace z cache mezi sweepy
  křídel po `wings_sweep_every` cyklech, zamrzlá kotace pod `quote_max_age_s`, objem IBKR pozadu
  za tisky) je krmený, počítadla tisků ale nevyprázdní — rozloží je až bar, který objem přinese.
  Jinak by jeho rozklad tisky dřívějších minut ztratil a připsal je struktuře. Padne-li mezi tím
  minuta s mezerou, je rozklad toho baru NULL a do pokrytí nejde: kotace z cache nemusí být
  čerstvá, interval tisků tak sahá přes mezeru (`add_volume`, kontrola části 1a 19. 10.).
- **Podíly pokrytí ve `/status` jen ze spárovaných tisků:** `printed_share` a
  `printed_share_total` počítá `day_stats` z tisků, které rozložil měřený přírůstek objemu.
  Tisky z minut bez baru kontraktu (fallback řetězu, stará kotace, první bar po startu) nese
  `printed_volume` i řádek `flow` na intervalu toku, objem k nim ale chybí a podíl by nadsadily
  (fallback 2. 10. pokryl většinu RTH). Součet řádků `flow` za den s fallbackem řetězu
  (`storage/cumdelta_compare.py`) je proto pro podíl tisků jen horní odhad.
