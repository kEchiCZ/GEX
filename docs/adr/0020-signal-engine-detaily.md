# ADR-0020: Signal engine — pinnuté detaily nad SPEC kap. 6

**Stav:** navrženo (PR s labelem `needs-decision`)
**Datum:** 2026-07-29
**Issue:** #294

## Kontext

SPEC kap. 6 definuje gate (6.2), pravidlovou logiku (6.3) i expiraci
„dohasnutím eventu (half-life) nebo potvrzenou změnou stavu" — ale nechává
otevřené čtyři kvantitativní detaily, bez kterých nejdou psát golden testy.

## Rozhodnutí

1. **Čerstvost eventu**: event smí založit signál, dokud jeho stáří ≤ τ
   (half-life kategorie × důležitosti ze SentIndex defaultů). Po τ z indexu
   z poloviny vyhasl — „čerstvý event" ze SPEC 6.3 interpretujeme decayem,
   ne pevným oknem, aby FED (τ 4,5 h) žil déle než TECH titulek (τ ~1 h).
2. **Expirace** `expiry_ts = ts_event + τ` — vázaná na event, ne na okamžik
   vzniku signálu (signál založený pozdě v životě eventu expiruje dřív).
   Potvrzená změna stavu (denní close, #292) expiruje aktivní signály
   okamžitě nastavením `expiry_ts = now` — `inputs` zůstávají immutable
   (S11), `expiry_ts` je lifecycle pole. Unconfirmed změna nic neruší
   (SPEC 6.3 — jen badge v UI).
3. **Strength = min(1, |skóre eventu|)** (skóre = směr × síla × w_cat).
   Síla klasifikace už nese confidence×magnitude (SPEC kap. 4); další
   škálování (Wilson LB, hloubka vlny) by míchalo jistotu bucketu do síly
   eventu — bucket jistotu UI ukazuje zvlášť (n, Wilson LB, SPEC 6.4).
4. **COMBINED vyžaduje dostupný GEX kontext** — bez barů (spotu) se COMBINED
   signál negeneruje, chybějící flip nebo sklon jen nepodporuje (chybějící
   kontext ≠ neutrální kontext; větev má měřit přidanou hodnotu kontextu,
   SPEC 6.3). Kontext: spot nad/pod flipem
   NEBO sklon CumΔ za posledních 10 minut.
   *Doplněk 21. 10. 2026 (#1589, #1583, ADR-0057 bod 4):*
   - Kontext se čte k okamžiku vzniku signálu. Razítko je čas těsně před
     výpočtem, ne začátek cyklu news-engine, a řádky po něm se nečtou.
   - Řádek starší než 2 min za minutou signálu neplatí: starý spot znamená,
     že kontext chybí; starý flip nebo sklon znamená, že ta noha nepodporuje.
     Flip dává jen aktivní řetěz, tedy nejbližší expirace před vlastním settle,
     která má řádky. Po settle tak flip nejde z expirovaného řetězu. Zpožděný
     aktivní řetěz znamená, že flip chybí; flip sekundáru D+1 ho nenahradí.
   - Sklon z tisků (řádky flow se zdrojem `aggressor`) = Σ flowΔ za 10 min
     v seanci, táž čistá funkce jako u tendence (`compute/cumdelta.flow_window_sum`).
     Mezera, neúplné okno nebo okno přes začátek seance (17:00 CT) znamená, že
     sklon chybí: noha nepodporuje a flip potvrdit může.
   - Pod midpointem zůstává rozdíl CumΔ jako dřív (ADR-0057 bod 11).
     Zlom poznají řádky, ne datum.
   - `inputs.gex` nese zdroj CumΔ (`cum_delta_source`, ADR-0057 bod 8)
     a minuty řádků, ze kterých kontext je.

Dedup: jeden signál per (event, mode) — anti-spam; event se po expiraci
signálu znovu nepoužívá (dedup okno = lookback kandidátů 8 h > max τ).

## Důsledky

- `signal_outcomes` tabulka doplněna do schématu (chyběla; precedens
  `news_weights` #282): realizovaný pohyb v oknech 1/5/15/60 po signálu,
  `correct` vůči směru — podklad pro srovnání NEWS vs. COMBINED (SPEC 6.3)
  a track record (#298).
- Signály se počítají always-on pro symboly z `GEXLENS_NEWS_SIGNAL_SYMBOLS`
  (default ES; NQ = přidat do výčtu, SPEC 6.5). Režim OFF/NEWS/COMBINED je
  čistě zobrazovací (S9, UI v #295).
- Gate stojí na `news_model_stats` — dokud buckety nemají n ≥ 30 a Wilson
  LB > 0.50, signály nevznikají (UI „collecting data", #295). Reakce nad
  hlubokými bary (#369) plnění bucketů výrazně urychlí.
