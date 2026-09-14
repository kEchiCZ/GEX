# ADR-0037: Korekční epizody SentIndexu — pokus vs. negace, práh v σ jako verzovaný placeholder

- **Stav:** přijato (uživatel 14. 9. 2026, #565 varianta V2)
- **Datum:** 2026-09-14
- **Souvisí:** #565, #640 (z-score vrstva, kalibrace jen nad živou érou), #563 (stav), ADR-0019 (vlny), ADR-0034 (walk-forward), ADR-0036 (váhy), report `data/reports/sentiment-episodes-2026-09-14.md`, sentiment-SPEC-v1.md §5.6

## Kontext

Referenční přístup (#561 §4) rozlišuje **pokus o korekci** nálady, který se do
1–2 týdnů zahladí, od **negace**, kdy korekce pokračuje. Vlny z ADR-0019 na to
nestačí: stav podle MA5/MA10 překlápí skoro denně (ES Ø 1,5 dne, NQ 1,4), takže
vlna je jednotka šumu, ne korekce. Řada má navíc tři éry měřítka (#640), proto
musí být práh relativní — v jednotkách σ(100 seancí) a měřený jako pokles od
klouzavého maxima.

Fáze 1 (#565, `scripts/measure_sentiment_episodes.py`, 14. 9. 2026) změřila nad
živou érou od 28. 7. 2026 (35 obchodních dní):

1. σ(100) je do ~11/2026 kontaminovaná backfillem (ES σ 0,19 → 2,61 v éře, jen
   z živých řádků 3,23), hloubky startů 3–32 σ → **každé D z gridu {0,5…3}
   dává tytéž epizody** — práh nejde kalibrovat.
2. Zahlazení „nad úroveň startu nebo nad MA10" (doslovné znění zadání) je
   **degenerované: 0 negací ve všech buňkách** obou symbolů i v backfillu
   (jediný odlehlý den stáhne MA10; odraz nad start je triviální).
3. Zahlazení `peak` (zpět nad 20denní maximum) obě třídy dává, ale vzorek je
   2–3 rozhodnuté epizody per buňku; vztah k ceně nelze tvrdit (backfill
   kontrola ES BA 0,60 vs. NQ 0,44 — opačně).

Uživatel zvolil V2: mechaniku nasadit hned s **verzovaným placeholderem**, aby se
klasifikace ukládala od teď (vstup pro #453), a přeměřit, až bude σ čistá.

## Rozhodnutí

### 1. Definice epizody (pinnutá, `gexlens_engine.compute.sentwaves`)

Vstup = uzavřené dny `sentiment_daily` (date, close, close_z), řádek každý
kalendářní den, close_z = close / σ(100 předchozích řádků) (#640).

- **Referenční úroveň** = klouzavé maximum close_z posledních
  `EPISODE_MAX_WINDOW = 20` řádků včetně aktuálního; None, dokud v okně chybí
  byť jediná hodnota (částečné okno by dávalo falešně nízké maximum).
- **Start** = den, kdy `maximum − close_z ≥ D` (`EPISODE_THRESHOLD_D`). Další
  start až po **odjištění** (drawdown < D) — jinak by trvající negace zakládala
  epizodu každý den — a epizody se **nepřekrývají** (nový start až po dni
  rozhodnutí předchozí; den rozhodnutí se pro odjištění projde znovu, protože
  zahlazení = nové maximum = drawdown 0).
- **Pokus** = close_z ZPĚT NAD referenční úrovní, hodnoceno od dne po startu.
  Varianta „nad úroveň startu / nad MA10" se **nepoužívá** (bod 2 kontextu).
- **Negace** = bez zahlazení do `EPISODE_HORIZON_H` obchodních dní; rozhodnutí
  = H-tý obchodní den po startu. Řada s méně dny = epizoda probíhá
  (`end` NULL, třída NULL).
- **Hloubka** `depth_z` = největší pokles pod referenční úroveň během epizody;
  **délka** = obchodní dny od startu do rozhodnutí.
- **Obchodní den** = pondělí–pátek (`is_weekday`). Svátky se NEgatují (zásada
  „otevřený trh = vidět vše"); proti skutečným seancím podkladu, které používá
  měřicí skript, se liší nejvýš o den kolem svátku. Funkce bere
  `is_trading_day` jako parametr, takže měření může dosadit reálný kalendář.
  Parita skript ↔ engine je golden test (`engine/tests/test_sentepisodes.py`).

### 2. Parametry = placeholder, verzované

`EPISODE_PARAMS_VERSION = 1`: D = 1 σ, H = 10 obchodních dní, okno 20.
**Není to kalibrace** — D nešlo kalibrovat (bod 1 kontextu), 1 σ je baseline
zadání a dnes nic nekazí (každé D dává totéž). Změna D/H/okna = nová verze
+ full-replace přepočet `sentiment_episodes` (WavesJob), řádky nesou
`params_version` a `series_variant = 'zscore_100'`; verze se nemíchají
(týž princip jako `mechanics_version` u setupů). Přeměření týmž skriptem
**~5. 11. 2026** (100 řádků od 28. 7. = čistá σ) a při ≥ 20 rozhodnutých
epizodách per symbol; do té doby UI všude říká „předběžné".

### 3. Epizodový stav (`assess_episode`)

`status`: `open` = epizoda probíhá (třída neznámá — zadání znalo jen
attempt|negation|none, `open` přibyl, protože třída je známá až dnem
zahlazení nebo horizontem); `negation` = poslední epizoda skončila negací
a close_z se od té doby nevrátil nad její referenční úroveň (korekce trvá);
`attempt` = zahlazeno v poslední den řady (poté je korekce pryč a stav nemá
co tvrdit; historie zůstává v `last_resolved` a tabulce); jinak `none`.
`correction_threshold` = aktuální práh v σ (20denní maximum − D).

### 4. Rozhraní

- Tabulka `sentiment_episodes` (symbol, start_date, end_date, ref_level_z,
  depth_z, label, length_days, params_version, series_variant), plní WavesJob
  full-replace per symbol, jen uzavřené dny (jako vlny).
- `GET /sentiment/state` + WS `sentiment.state`: `episode_status`, `episode`,
  `last_episode`, `correction_threshold`, `correction_threshold_d`,
  `episode_horizon_h`, `episode_params_version`.
- `GET /stats/episodes[?symbol]`; `GET /sentiment/daily?symbol=…` nese per den
  `correction_level_z` (sdílená funkce nad celou řadou — UI nekreslí vlastní
  výklad prahu).
- UI: badge v `StateChip` (KOREKCE n d / POKUS / NEGACE, tooltip s odrážkami
  a předběžností), řádek „Korekce" v popoveru, schodovitá linie prahu ve
  spodním panelu Sentiment v Daily pohledu (osa surová kvůli érám, popisek
  v σ), blok „Korekční epizody" ve Stats vedle vln.

## Důsledky

- `assess_state` ani potvrzovací práh ADR-0019 se nemění — epizody jsou
  vrstva vedle, ne náhrada.
- Epizody vznikají jen tam, kde má řada close_z a 20 řádků historie: backfill
  éra od ~2023-09, živá éra celá. Historické epizody backfill éry jsou
  v tabulce (mechanika je stejná), ale kalibrace se z nich dělat nesmí (#640).
- Prvních ~7 týdnů po nasazení hlásí stav epizody s kontaminovanou σ — badge
  i Stats to říkají („předběžné"). Signální větev (#453) může `episode_status`
  číst, ale ne kalibrovat, dokud neproběhne přeměření.
- Do `scripts/measure_sentiment_episodes.py` se nesahá — zůstává měřicím
  nástrojem se širším gridem (varianta B, pravidlo `either`) pro příští
  přeměření; parita s produkční definicí je hlídaná testem.
