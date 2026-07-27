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

## Ruční hodnocení uživatelem (Fáze 1)

Vedle automatického vyhodnocení (target/stop) může uživatel každý uzavřený
setup ručně ohodnotit: **👍 / 👎 + volitelná poznámka** („vyšlo přesně podle
predikce", „trefa, ale entry moc brzo"…). Hodnocení se ukládá k setupu
(`user_rating`, `user_note`) a zobrazuje v historii vedle automatického
výsledku. **Nevstupuje do automatické kalibrace confidence** — je to
kvalitativní vrstva pro revizi šablon: při ladění pravidel uvidíme vedle
čísel i subjektivní pohled tradera (např. setup skončil na stop, ale
uživatel ho hodnotí kladně, protože logika byla správná a stop jen těsný).

## Schéma a rozhraní

- **PG `setups`**: id, symbol, expiry, template, direction, created_ts, entry,
  target, stop, confidence, context (JSON), status, closed_ts, outcome_r,
  mfe, mae, **user_rating** (null/+1/−1), **user_note** (text). Bez delete API.
- **API navíc**: `PATCH /setups/{id}/review` — zápis hodnocení a poznámky
  (jediná mutace, kterou setup připouští; predikce samotná je neměnná).
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

## Dodatek 2026-07-24: šablona T5 — divergenční spring (#250)

Z živého případu 24. 7. 8:49 (nové low 7433.75 ve vzduchoprázdnu + CumΔ na
maximech okna → spring +25 b), který T1 nepokryl (cena mimo zónu zdi) a T2
chytil pozdě (čeká na reclaim).

- **Kontext:** minuta udělá nové extrémum okna `spring_lookback` (90 min)
  a close NENÍ v zóně zdi (±wall_zone — tam patří T1).
- **Trigger LONG:** nové low okna + CumΔ ≥ maximum okna (nákupy do slabosti)
  + close ≥ low + `spring_rejection` (1 b). SHORT zrcadlově.
- **Cíl:** nejbližší úroveň nad/pod entry (Max Pain/flip/protilehlá zeď).
  **Stop:** extrém ∓ `spring_stop_buffer` (2 b). Filtr RRR ≥ 1,2.
- Startovní confidence **50 %** (bez historie); kontext (extrém, CumΔ,
  zeď, gamma režim) do DB pro kalibraci Fáze 2.

## Dodatek 2026-07-24: kontra-režimový filtr B+C (#252)

Z 24. 7.: NQ v negativní gammě trendově klesal a detektor do poklesu opakovaně
fadoval LONG — 11 kontra setupů T1/T2, 9 stopů, „žebřík ztrát" (4 stopy za
hodinu), který 10min cooldown nezastavil. Varianta A (tvrdý zákaz kontra
obchodů) zamítnuta — připravila by o vítěze v range dnech; D (čekat na
kalibraci Fáze 2) běží souběžně dál.

- **B — konfluence toku:** kontra-režimový T1/T2 (long v negativní gammě /
  short v pozitivní; `is_counter_regime`) vyžaduje navíc CumΔ divergenci přes
  `counter_flow_lookback` (30 min) — fade proti gammě jen s důkazem, že se tok
  otáčí i na delším horizontu. Krátká historie po startu = konfluenci nelze
  ověřit → kontra setup nevzniká (konzervativně). Neznámý režim (None) není
  kontra — stejná konvence jako u neznámé dominance (ADR-0010).
- **C — cooldown po stopu:** stop kontra setupu spustí pro šablonu delší
  cooldown `counter_stop_cooldown_minutes` (45 min) na další kontra pokus;
  first-try neblokuje, obchody po směru režimu nechává být. Stav je v paměti
  enginu — po restartu se žebřík hlídá od prvního nového setupu.
- Kontext setupu nese `counter_regime`; potvrzená konfluence je v `reason`
  („Kontra-režim potvrzen tokem"). Prahy: `GEXLENS_SETUP_COUNTER_FLOW_LOOKBACK`,
  `GEXLENS_SETUP_COUNTER_STOP_COOLDOWN_MINUTES`.

## Dodatek 2026-07-27: T5 divergence_spring vypnuta (#303)

Za 20.–27. 7. je detektor celkově −43,5R (166 uzavřených, 15 % úspěšnost).
T5 je z toho nejhorší šablona: **23 setupů, 2 výhry (8,7 %), Ø −0,69R**.

Šablona vznikla z **jediného pozorovaného živého případu** (24. 7. 8:49, ES
spring +25 b) a byla rovnou nasazena do živého detektoru. To bylo předčasné —
jeden případ je hypotéza, ne pravidlo. Premisa „nové extrémum okna × extrém
CumΔ" edge nepotvrdila.

- Šablony lze vypnout přes `disabled_templates` (`SetupParams`) resp.
  `GEXLENS_SETUP_DISABLED_TEMPLATES`; filtr je v `detect_all`, čisté detektory
  zůstávají volatelné (testy a budoucí přeměření).
- **Default: `divergence_spring` vypnuta.** Kód zůstává — po opravě
  R-mechaniky (#302) se dá šablona přeměřit nad stejnými daty.
- Pravidlo pro příště: nová šablona se nejdřív měří proti historii, do živého
  detektoru jde až s výsledkem, ne s jedním pozorováním.

## Dodatek 2026-07-27: jednotná R-mechanika (#302)

Za 20.–27. 7. byl detektor **−43,5R** (166 uzavřených, 15 % úspěšnost).
Příčina nebyla v premisách šablon, ale v mechanice entry/stop/target.

**Měření (setupy 20.–27. 7. + ATR z 1min barů):**

| | ES | NQ |
|---|---|---|
| ATR(14) 1min, medián | 1,57 b | 11,52 b |
| Ø risk T5 / T2 / T1 | 4,1 / 9,6 / 13,6 b | 9,9 / 22,4 / 38,9 b |
| Ø risk v násobcích ATR (T5) | 2,6 × | **0,86 ×** |
| Ø RRR T5 / T2 / T1 / T3 | 17,5 / 6,9 / 2,8 / 0,7 | 41,9 / 16,5 / 2,9 / 0,7 |

Tři vady:

1. **Absolutní buffery na dvou různě volatilních instrumentech.** NQ se hýbe
   7,3× víc než ES, ale buffery byly sdílené v bodech — na NQ tak T5 riskovala
   0,86 ATR (uvnitř minutového šumu; setupy padaly do minuty).
2. **Nedosažitelné cíle.** Cíl = nejbližší z (max_pain, flip, protilehlá zeď);
   když byly všechny daleko, vzniklo RRR 16–42 a vždy se dřív trefil stop.
   `min_rrr` je dolní mez, takže tomu nebránil.
3. **RRR se kontrolovalo jen v T1 a T5.** T2, T3 a T4 na kontrolu zapomněly —
   T3 proto pouštěla setupy s RRR 0,7 (riskovala víc, než byl cíl hoden).

**Řešení — `normalize_candidate` v `detect_all`, jediné místo pro celou
mechaniku** (šablony samy už o risku/RRR nerozhodují, aby na to nešlo zapomenout
při přidání nové):

- **Prahy v násobcích ATR(14), ne v bodech** — platí pro ES i NQ zároveň.
- **Minimální risk** `min_risk_atr` (2,0 × ATR): těsnější stop se rozšíří.
  Na ES floor většinou nezasáhne (setupy měly 2,6–8,7 ATR), na NQ opraví
  právě T5 a T2. Riskem se nesmí lichotit R metrice.
- **Strop vzdálenosti cíle** `max_rr` (3,0 × risk): dál = částečný cíl.
  Horní mez RRR je tím strukturální, ne další filtr.
- **`min_rrr` až po normalizaci** a pro všechny šablony.
- Bez měřitelného ATR (krátká historie po startu) setup nevzniká — stejná
  konzervativní konvence jako u kontra-režimu (#252 B).
- **T3:** stop `pin_stop_ratio` (0,75) × vzdálenost k Max Pain místo 1,5× →
  RRR 1,33 místo 0,67.
- **Strop pokusů per směr** `max_stops_per_direction` (3) /
  `direction_block_minutes` (90): per-šablonový anti-spam se dal obejít
  prokládáním šablon — 27. 7. vzniklo 20 shortů za sebou proti stoupajícímu NQ.
  Blokace je napříč šablonami; počítadlo maže až výhra v daném směru, takže
  po vyčerpané sérii projde jen jeden pokus za okno.
- Kontext setupu nese `atr` a `risk` pro kalibraci Fáze 2. Env:
  `GEXLENS_SETUP_MIN_RISK_ATR`, `GEXLENS_SETUP_MAX_RR`,
  `GEXLENS_SETUP_MAX_STOPS_PER_DIRECTION`, `GEXLENS_SETUP_DIRECTION_BLOCK_MINUTES`.

Defaulty jsou odvozené z měření výše, ne odhadnuté — ale zůstávají kandidátem
na kalibraci Fáze 2, až se nasbírají výsledky s opravenou mechanikou. Historické
setupy se nepřepočítávají; srovnávat lze až od nasazení.
