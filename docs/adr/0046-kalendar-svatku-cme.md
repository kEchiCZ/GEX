# ADR-0046: Kalendář svátků a zkrácených seancí z pravidel NYSE

- **Stav:** přijato (zadání #1308, 7. 10. 2026)
- **Datum:** 2026-10-07
- **Souvisí:** #1308, #1307 (hlídače při zavřeném trhu), #1309 (jeden predikát obchodního dne),
  #1294 (předobchodní souhrn o svátku), ADR-0023 bod 4, ADR-0037, ADR-0039

## Kontext

ADR-0023 bod 4 řekl „svátky neřeší kalendář, rozhodují bary“ a `marketclock` výslovně
odmítl udržovaný seznam svátků („tiše zastará“). Pro data, která už existují (reakce zpráv,
market_closed u zpráv #339), to platí dál. Hlídače dat (#1307) se ale ptají **předem**,
jestli se data čekají, a bary, které ještě nepřišly, odpověď nedají: o Vánocích by
`feed_crosscheck` hlásil „oba zdroje mlčí“ ~26×, o Thanksgivingu ~12×. Stejně tak
`is_trading_session` (#1309) a jeho volající (publikace OI, ΔOI, FA/α, scénáře, verdikty,
Forward GEX) braly svátek za obchodní den a zkrácená seance měla settle v 16:00 ET.

## Rozhodnutí

1. **Kalendář z pravidel, ne seznam dat**: `compute/settle.us_market_holidays(year)` počítá
   celodenní svátky NYSE (Nový rok, MLK, Presidents, Velký pátek přes výpočet Velikonoc,
   Memorial, Juneteenth od 2022, 4. 7., Labor Day, Thanksgiving, Vánoce; víkendový svátek
   v pátek/pondělí, Nový rok v sobotu se nepřesouvá). Pravidla nezastarají; frontendový
   protějšek `instrument/holidays.ts` se mění spolu.
2. **Obchodní den** `is_trading_session` = po–pá mimo svátky (a tím všichni volající, #1309).
   Zkrácená seance (`is_early_close`: den po Thanksgiving, 24. 12. a 3. 7. po–čt) obchodní
   den je, jen **settle i konec US RTH je 13:00 ET** (`settle_ts`, `outside_us_rth`,
   frontend `expirySettleUtc`, `outsideUsRth`). Tím i roll 0DTE expirace (ADR-0039 dodatek).
3. **Rozvrh Globexu** (`marketclock.is_market_closed`): Vánoce, Nový rok, Velký pátek zavřeno
   od zavření předchozího dne do 17:00 CT; ostatní svátky obchod do 12:00 CT, pak do 17:00 CT
   zavřeno; zkrácená seance do 12:15 CT.
4. **Bary mají dál konečné slovo** u historie (ADR-0023 bod 4 platí pro změřená data):
   mimořádná zavření (státní smutek) a halty pravidla neznají — rozvrh je odhad pro hlídače.
5. **Epizody sentimentu** (ADR-0037) svátky přestávají počítat jako obchodní dny — shoda se
   skutečnými seancemi měřicího skriptu.

## Důsledky

- O svátku neběží obnova OI (`_oi_published`), FA/α, scénáře ani verdikty; ΔOI přeskočí
  sváteční archiv; Forward GEX svátek vynechá z horizontu.
- Partice barů svátku s Globexem (např. Thanksgiving do 12:00 CT) `deepbars.task_is_covered`
  nevyžaduje — bary se zapisují živě; hluboký backfill je dotáhne s okolními dny.
- Zkrácené seance mají settle 13:00 ET i pro gamma útes, stav mapy, T6 a vyhodnocení po settle.
- Nepřesnost: o přesunutém svátku (Vánoce/Nový rok v pondělí) modelujeme Globex jako
  u ostatních svátků (do 12:00 CT); Velký pátek s výplatní páskou CME občas obchoduje
  zkráceně — bary to při vyhodnocení opraví.
