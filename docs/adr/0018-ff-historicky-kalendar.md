# ADR-0018: Historický kalendář FF, actual hodnoty a surprise_z

**Stav:** navrženo (PR s labelem `needs-decision`)
**Datum:** 2026-07-29
**Issue:** #277 (navazuje na ADR-0013, který rozhodnutí explicitně odložil sem)

## Kontext

Widget feed ForexFactory nese jen aktuální týden a **nemá pole `actual`**
(ADR-0013). Pro počáteční trénovací dataset (SPEC 3.4) zbývaly tři varianty:
(1) scrape webového kalendáře FF, (2) aproximace forecast ≈ previous,
(3) surprise jen z FRED/BLS hodnot bez konsensu.

**Měření 29. 7. 2026:** historické stránky `forexfactory.com/calendar?week=jul7.2025`
jsou dostupné běžným HTTP klientem s prohlížečovou UA (200, ~350 kB) a nesou
vložený JSON (`calendarComponentStates`) s kompletními daty: `name`,
`currency`, `dateline` (**epoch UTC**, nezávislý na timezone stránky),
`impactName`, **`forecast` i `actual`**, revize, a dokonce stabilní `id`
eventu. Tedy plná varianta 1 bez aproximací.

## Rozhodnutí

1. **Backfill = scrape historických stránek FF** (`ffhistory.py`), jednorázový
   CLI příkaz `python -m gexlens_news backfill-ff [--weeks N]`, default 156
   týdnů (3 roky, konfig `GEXLENS_NEWS_FF_BACKFILL_WEEKS`). Šetrně: 1 stránka
   / 2 s, chyba týdne se přeskočí, celé je to idempotentní (dedup_hash +
   doplňování NULL hodnot) — dá se kdykoli pustit znovu.
2. **Normalizace zrcadlí živý collector** (titulek `{MĚNA} {název}`,
   `source_uid`, `dedup_hash`), takže tentýž event z obou cest splyne.
   U eventů, které už v DB jsou (živý sběr), se doplňuje jen NULL `actual`
   (+ forecast/previous) — už vyplněné hodnoty se nepřepisují, aby revize
   nemenily data, na kterých stavěly reakce a klasifikace.
3. **Actual pro živý provoz: hodinový `FfActualRefreshJob`** nad toutéž
   stránkou (aktuální + začátkem týdne minulý týden). ADR-0013 počítal
   s mapováním BLS/BEA/FRED řad — to zůstává jako budoucí zpřesnění;
   FF stránka pokrývá VŠECHNY kalendářní eventy (i ifo/PMI bez oficiálního
   API) jedním mechanismem. **Vědomý kompromis:** latence actual ~1 h
   neplní cíl „actual do 3 min" (SPEC kap. 10) — ten zůstává na budoucím
   napojení oficiálních API; pro trénovací dataset a surprise_z je hodinová
   latence irelevantní.
4. **surprise_z = (actual − forecast) / σ překvapení řady**, kde řada =
   normalizovaný titulek a σ = výběrová směrodatná odchylka historických
   překvapení (min. 6 vzorků). SPEC formulaci „σ z FRED/BLS historie"
   naplňujeme věrněji: σ se počítá přímo z (actual − forecast) párů, které
   FRED nemá (nenese konsensus). Full-recompute po každém doplnění actual —
   σ se s každým releasem zpřesňuje.

## Důsledky

- ~150 týdnů × ~70 eventů ≈ 10 k scheduled eventů s měřitelným překvapením
  jako počáteční dataset; klasifikuje je pravidlový pass (scheduled se do
  Gemini neposílají) a směr dostávají ze surprise_z + znaménkové konvence.
- Reakce hlubší historie vyžadují 1min bary starší než aktuální archiv —
  řeší se samostatně (rozšíření bar backfillu enginu / #278 doplněk); bez
  nich dataset slouží pro σ řad a surprise_z, buckety reakcí se plní od
  začátku živého sběru.
- Scrape je závislý na neoficiálním formátu stránky — parsování je
  defenzivní (změna formátu = prázdný výsledek + warning, nikdy pád)
  a jde o jednorázový/hodinový nízkoobjemový přístup s browser UA
  (precedens ADR-0014).
