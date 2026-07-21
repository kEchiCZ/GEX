# ADR-0006: Projekce heatmapy do konce seance + jemnější seance

**Stav:** přijato (2026-07-21, rozhodl uživatel)
**Kontext:** SPEC v2.0 (kap. 7.2) popisuje heatmapu jen nad naměřenými minutami —
osa X končí posledním snapshotem. Referenční nástroj (Moodix) kreslí gamma
plochu až do konce seance, i tam, kde ještě žádná cena není.

## Problém

Bez projekce se z grafu špatně čte otázka, kvůli které nástroj existuje:
*„kam mě zbytek dne tlačí positioning?"*. Zdi a flip jsou funkcí OI, které se
mezi minutami mění málo — jejich tvar v čase je proto do značné míry předvídatelný
a je užitečné ho vidět dopředu. Dnes uživatel vidí jen plochu do „teď" a musí si
pokračování domýšlet.

## Rozhodnutí

1. **Čistě klientská vizualizační vrstva.** Engine, API ani Parquet se nemění —
   projekce je odvozená z dat, která klient už má. Nic se neukládá.
2. **Horizont = settle expirace** (`expirySettleUtc`, 20:00 UTC dne expirace).
   Po settle nebo pro Daily pohled se neprojektuje. Strop `PROJECTION_MAX_MINUTES`
   brání absurdně široké ose u vzdálených expirací.
3. **Obsah = poslední naměřený sloupec držený konstantní.** Žádná extrapolace
   trendu ani modelování decay — projekce odpovídá předpokladu „OI se do konce
   seance nezmění". Cokoliv chytřejšího by předstíralo znalost, kterou nemáme.
4. **Projekce musí být na první pohled odlišitelná od dat.** Kreslí se se
   sníženou sytostí (`PROJECTION_ALPHA`) a odděluje ji svislý předěl v místě
   posledních dat. Bez toho by graf tvrdil, že vpravo jsou naměřené hodnoty —
   to je horší než projekci vůbec nemít.
5. **Projekce neovlivňuje nic než plochu heatmapy.** Playback, spodní panely,
   profil, levels ani cena se do projektované oblasti nerozšiřují; pracují dál
   nad naměřenými minutami. `HeatmapGrid` proto nově rozlišuje `minutes`
   (celkem sloupců) a `dataMinutes` (kolik z nich je naměřených).
6. **Přepínatelné** (`Projekce` v řádku přepínačů), výchozí zapnuto pro intraday.

## Sdílená časová osa

Spodní panely počítají šířku koše z délky vlastní řady (`baseBucketPx`).
Rozšíří-li se osa heatmapy, musí panely dostat **stejný počet sloupců**, jinak
se časové osy rozjedou (stejná třída chyby jako u PR #103). `BottomPanels` proto
dostává `totalMinutes` pro měřítko a kreslí dál jen svá data.

## Jemnější seance

`WORLD_SESSIONS` se rozšiřuje ze 7 na 14 markerů (Sydney, Tokio, Šanghaj, Indie,
Frankfurt, Londýn, US — otevření i zavření). Časy byly původně pevné v UTC bez
DST korekcí; po review #132 dostaly US a evropské seance aproximaci DST po
celých dnech UTC (#159) — mimo letní čas se posouvají o hodinu později, asijské
zůstávají pevné (jde o orientaci, ne o burzovní kalendář).

Markery, které padnou na tutéž minutu, se slučují do jednoho popisku
(`Frankfurt · Londýn`), aby se texty nepřekrývaly; sousední markery se střídají
ve dvou řádcích.

## Důsledky

- Osa X intraday grafu sahá po settle; při málo datech je plocha z větší části
  projekce — proto je odlišení sytostí zásadní.
- `HeatmapGrid.dataMinutes` je nové povinné pole; konzumenti, kteří potřebují
  „kolik je skutečných dat", musí sáhnout po něm, ne po `minutes`.
- Walls módy se počítají **jen z naměřené části** gridu (před projekcí) a
  jejich linie končí na předělu — projekce nekreslí žádné „zdi" v budoucnu.
  (Původní znění ADR tvrdilo opak; kód to od začátku dělal bezpečněji a po
  review #132 je autoritativní tohle znění, viz #156.) Horizontální projekce
  pojmenovaných úrovní s cenovkou přes celou šířku zůstává.

## Upřesnění po review #132 (#156)

- **Stáří buněk se do projekce nepřenáší** — projekce není „stará data", je to
  předpoklad; projekční sloupce mají stáří 0, i když poslední naměřená minuta
  stale je.
- **Strop `PROJECTION_MAX_MINUTES` je v minutách reálného času** a ořezává se
  před přepočtem na koše — na každém timeframe znamená stejný časový úsek.
- **Při přetáčení (mimo live) se neprojektuje** — slice nuluje buňky za pozicí
  a projekce by držela vynulovaný sloupec.

## Ověření

- Projekce má hodnoty posledního naměřeného sloupce a nemění naměřenou část.
- `dataMinutes` zůstává počtem naměřených minut; playback nejde scrubnout
  do projekce.
- Panely a heatmapa mají shodné měřítko osy X i s projekcí.
- Seance: 14 markerů, kolize na stejné minutě se slučují.
