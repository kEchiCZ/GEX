# Analýza: Sosnoff „What volatility actually is" vs. GEXLens

Zadání: `docs/Volatility/Volatility.md`. Audit provedeno 26. 8. 2026 nad aktuálním
main (`a8da53b`). Nic z níže uvedeného není implementováno — jde o návrh; issues
nezaloženy (dle zadání).

**Celkový verdikt předem:** aplikace už má překvapivě velkou část volatilitní
vrstvy — expected move z ATM straddlu (#676), volatilitní režim z vlastní
2leté historie rozsahů (ADR-0028, #713) a věčný denní archiv IV per strike
(#519). Chybí hlavně **kontextualizace implied volatility (IVR)**, srovnání
IV×HV (premium rich/cheap) a **propsání volatility do rozhodovací rutiny**
(briefing, ranní plán). Zhruba třetina konceptů z videa je pro intradenní
ES/NQ futures irelevantní — video je o prodeji opčního prémia a portfoliích,
my opce neprodáváme (R1: vizualizace positioningu, obchoduje se futures).

---

## Úkol 1 — Tabulka konceptů A/B/C

| Koncept z videa | Kat. | Kde v aplikaci / poznámka |
|---|---|---|
| Expected move | **A** | `frontend/src/instrument/expectedmove.ts` (#676): EM = mid(C)+mid(P) ATM straddlu v referenční minutě, EM± linie v grafu, vyčerpání pásma v cenovce (`emUsage`), pre-open průběžný odhad zamknutý openem. Traders mode (#677). |
| Implied expected move „u každého obchodu" | **B** | EM je v grafu a cenovce, ale NENÍ u „obchodu": kalkulačka pozice (#679, Settings → Trading) o EM neví a kostra ranního plánu (#673) ho nenese. Video chce EM v místě rozhodnutí — viz doporučení D1. |
| IV Rank (IVR) | **C** | Nikde. Jediná kontextualizace volatility je percentil REALIZOVANÉHO rozsahu (ADR-0028) — to je HV strana, ne IV. Datové cesty viz úkol 2. |
| IV vs. HV | **B** | HV strana: `engine/compute/volregime.py` — percentil denního rozsahu v okně 252 seancí z ~2 let barů (věčný archiv), buckety low/normal/elevated/crisis, API `/volregime/{symbol}`. IV strana: IV per strike v snapshotech a denně ve věčném `oi_eod` (#519) — ale NIKDE se neagreguje na „IV instrumentu" ani nesrovnává s HV. Poměr/spread IV×HV chybí. |
| Mean reversion volatility | **C** | Žádný mean-reversion signál volatility. Kriticky: pro intradenní futures obchodování je to interpretační kontext, ne obchodní signál (nemáme vega pozice, které by z kontrakce IV profitovaly). Nedoporučuji implementovat jako signál; stačí věta v manuálu ke čtení IVR („vysoké IVR má tendenci klesat"). |
| Komparativní ocenění volatility | **B** | Přesně tohle dělá vol režim (ADR-0028: „stejný stop v bodech je v jiném režimu úplně jiný obchod") — ale jen pro realizovanou stranu. Komparativní ocenění IMPLIED strany = IVR (chybí, D2). |
| Premium rich / cheap | **C** | Chybí. Proxy po D2: percentil IV vs. percentil HV (rich = IV percentil ≫ HV percentil). Pro nás nemá exekuční využití (neprodáváme prémii), ale je to čitelný kontext pro gamma vrstvu: rich prémie ⇒ trh platí za hedge ⇒ typicky silnější dealer hedging flow. Nízká priorita (D5). |
| Nálada trhu (complacent / capitulating) | **B** | Složená z existujících dílů: CNN Fear & Greed (`news-engine/crowd.py`, Tier C #290), SentIndex stav RiskOn/RiskOff/Neutral (#563), vol bucket low/crisis (ADR-0028). Jediný „complacent/capitulating" štítek neexistuje — a nedoporučuji ho: slévání tří měřených veličin do jedné nálepky by zahodilo informaci (přesně proti R4). Stačí je v briefingu ukázat VEDLE sebe (D1). |
| Vztah IV → prémie → POP | **C / irelevantní** | POP je metrika prodejce opcí. My opce neobchodujeme; pravděpodobnostní ekvivalent u nás je track record setupů (Wilson LB, mechanika v5) a EM pásmo. Neimplementovat. |
| Trade-off zisk vs. pravděpodobnost | **irelevantní** | Přímý překlad (omezit zisk za vyšší POP = credit strategie) na futures nepřenositelný. Nepřímý ekvivalent už máme: R statistiky setupů + kalibrace prahů (#794/#575). Nic nového nezavádět. |
| Zlepšování nákladové báze (covered call / CSP) | **irelevantní** | Strategie držitele akciového podkladu. Intradenní ES/NQ žádnou nákladovou bázi ke zlepšování nemá. Neimplementovat. |
| Outlier / binární události | **B** | Earnings pro ES/NQ nedávají smysl; binární MAKRO eventy pokrývá SentimentLens: makro kalendář s importance v briefingu (#674), high-impact výhled na týden (#830), anomální reakce → zvonek (#295), měřené reakce oken (`news_reactions`). Chybí jediné propojení: EM vs. plánovaná binární událost („dnes CPI — EM je širší než obvykle?") — pokryje D2/D5 přirozeně. |
| Defined vs. undefined risk | **irelevantní** | Opční strukturace kapitálu. U futures řeší risk kalkulačka pozice (#679: účet + % rizika → kontrakty vč. micro). Neimplementovat. |
| Volatility box | **C** | Nejpřenositelnější myšlenka videa. Deník už vol režim ZAZNAMENÁVÁ (`frontend/src/journal/context.ts` — `volRegime` v kontextu záznamu), ale kostra ranního plánu (`briefingToPlanText`) ani briefing ho NEUKAZUJÍ a nikdo ho nemusí potvrdit. Viz D1. |
| Listed vs. non-listed | **irelevantní** | Edukační rámec videa. Bez implementačního obsahu. |
| Bull market bias | **B (vyřešeno jinak)** | Přesně tenhle jev jsme empiricky změřili a ošetřili: #564/#453 — denní okna procházela gate jen driftem býčího trhu (naivní „vždy long" 0,662 vs. 0,511), proto drift-adjusted práh a signály jen na minutových oknech. Nic dalšího netřeba. |
| Špatně přiřazená volatilita → alokace | **B** | Rozpoznání režimu máme (ADR-0028), ale alokace na něj NEreaguje: kalkulačka pozice (#679) počítá kontrakty z fixního % rizika bez ohledu na vol bucket, a stopy setupů jsou v bodech (známý problém #434 pro NQ). Viz D4. |

---

## Úkol 2 — Datová proveditelnost přes IBKR

Klíčové: **žádná z chybějících položek nekoliduje s retencí 14 dní** — všechno
potřebné stojí na řadách, které už dnes žijí mimo retenční politiku:

| Zdroj | Historie dnes | Retence |
|---|---|---|
| Bary (věčný archiv, `data/derived/*/bars`) | ~2 roky ES i NQ | věčná |
| `oi_eod` + denní snímek řetězce: IV, greeks, close prémie, undPrice per strike (#519) | od 13. 8. 2026, roste navždy | věčná (výjimka R3) |
| `vol_regime` (PG, ADR-0028) | backfill z barů ⇒ ~2 roky od prvního dne | věčná |
| Snapshoty (minutová IV per strike) | 14 dní | 14 dní — pro volatilitní vrstvu NEpotřebné |

**IVR — tři cesty, doporučené pořadí:**

1. **tastytrade market metrics (issue #618 už existuje, P3).** Endpoint
   `/market-metrics` vrací hotové `implied-volatility-index-rank` a
   `implied-volatility-percentile` — nula vlastní historie, nula requestů na
   IBKR, čte se v rámci existující tasty větve (OAuth už běží). Nejrychlejší
   cesta k IVR „dnes". Nevýhoda: černá skříňka (jejich definice ranku, jejich
   podklad — pravděpodobně /ES index metrika), závislost na druhém zdroji.
2. **Vlastní řada z `oi_eod`.** ATM IV per den = IV striku nejblíž `und_price`
   (obě hodnoty v archivu). Plnohodnotný IVR (percentil v 252denním okně,
   stejná mechanika jako `volregime.percentile_of`) bude až za ~rok, ale
   **měřitelný percentil v rostoucím okně je k dispozici hned** (vzor
   MIN_SAMPLE z ADR-0028 — pod 60 dnů se rank neurčuje). Trvale nezávislé na
   třetí straně; kotva pro křížovou kontrolu cesty 1.
3. **IBKR historická IV** (`reqHistoricalData`, `whatToShow=
   OPTION_IMPLIED_VOLATILITY`). Pro akcie/indexy IBKR dodává denní 30d IV
   roky zpět jedním requestem — pokrylo by díru cesty 2 okamžitě. Pro ES/NQ
   futures opce je podpora NEOVĚŘENÁ (dokumentace ji slibuje pro stock/index).
   Ověřit sondou mimo seanci (vzor #609/#612); jeden request, žádná linka
   navíc. Pokud funguje, je to nejlepší backfill; pokud ne, zbývá 1+2.

**HV:** hotovo — bary + `volregime` (rozsahová varianta HV vhodnější pro
intraday než close-to-close σ; nic nechybí).

**EM statistika (respektování pásma):** bary (věčné) + EM. Historický EM jde
rekonstruovat z `oi_eod.close_prem` ATM striku (close prémie C+P) — od 13. 8.,
roste navždy. Za 14denní EM historii navíc snapshoty. Žádný nový sběr.

**Premium rich/cheap:** odvozenina IVR (cesta 1 nebo 2) × HV percentilu
(`vol_regime`) — po D2 zadarmo.

---

## Úkol 3 — Synergie s gamma vrstvou

1. **EM pásmo × walls × flip (konfluence).** EM± linie už v grafu jsou; chybí
   VZTAH k úrovním. Dvě levné, měřitelné vazby:
   - *Konfluence:* EM hranice do 0,5 kroku striku od call/put wall ⇒ hranice
     se navzájem potvrzují (dealer hedging + statistické pásmo míří na totéž
     místo). Chip u cenovky EM („EM↑ ∩ call wall").
   - *Režimová šířka:* je-li vzdálenost spot→flip menší než EM, statistika
     dne říká, že překlopení režimu je „v ceně" — dnešek může být obourežimový.
     Opačně (flip daleko za EM) je režim dne prakticky zamčený.
2. **Gamma režim × volatilitní kontext.** Otázku „chová se pozitivní/negativní
   gamma jinak při vysokém a nízkém IVR" umíme zodpovědět Z VLASTNÍCH DAT,
   bez implementace UI: track record setupů nese gamma režim, `vol_regime`
   nese bucket per seanci, deník má obojí v kontextu záznamu. Návrh: rozšířit
   noční setup statistiky o řez (gamma režim × vol bucket) — stejná mechanika
   jako `aggregate_by_regime` v news modelu (#402). Až bude IVR (D2), přidat
   IVR bucket jako třetí dimenzi. Nic se nezapíná — jen se měří (R4).
3. **Statistika respektování EM.** Kolektor po settle (přesný vzor
   `volregime.py`/`gammacliff.py`): per seance zapsat EM referenční minuty,
   skutečný rozsah, close-in-band (ano/ne), touch-beyond (ano/ne), poměr
   range/EM. Výstup: „za posledních 90 dnů close uvnitř EM v X % (teorie
   ~68 %), průraz nejčastěji v negativní gammě" — druhé tvrzení je přesně
   průsečík s gamma vrstvou a testovatelná hypotéza z manuálu kap. 18.
4. **Briefing sekce Volatilita.** `BriefingView` dnes: režim gammy, úrovně,
   včerejšek+overnight, odpad gammy, makro, ΔOI, sentiment, výhled na týden.
   Přidat kartu: vol bucket + percentil (z `/volregime`, API existuje),
   pre-open EM v bodech a % (z #676), EM respect statistika (bod 3), později
   IVR (D2). Čistá kompozice existujících endpointů — v duchu #674 („žádný
   nový výpočet v enginu").
5. **Volatility box v ranním plánu.** `briefingToPlanText` dostane blok
   „Volatilita: [bucket, percentil, EM ±X b]" + řádek k doplnění
   („[ ] riziko přizpůsobeno režimu — stop/velikost"). Deník už vol režim
   ukládá do kontextu záznamu, takže vyhodnotitelnost zpětně (kolik plánů
   box ignorovalo) je zadarmo.

---

## Doporučení k implementaci (seřazeno přínos/náročnost)

### D1 — Volatility box: briefing sekce Volatilita + blok v kostře ranního plánu
*Přínos vysoký, náročnost nízká (frontend kompozice existujících dat).*

> **Návrh issue:** `feat(frontend): sekce Volatilita v briefingu + volatility box v ranním plánu (#674, #673 follow-up)`
> Briefing dostane kartu Volatilita: vol bucket + percentil (`/volregime`),
> pre-open EM v bodech a % spotu (#676), vedle sebe se sentimentem (F&G,
> RiskOn/Off) — bez slévání do jedné nálepky. `briefingToPlanText` přidá blok
> „Volatilita: …" s řádkem potvrzení rizika. AC: (1) karta ukazuje bucket,
> percentil a EM před openem i po zamknutí; (2) plán založený z briefingu
> nese volatilitní blok; (3) bez dat (málo vzorků, chybí straddle) karta
> říká proč, nedosazuje „normal" (zásada ADR-0028); (4) vitest na skládání
> textu plánu.

### D2 — IVR: tasty market metrics hned + vlastní věčná ATM IV řada
*Přínos vysoký (jediná chybějící „first-class" metrika videa), náročnost střední.*

> **Návrh issue:** `feat(engine): IV Rank — tasty market metrics + vlastní ATM IV řada z oi_eod (#618 povýšit)`
> Fáze 1: číst `implied-volatility-index-rank`/`percentile` z tasty
> market-metrics v existující tasty větvi, ukládat denně (věčně), zobrazit
> v briefingu (D1) a hlavičce vedle badge režimu. Fáze 2: vlastní řada ATM IV
> z `oi_eod` (strike nejblíž `und_price`), percentil v rostoucím okně s
> MIN_SAMPLE 60 dle vzoru ADR-0028; křížová kontrola proti tasty hodnotě.
> Fáze 0 (sonda): ověřit `whatToShow=OPTION_IMPLIED_VOLATILITY` na ES u IBKR
> mimo seanci — funguje-li, backfill vlastní řady roky zpět. AC: denní IVR
> v DB mimo retenci, zdroj hodnoty vždy uveden (tasty/vlastní), rozdíl obou
> řad logován; žádná nová IBKR linka.

### D3 — Statistika respektování EM (kolektor po settle)
*Přínos střední-vysoký (validuje EM pásmo, na kterém stojí D1), náročnost nízká-střední.*

> **Návrh issue:** `feat(engine): EM respect kolektor — close/touch vůči pásmu EM per seance (#676 follow-up)`
> Po settle zapsat: EM referenční minuty (rekonstrukce i z `oi_eod.close_prem`
> pro dny bez snapshotů), skutečný high/low/close, close-in-band, touch-beyond,
> range/EM. API + řádek do briefing karty Volatilita („close uvnitř EM: X %
> za 90 d"). AC: golden test na klasifikaci seance, backfill přes dostupnou
> historii close prémií, výsledky per symbol; přesný vzor `volregime.py`
> (žádný default při málu vzorků).

### D4 — Risk sizing podle volatilitního režimu (kalkulačka pozice)
*Přínos střední, náročnost nízká; navazuje na #434.*

> **Návrh issue:** `feat(frontend): kalkulačka pozice zohlední vol režim (#679 + #434 follow-up)`
> Kalkulačka ukáže vedle výsledku vol bucket dne a přepočet „stop v bodech ↔
> % denního rozsahu (percentil)"; při bucketu elevated/crisis zvýrazní, že
> fixní bodový stop je v tomto režimu těsnější obchod. NIC nemění automaticky
> — jen ukazuje (R4; plná adaptace stopů patří do #434/#575, ne sem).

### D5 — Premium rich/cheap (IV percentil × HV percentil)
*Přínos nižší (kontext, ne exekuce), náročnost nízká — až PO D2.*

> **Návrh issue:** `feat: indikátor rich/cheap prémie — spread percentilů IV × HV`
> Po D2: rozdíl IVR (implied) − vol percentil (realized); chip v briefing
> kartě Volatilita („prémie rich: IV p85 vs. HV p40 — trh platí za hedge").
> AC: definice spreadu zdokumentovaná v manuálu vč. limitů; žádný signál,
> jen kontext.

### Pořadí a závislosti
D1 (hned, bez závislostí) → D3 (nezávislé, posiluje D1) → D2 (fáze 0 sonda
kdykoli mimo seanci) → D4 (nezávislé) → D5 (po D2). Gamma×vol řez (úkol 3
bod 2) je měřicí dotaz, ne feature — může běžet kdykoli jako rozšíření
nočních statistik.

---

## Co výslovně NEdoporučuji implementovat

- **Covered calls / zlepšování nákladové báze, POP, defined/undefined risk,
  trade-off zisk×pravděpodobnost** — strategie prodejce opčního prémia na
  akciovém podkladu; na intradenní ES/NQ futures nepřenositelné (a R1 říká,
  že aplikace vizualizuje positioning, neobchoduje opce).
- **Earnings obchody** — indexové futures earnings nemají; binární makro
  eventy už pokrývá SentimentLens.
- **Mean-reversion volatility jako signál** — bez vega expozice není co
  obchodovat; patří do manuálu jako věta o čtení IVR, ne do enginu.
- **Jednotný štítek complacent/capitulating** — slévání tří měřených veličin
  (F&G, SentIndex stav, vol bucket) do jedné nálepky zahazuje informaci;
  ukázat vedle sebe (D1).
- **Nemovitosti / non-listed aktiva** — mimo doménu aplikace.
