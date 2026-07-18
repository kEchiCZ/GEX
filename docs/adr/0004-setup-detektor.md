# ADR-0004: Setup detektor — obchodní analýzy s automatickým vyhodnocováním

**Stav:** navrženo (2026-07-18, čeká na schválení šablon uživatelem)
**Kontext:** Uživatel chce, aby aplikace analyzovala vývoj ceny v kontextu GEX
positioningu (jako to dělá ručně workflow A. Koopera) a dávala **indicie**:
na jakých úrovních a za jakých podmínek long/short, s cílem a stop lossem.
Analýzy se ukládají a automaticky vyhodnocují proti realitě, aby se jejich
váha časem zpřesňovala. SPEC v2.0 tuto funkci nepokrývá.

## Zásadní rozhodnutí

1. **Decision support, nikdy auto-trading.** Aplikace generuje setupy s textovým
   zdůvodněním; obchod je vždy rozhodnutí uživatele. Read-Only API zůstává.
2. **Fáze 1 = transparentní pravidla, žádné ML.** Každý setup vzniká z explicitní
   šablony s čitelnými podmínkami. Statistické „učení" (Fáze 2) = kalibrace
   confidence podle historické úspěšnosti šablony; ML vrstva až po měsících dat
   (samostatné ADR).
3. **Výpočty setupů jsou čisté funkce s golden testy** (CLAUDE.md pravidlo 3).
4. **Tabulka `setups` je bez delete API** (jako `oi_eod`) — historie analýz je
   trvalý dataset pro kalibraci.

## Šablony setupů (Fáze 1)

Všechny prahy jsou konfigurovatelné (env `GEXLENS_SETUP_*`); uvedené hodnoty
jsou defaulty. Vzdálenosti v bodech podkladu. Společné podmínky: OI dostupné
(ne volume fallback), spot známý, úrovně z posledního minutového cyklu.

### T1 — Odraz od zdi (wall bounce)
- **Kontext:** cena v zóně zdi (±3 b od call/put wall) a na „správné" straně
  flipu (put wall bounce: cena ≥ flip = kladná gamma tlumí; jinak nižší confidence).
- **Trigger:** Cum Δ divergence — cena k zdi klesá/roste, ale Cum Δ za posledních
  10 min jde proti (agresoři drží protistranu), a cena zavře minutu zpět od zdi
  (odmítnutí ≥ 1 b).
- **Směr:** od zdi (put wall → long, call wall → short).
- **Cíl:** nejbližší protilehlá úroveň (Max Pain / flip / protější zeď).
- **Stop:** za zdí, buffer = max(3 b, 25 % vzdálenosti k cíli).
- **Zahodit, když** RRR < 1,2.

### T2 — Neúspěšný průraz (failed breakdown/breakout)
- **Kontext:** cena prorazila zeď nebo flip o ≥ 3 b.
- **Trigger:** žádná akceptace — do 15 minut se cena vrátí zpět za úroveň
  (akceptace = 5 po sobě jdoucích minutových closes za úrovní → šablona umírá)
  a reclaim potvrdí minutový close ≥ 1 b zpět.
- **Směr:** proti směru průrazu (spring/upthrust logika).
- **Cíl:** protilehlá úroveň; **Stop:** za extrémem průrazu + 1 b.
- Přesně scénář z 17. 7.: průraz 7500 → flush 7473 bez akceptace → reclaim → 7529.

### T3 — Max Pain pin (jen expirace dne)
- **Kontext:** do expirace < 3 h, |cena − Max Pain| ≥ 8 b, Max Pain stabilní
  (změna < 5 b za poslední hodinu).
- **Trigger:** Opt Vol klesá (poslední 30min průměr < denní průměr) — trh „dohrává".
- **Směr:** k Max Pain. **Cíl:** Max Pain. **Stop:** 1,5× vzdálenost k cíli
  od entry (pin je slabší edge, potřebuje volnější stop → RRR ~0,67, kompenzováno
  historicky vyšší úspěšností; pokud statistika Fáze 2 neukáže > 65 %, šablona se vypne).

### T4 — Gamma momentum (breakout v záporné gammě)
- **Kontext:** cena prorazí flip směrem do záporné gammy (dolů pod flip / nahoru
  nad něj při inverzním profilu) o ≥ 2 b.
- **Trigger:** Δ Flow souhlasí (strana průrazu ≥ 60 % delta-váženého toku za
  posledních 10 min) a Cum Δ dělá nové extrémum ve směru.
- **Směr:** po směru průrazu (dealeři zesilují). **Cíl:** další zeď ve směru.
- **Stop:** zpět za flip + 1 b.

## Životní cyklus setupu

```
kandidát (podmínky kontextu) → AKTIVNÍ (trigger splněn, zapsán + alert)
  → CLOSED_TARGET   (dotčen cíl dřív než stop)
  → CLOSED_STOP     (dotčen stop dřív než cíl)
  → CLOSED_TIMEOUT  (konec seance / expirace řetězu bez rozhodnutí)
```

- Vyhodnocuje engine automaticky z minutových high/low barů podkladu.
- Zaznamenává se **MFE/MAE** (max příznivý/nepříznivý pohyb v bodech) a
  **R výsledek** (zisk/ztráta v násobcích risku).
- Anti-spam: max 1 aktivní setup per (šablona × úroveň); nový vznikne až po
  uzavření předchozího; globální cooldown 10 min per šablona.

## Confidence (Fáze 1 → 2)

- Fáze 1: statická startovní confidence per šablona (T1 55 %, T2 55 %, T3 60 %,
  T4 50 %) + plný kontext do DB (gamma režim, vzdálenosti, čas do expirace,
  Cum Δ stav, den v týdnu, typ expirace).
- Fáze 2: confidence = Bayesovská aktualizace startovní hodnoty výsledky téže
  šablony (Laplace smoothing — malé vzorky nepřestřelují), volitelně podmíněná
  kontextem (gamma režim, typ expirace). Obrazovka Statistiky: win-rate,
  expectancy (R), MFE/MAE distribuce per šablona.

## Schéma a rozhraní

- **PG `setups`**: id, symbol, expiry, template, direction, created_ts, entry,
  target, stop, confidence, context (JSON), status, closed_ts, outcome_r,
  mfe, mae. Bez delete API.
- **Engine**: `compute/setups.py` (čisté funkce: kontext → kandidáti → trigger)
  volané po `run_cycle` aktivní expirace; golden testy na scénářích ze 17. 7.
- **API**: `GET /setups?symbol&date&status`, WS kanál `setups.{symbol}`.
- **UI**: značky entry/target/stop v heatmapě (zóny + popisky), karta aktivního
  setupu se zdůvodněním česky, obrazovka **Setupy** v sidebaru (historie +
  výsledky), alert `setup` do zvonku.

## Poctivá očekávání (zapsat i do manuálu)

Setupy jsou kontextové pravděpodobnosti, ne předpovědi — i dobrá šablona má
55–65 % úspěšnost a smysl dává jen se stop lossem. Statistická významnost
kalibrace přichází po týdnech dat (jednotky setupů denně). Aplikace nikdy
neobchoduje sama.
