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
   bumpu s číselným prahem (doplněk 21. 10. 2026); když neprojde, walk-forward v6 jede jen nad živou 1A
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
  Jinak by jeho rozklad tisky dřívějších minut ztratil a připsal je struktuře. Počítadla vyprázdní
  každý jiný bar: bar s přírůstkem (rozklad), bar s poklesem kumulativu (nekonzistence feedu)
  a bar, který stav objemu zakládá (první bar dne, po restartu, po nekrmeném cyklu) — poslední dva
  bez rozkladu i pokrytí. Rozklad baru s přírůstkem je NULL a do pokrytí nejde, když je mezerou
  minuta toho baru nebo minuta některého baru bez přírůstku od posledního vyprázdnění: kotace
  z cache nemusí být čerstvá, interval tisků tak sahá přes mezeru (`add_volume`, `_gapped`,
  kontrola části 1a 19. 10.).
- **Limit: rozklad baru sahá za řez vzorku.** Verdikt minuty M pokrývá tisky do řezu ve vzorku M
  (`seal`), bar minuty M ale přichází až po sweepu (`__main__.py` vzorek → `runtime.run_cycle`:
  sweep → `begin_cycle` → bary) a vyprázdní i tisky, které přišly mezi řezem a barem — `seal`
  počítadel tisků od baru nesahá. Rozklad baru M tak čte verdikt M nad intervalem, jehož konec
  pokrývá až vzorek M+1. Spadne-li stream mezi vzorkem M a barem M, zapíše se rozklad baru M jako
  měřený, i když mu tisky z výpadku chybí (`printed` nižší, `structured` vyšší), a pokrytí z něj
  započte strukturu i spárované tisky. Řádek M+1 je pak mezera a jeho tisky se z `printed_volume`
  vrátí, spárované tisky (`_paired_printed`) je ale nesou dál — nejsou podmnožinou tisků
  v řádcích `flow`. Tok, CumΔ ani čistý objem se to netýká (ty řeže `seal` přesně na verdikt).
  Rozklad (`printvol`) a pokrytí (řádky `flow`, podíly ve `/status`) jsou proto metrika kvality
  dat, ne vstup obchodního rozhodnutí; kód limit nehlídá (kontrola 85bb08b, 19. 10.).
- **Podíly pokrytí ve `/status` jen ze spárovaných tisků:** `printed_share` a
  `printed_share_total` počítá `day_stats` z tisků, které rozložil měřený přírůstek objemu.
  Tisky z minut bez baru kontraktu (fallback řetězu, stará kotace, první bar po startu) nese
  `printed_volume` i řádek `flow` na intervalu toku, objem k nim ale chybí a podíl by nadsadily
  (fallback 2. 10. pokryl většinu RTH). Bez spárovaného objemu (start do fallbacku řetězu) je
  podíl `null`, ne 0 — nic se neměřilo (#1172); pod midpointem zůstává 0 jako dřív. Součet řádků
  `flow` za den s fallbackem řetězu (`storage/cumdelta_compare.py`) je proto pro podíl tisků jen
  horní odhad.

## Doplněk (20. 10. 2026, #1460 část 1b)

Zpřesnění bodů 3, 4, 6 a 8, která zavádí část 1b. Nové rozhodnutí přinášejí jen volby
vlastníka v epice #1385: z 19. 10. 2026 E-0.12 varianta C a zobrazení hlasu s vahou 0
(varianta 1), z 20. 10. 2026 mezera rollu v settle varianta C („chci to přesně“).

- **Tendence verze 5** (body 4 a 8, `compute/tendency.py`, `tendency.py`). Zapíná ji
  mechanika v6 přes instanční pole `TendencyEngine.cumdelta_prints`, pod v5 počítá dál v4.
  - **Sklon CumΔ** = `cumdelta.flow_window_sum` nad řádky `flow` (Σ flowΔ za
    (now − 10 min, now] v seanci). Neúplné okno, mezera nebo okno přes začátek seance =
    sklon `None`, sklon ani rozchod nehlasují. Rozchod bere cenu v minutě hranice okna
    bez ohledu na seanci, protože cena je přes pauzu spojitá (#1581 varianta A). Za
    zavřeného trhu nese řada nuly, sklon je tedy 0 (měřený klid).
  - **Max Pain** hlasuje jen 0–90 min do settle. Ve v4 chyběla spodní mez a hlasoval
    po settle až do 00:00 UTC (#1584).
  - **Hystereze pásma** se resetuje s obchodním dnem (`trading_session_date`, 17:00 CT),
    ne v 00:00 UTC. **SentIndex** hledá hodnotu před oknem i ve včerejší UTC partici.
  - **Poloha mezi zdmi má váhu 0** (E-0.12 C, #1113). Hlas se dál počítá a ukládá
    s `weight` 0, zašednutí v rozpadu je frontend (#1588). Minuta, ve které hlasuje jen
    složka s vahou 0, nemá výsledek (jako minuta bez dat), ne skóre 0. Badge
    „nekalibrováno“ zůstává, protože C není kalibrace vah.
- **Verdikt dne verze 4** (bod 8): pravidla se nemění, verze jen značí zlom vstupu.
  Golden v3 platí pro obě verze.
- **Kalibrace α pod tisky** (bod 6, `storage/fa_calibration.py`):
  - kalibruje se jen řetěz, který byl celou seanci D sekundárem, tedy první expirace
    po D; náhradní řetěz se nehledá (#1580);
  - bere se jen netflow se zdrojem `aggressor`.
- **Den s mezerou** se pozná ze značky `derived/{sym}/{exp}/netgap/`. Runtime aktivního
  i sekundárního řetězu do ní zapíše minutu, kterou za otevřeného trhu neměřil.
  - Značka je nutná, protože sekundár řádek `flow` nepíše a jeho netflow chodí jen
    každý N-tý cyklus (u ES ~3 min, mezery 1–5 min). Chybějící netflow tak mezeru
    od běžné kadence neodliší.
  - Den bez bodu je i takový, jehož řada netflow končí víc než 5 min před řezem. Engine
    spadlý přes konec dne totiž značku do seance D nezapíše.
  - **Pravidlo platí bez výjimky.** Roll v settle mezeru nedělá, viz následující bod.
- **Roll v settle předá stav kalibrovaného řetězu** (varianta C, Rozhodnuto 20. 10.; zpřesňuje
  bod 3 „po rollu v settle až po obnově plánu“ a doplněk 18. 10. „expirace musí sedět na
  běžící řetěz“).
  - Řetěz D+1 si nechá tracker sekundáru, mění se jen runtime kolem něj. Nová pipeline ho
    převezme, je-li D+1 její aktivní expirací (`EngineRuntime.take_over`,
    `CumDeltaTracker.promote`). Net v paměti, tisky ještě neuzavřené v řádku i vzorky okna
    zůstávají. Navázání z partice netflow se podruhé nespouští.
  - **Pásmo řetězu přes roll jen roste** — pod v6 výjimka z ADR-0002 bod 3 („reset na
    session start“): nový aktivní runtime začne v pásmu kolem spotu rozšířeném o striky
    sekundáru (`discovery.band_covering`). Strany, které sekundár měřil, tak dostávají tisky
    i kotace až do řezu dne. Širší pásmo pak drží celou seanci D+1, dokud pipeline žije; dál
    roste podle ADR-0002 bodů 2 a 4. Náklad: delší je jen sweep křídel jednou za
    `wings_sweep_every` cyklů, linky ani `batch_size` se nemění. Bez toho by strany mimo nové
    pásmo tisky ztratily: 19. 10. 2026 u NQ 8 strikových párů (29920–29990), na kterých
    leželo ~8,5 % lotů tisků D+1 poslední hodiny před settle. Pod v5 se pásmo v rollu
    resetuje jako dřív.
  - **Trasy tisků patří trackeru, ne pipeline.** Pod tisky nese každá část tras tracker, pro
    který se stavěla. Tisk jde do něj (`route_trade`) i mezi zastavením staré a založením nové
    pipeline, ale jen dokud čeká předání (`pending_handovers`): symbol bez pipeline a bez
    předání tisky nedoručuje nikam jako v části 1a. Vzorek uzná trasy podle identity trackeru
    (`owned_universe`), takže trackeru nového aktivního runtime patří sekundární část tras
    ještě před obnovou plánu. Ostatní trasy nové pipeline přestaví plán subskripcí do sekund
    po založení (`on_pipeline_created`), spolu s odběrem tisků, ne dřív: trasa, po které
    tisky ještě nechodí, by neúplnou minutu potvrdila (kotace z účelu extended chodí). Nový
    tracker (sekundár D+2, pipeline založená znovu nad týmiž expiracemi po přepojení IBKR)
    má proto mezeru v prvních dvou oknech. Do trackeru jiné expirace tisk nejde ani teď.
  - **Subskripci tisků řetězu D+1 drží čekající předání.** Plán tasty subskripcí bere účel
    chain z běžících pipeline a z čekajících předání. Bez toho by v okně mezi zastavením
    a založením odhlásil TimeAndSale D+1 (19. 10. 2026 pod v5 ticho tisků D+1 ES
    20:01:44–20:03:34 UTC, NQ 20:02:05–20:03:57 UTC). Vzorek by tu ztrátu nepoznal, protože
    kotace z účelu extended chodí dál. Předání se proto ze slovníku bere až po založení
    pipeline.
  - **Hranice** je vzorek minuty rollu, ve kterém aktivní řetěz D uzavřel poslední řádek.
    Od ní začínají metriky aktivního řetězu jako u nového trackeru: tok do CumΔ, pokrytí,
    podíly a rozklady barů. První bar stav objemu jen založí. Tok a pokrytí sekundáru před
    hranicí do řádku aktivního řetězu nejdou. CumΔ symbolu dál navazuje z flow aktivního
    řetězu, kumulativ sekundáru do ní nepatří.
  - **První minuta aktivního D+1 je měřená**, když vzorky pokryjí hranici. Její verdikt
    zahrnuje i okno, které sekundár ještě neuzavřel (cyklus po N minutách). Výpadek v tomto
    okně proto udělá mezeru z prvního řádku aktivního řetězu a vrátí celý uzavřený úsek
    netu. Konzervativní je to jen o pár minut navíc, kalibraci to nemění: den s mezerou bod
    nedá tak jako tak.
  - **Mezera rollu v CumΔ** z bodu 3 pod v6 zaniká. Nový sekundár D+2 má nový tracker
    s mezerou v prvních dvou oknech (viz výše). V seanci D kalibrovaný není (#1580) a jeho
    net seance D+1 začíná až jejím openem.
  - **Předání platí jen v první iteraci po rollu.** Nevznikne-li nová pipeline hned
    (spojení, chyba setupu, cooldown), tracker propadá s varováním a první minuta nového
    je mezera jako po restartu. Tracker, který vynechal vzorek, by okno potvrdil bez
    doložení. **Restart procesu zůstává mezerou.**
  - Pod v5 (midpoint) se nic nepředává. Trasy i vzorky se řídí pozicí a expirací jako
    v části 1a, pásmo začíná kolem spotu a net sekundáru se po rollu navazuje z partice.
- **Reset α při bumpu je automatický.** `fa_alpha` i `fa_alpha_history` nesou `source`
  (aditivní sloupec, NULL = midpoint).
  - Pod tisky se stav jiného zdroje bere jako nekalibrovaný a první bod jde přímo.
    Ruční `set_state(symbol, 0.0, 0)` z plánu 1a tím odpadá a nemůže se zapomenout.
  - FA vrstva dostane α až po 5 dnech kalibrace z tisků (`FA_MIN_DAYS`), do té doby
    0, i bez stavu. Výchozí α z konfigurace se pod tisky nečte ani bez nastavené α
    (start, chyba DB; `EngineRuntime.fa_alpha`), takže „výchozí α 0“ z bodu 6 plní
    tato brána.
- **Řez dne kalibrace α a FA validace** je 16:00 CT dne D (`marketclock.daily_close_utc`),
  bez přepínače. Pevných 21:00 UTC by od konce US letního času řízlo v 15:00 CT.
- **Mimo část 1b** zůstávají dva podklady z dat: číselný práh parity stín ↔ 1A (bod 7)
  nese #1590 a retro α z recorderu na sucho jako podklad prahů nese #1627.

## Doplněk (21. 10. 2026, #1590)

Zpřesnění bodu 7, které zavádí #1590. Nové rozhodnutí je jen číselný práh parity
(vlastník 21. 10. 2026 v epice #1385, varianta B).

- **Zdroj CumΔ replaye** (`scripts/backtest_setups.py`, `build_cum_series`):
  - mechanika v5 čte jen řádky `flow` se zdrojem midpoint (NULL = midpoint), minuty jiného
    zdroje jsou mezera;
  - mechanika v6 volí zdroj per seance podle řádků, ne podle data. Seance s aspoň jedním
    řádkem `flow` se `source = aggressor` **za otevřeného trhu** čte 1A a řádky jiného zdroje
    v ní jsou mezera. Řádky z denní pauzy se nepočítají: nasazení v pauze 16:00–17:00 CT
    patří ještě seanci D, jejíž RTH je midpoint, takže D čte stín a 1A až D + 1 (bez toho by
    RTH seance D bylo mezerou). Nasazení uprostřed seance (výjimka mimo pauzu) dá seanci 1A
    a její úsek před nasazením je mezera. Ostatní seance od 21. 9. 2026 čtou stín
    `cumdelta_dx`, starší seance jsou mezera. Seance = `trading_session_date` (17:00 CT),
    sobota a neděle před openem jsou pseudo-dny;
  - chybějící CumΔ je `None`, ne 0. Minuta bez řádku bere pod v6 poslední hodnotu téže seance
    (živý `runtime.last_flow`), pod v5 je jako dřív mezerou.
- **Štítek stínu → minuta rozhodnutí.** Štítek L nese stav až v L + φ (φ 0–60 s, flush stínu
  není srovnaný na minutu, #1586), patří proto rozhodovací minutě L + 1 (bar N − 1 + stav N,
  bez look-ahead). Úroveň = `cum_ring + cum_hot`.
  - Jeden přeskočený štítek = poslední kumulativ (bod 7). Díra ≥ 2 štítků za otevřeného trhu
    je mezera a mezerou je i první minuta po ní.
  - Mezera platí pro štítek L, leží-li v ní L nebo L + 1 (po přeskoku i L − 1). Sémantika
    je mezera 1A podle enginu, který minutu zapsal: minuta v5 = `flow.printed_volume` NULL
    nebo `printvol.printed` NULL; minuta s řádkem `aggressor` = `flow.cum_delta` NULL, protože
    `printvol.printed` NULL pod tisky nese až bar s přírůstkem, klidně o desítky minut
    později. Řádek, který po posunu padne do jiné seance, se zahodí.
- **Mezera mezi řádky** (`cum_gap_before`) má v replayi tutéž sémantiku jako živý
  `SetupEngine`: mezi minulým a tímto řádkem historie leží minuta s mezerou CumΔ (minuta
  bez baru nebo levels do historie nejde), pod v6 i hranice seance 17:00 CT (#1581).
- **Walk-forward** (`scripts/walkforward_setups.py`): `--mechanics` (default mechanika enginu)
  a `--from` (první seance; pod v6 default 21. 9. 2026, dřívější datum skript odmítne).
  Hlavička reportu i JSON nesou mechaniku a začátek, baseline i mechaniku verze ze store.
- **Práh parity stín ↔ 1A (varianta B).** Měří `scripts/replay_parity.py --mechanics 6
  --cum-source stin` nad ≥ 5 seancemi po bumpu: CumΔ replaye ze stínu i v seancích s 1A
  (touž transformací jako historii, štítek L → minuta L + 1) proti živému feature logu. Po
  bumpu stín štítky zarovnává (#1586), posun o 1 min je tam celé zpoždění, kdežto historie
  se zpozdí o 1 min − φ; brána tak měří horní mez zpoždění. Projde, když platí všechno:
  - medián r 10minutových přírůstků CumΔ ≥ 0,85 a žádná seance pod 0,75;
  - shoda znaménka nenulových přírůstků ≥ 80 % v každé seanci;
  - minut s mezerou jen v jedné řadě ≤ 2 % ze všech seancí dohromady; NaN × NaN se za shodu
    nepočítá;
  - seance bez r nebo bez shody znaménka (málo úplných oken) bránu nedoloží.

  Strop daný samotným posunem štítku o 1 min při shodném modelu (14.–20. 10. 2026): ES r
  0,843–0,950 (medián 0,916), NQ 0,900–0,923, znaménko 86,3–89,1 %, mezera 0–1,1 % minut.
  B nechává pod stropem rezervu ~0,07 v r a ~5 p. b. ve znaménku na drobné rozdíly modelu
  (univerzum, delta); obrácené znaménko nebo chybějící tisky neprojdou. Zvažováno: A (každá
  seance r ≥ 0,85, znaménko ≥ 85 %, mezera ≤ 1 %) by shodila ES 20. 10. (r 0,843) i se
  shodným modelem; C (medián r ≥ 0,70, znaménko ≥ 75 %, mezera ≤ 5 %) by připustila
  historii, která 1A popisuje jen zhruba.
- **Řady obou mechanik se nemíchají ani mimo replay.** `storage/cumdelta_compare.py` (živá
  řada jen midpoint, net hybridu bez řádků tisků) a `scripts/recompute_fa.py` (historie
  a stav α jen zdroje živé mechaniky) filtrují řádky podle `source`.
- **Přehrání v5 × v6** nad 21. 9.–15. 10. nese `scripts/compare_mechanics_v5_v6.py`
  (report v #1403).

## Doplněk (22. 10. 2026, #1602 engine část)

Vstup objemu pod mechanikou v6. Vlastník rozhodl o dvou věcech v epice #1385: #1602 jde
variantou B, tedy s bumpem v6 (20. 10.), a minuta po mezeře v objemu je B, i v enginu
(21. 10.). Ostatní body plynou z bodu 11.

- **Přírůstek denního kumulativního volume se počítá proti maximu seance**
  (`compute/volume.py`, `volume_increment`, `SessionVolumes`).
  - Proč: IBKR kumulativ kmitá (862 ↔ 863) a propadá (pauza Globexu, noc). Přírůstek proti
    poslední hodnotě proto po každém poklesu započetl návrat znovu: Opt Vol v RTH +12–70 %,
    Δ Flow C/P +5–22 % (#1602).
  - Reference je maximum viděné v seanci. Pokles i návrat k maximu dají 0, NULL referenci
    nemění.
  - První výskyt kontraktu v seanci a minuta po mezeře jen založí stav. Přírůstek se
    neměří (v součtu 0) a reference = max(reference, hodnota).
  - Mezera = kontrakt v minulém cyklu objem nenesl (NULL z tasty fallbacku, chybějící nebo
    stará kotace). Jinak by celý objem výpadku spadl do jedné minuty: ES 2. 10. 19:18 UTC
    C 44 243 / P 49 343.
  - Nová seance (17:00 CT) stav maže, restart začíná prázdný.
  - Totéž počítají API a frontend od #1630 a #1636. Daily ale minutu po mezeře započte.
- **Kde pravidlo platí (jen pod v6):**
  - bar větev trackeru pod tisky (`CumDeltaTracker.add_volume`: `printvol`, pokrytí):
    - pokles ani návrat k maximu nevyprázdní počítadla tisků;
    - pokles varuje jednou za kontrakt a seanci;
    - bar po cyklu bez baru (doplněk 18. 10., „jen založí stav“) drží maximum;
    - předání v rollu (`promote`) drží maximum a první bar aktivního řetězu jde jako po mezeře;
  - `SetupEngine._flows`: Δ Flow C/P pro T4 a `opt_vol` pro aktivitu T3. Cyklus je volání
    v minutě s bary;
  - `TendencyEngine._flows`, hlas `delta_flow` pravidel tendence v5. Minuta, ve které žádný
    kontrakt nemá měřený přírůstek, hlas vynechá jako dosud za fallbacku;
  - replay (`scripts/backtest_setups.py`, `option_flows`) jako vektorové zrcadlo:
    - cyklus = rozhodovací minuta (minuta s barem v okně aktivního řetězu #1464), stejně jako
      volání živého `_flows`. Přírůstek zapsané minuty bez baru připadne dalšímu cyklu;
    - cyklus bez jediného objemu (plný tasty fallback) je tok 0, ne chybějící řádek;
    - stav začíná prázdný na začátku okna, protože živý `SetupEngine` kontrakty expirace před
      rollem nečte;
    - parita s živým `_flows` má test (roll seance, víkend, mezera, fallback, minuta bez baru).
- **Pod v5 beze změny (bod 11):** tracker pod midpointem, setupy v5, tendence v4 i replay v5
  počítají přírůstek dál proti poslední hodnotě. `main` jde proto nasadit kdykoli a vzorky v5
  se nemění. Tabulka bodu 5 („T3, … Δ Flow C/P … beze změny“) mluví o zdroji znaménka. Vstup
  objemu T3, T4 a hlasu `delta_flow` se pod v6 mění tímto doplňkem.
- **Verze:** tendence přechází s bumpem na pravidla v5 a hlas `delta_flow` se mění jen pod
  nimi, vlastní zvýšení verze proto nepotřebuje. Mechanika setupů v6 nese změnu T3 a T4
  v téže dávce. Přehrání v5 × v6 (#1403) spuštěné po tomto doplňku už nový vstup objemu
  obsahuje.
- **Limit replaye:** restart enginu uprostřed okna replay nezná. Živý engine po restartu jen
  založí stav, replay přírůstek přes restart započte.
- **Ověření po nasazení (AC #1602):**
  - Σ `volume_delta` v `printvol` za RTH = Σ přírůstků od průběžného maxima ze snapshotů;
  - okno CumΔ v tendenci má v minutách pauzy řád jednotek tisíc;
  - partice `flow` nemá v 16:34–16:45 CT skok.

## Doplněk (23. 10. 2026, #1600, #1623, #1622)

Spot minutového cyklu pod mechanikou v6. Vlastník v epice #1385 rozhodl 23. 10. o rozsahu
(A) a o tom, že vše jde pod přepínač v6 (a), stejně jako #1602 (bod 11).

- **Příčina:** minutový cyklus bral tasty override, jinak `ticker.last`, jinak `marketPrice()`
  a jinak poslední uloženou hodnotu. Vždy dostal číslo, nikdy čas. Během fallbacku na tasty
  (#614) vrací `SpotFallback.resolve` při nečerstvé tasty `price=None`. `fallback_price` si tak
  drží poslední cenu a override ji vrací dál, i bez stáří. Po návratu IBKR přitom override
  drží tasty ještě 60 s.
  - ES 7. 10. 18:51–18:55 UTC stál spot na 7854,62 (mid), bar 18:55 měl rozsah 7855,5–7857,0.
    Z hlasů tendence 20 ze 155 minut RTH sedělo na barech ts−1 až ts−5 (#1623).
  - Verdikt dne odmítal jen NaN a cenu ≤ 0, zamrzlá cena tak prošla jako úplný výpočet (#1600).
- **Spot se stářím** (`compute/spot.resolve_spot`, čistá funkce). Pořadí zdrojů:
  1. IBKR ticker, živý feed (aktualizace ≤ 30 s, práh přepnutí #614);
  2. close posledního 5s baru IBKR (konec baru ≤ 60 s) — ticker mlčí, bary jdou;
  3. čerstvá kotace tasty (≤ 30 s);
  4. jinak spot **chybí**.
  `SpotReading.price` nese i tak nejčerstvější známou cenu. GEX výpočet, obálka striků a hlídač
  barů s ní běží dál jako dosud, hlasovat z ní ale nesmí nikdo.
- **Spotřebitelé (jen pod v6):**
  - verdikt dne: chybějící spot = hlas overnight „bez dat“ a neúplný řádek s důvodem a stářím
    (`missing`, např. „spot: poslední cena 7855 (bar) stará 120 s“). Neplatná cena v5 (NaN,
    ≤ 0) se jako dosud nezapíše. Automatický scénář čeká na úplný verdikt, takže nad chybějícím
    spotem nevznikne;
  - tendence (pravidla v5): bez spotu se přeskočí hlasy z polohy ceny (flip, poloha mezi zdmi,
    Max Pain, těžiště, rozchod) a hlasy gamma, charm a vanna v místě ceny. Ostatní hlasují.
    Minuta bez živé ceny chybí v cenové historii rozchodu;
  - tendence při zavřeném trhu nepočítá ani nezapisuje (#1622). V sobotu se dřív pásmo měnilo
    bez obchodu (charm rampa podle hodin) a řádky míchaly tabulku kalibrace vah;
  - setupy a paper spot nečtou už od #1346 a #1320 (jen bary), beze změny.
- **Log:** přechod živá → chybějící cena při otevřeném trhu varuje jednou („žádný zdroj nemá
  živou cenu“), návrat je na úrovni INFO. Při zavřeném trhu se nevaruje (#1307).
- **Pod v5 beze změny (bod 11):** `instruments._current_spot` i zamrzlý `fallback_price`
  zůstávají, vzorky v5 i tendence v4 se nemění a `main` jde nasadit kdykoli.
- **Verze:** pravidla verdiktu 4, tendence v5 a mechanika v6 přicházejí s bumpem, vlastní
  zvýšení verze to nepotřebuje.
