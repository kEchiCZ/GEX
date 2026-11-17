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

## Dodatek 2026-11-17: identita položky kalendáře = řada + minuta releasu (#1298)

- **Kontext:** `dedup_hash` (SPEC 3.3) nesl u všech zpráv normalizovaný titulek a den UTC.
  U položky kalendáře to znamenalo nejvýš jeden release řady za den. Když FF uvedl dvě hodnoty
  téže řady v jeden den, `ON CONFLICT DO NOTHING` druhou zahodil. Dry-run 17. 11. proti historii
  FF od 7/2023 našel 172 chybějících položek (110 USD, z toho 12 USD High): dohánění po shutdownu
  2025 (NFP a AHE 16. 12. 2025 13:29 / 13:30, PPI a Core PPI 14. 1. 2026, Core PCE 22. 1. 2026,
  JOLTS 9. 12. 2025), ADP Weekly (FF uvádí dřívější týden o minutu dřív a přidává ho dodatečně),
  zásoby EIA 15. a 16. 11. 2023 (dva týdny najednou) a dva projevy člena FOMC v jednom dni.
- **Rozhodnutí (vlastník 17. 11., varianta A2):**
  1. Identita položky kalendáře (`kind = scheduled`) = normalizovaný titulek + **minuta releasu
     v UTC** (`compute/newstext.scheduled_key`). `dedup_hash` i klíč rolling dedupu kalendáře
     jsou z ní. Ostatní zprávy mají v klíči dál den (cross-source merge, ADR-0059 bod 3).
  2. FF `id` identitou není: nese ho jen historická stránka, widget feed ne (ADR-0013;
     v produkční DB 17. 11. má `raw.id` 14 771 z 16 481 řádků FF, zbytek je živý sběr).
     Minutu releasu znají obě cesty a `source_uid` ji nesl už dřív.
  3. **Posun času 1:1 není nový release.** FF po zveřejnění posouvá čas položky (tentative
     releasy, aukce, BOJ, čínská data) a widget feed posun převezme (Cleveland Fed 16. 11.:
     uloženo 14:48, feed 14:45). Za 3,5 měsíce živého sběru 103 řádků, ~6 týdně, 88 Low,
     12 High (BOJ, BOE). Ve skupině řada + den UTC se položka páruje s řádkem podle FF `id`,
     pak podle minuty; zbude-li jeden řádek, který ve feedu není, a jedna nová položka, je to
     posun (`gexlens_news/ffcalendar`). Živý zápis, `backfill-ff` i skript nový řádek
     nezaloží, refresh actual dá hodnotu původnímu řádku. Výsledek je stejný jako před #1298
     s denním klíčem: jeden řádek s původním časem a s actual. Jiný nepoměr ve skupině živý
     zápis zapíše (release se neztratí), skript ho jen vypíše (v historii 0 případů).
  4. Historie se srovná jednorázově `scripts/backfill_ff_scheduled_1298.py`: přepočet klíče
     řádků `scheduled` a doplnění chybějících položek z historické stránky stejným párováním.
  5. Dotaz kalendáře pro čas releasu BLS (ADR-0059 bod 5) vrátí ze dvou releasů téhož dne
     pozdější čas, tedy čas aktuálního releasu.
- **Zamítnuté varianty:** A (jen klíč s minutou) by z každého posunu udělala druhý řádek a
  původní by zůstal bez actual; fantom s importance 2 (BOJ, Bessent) by kazil kontaminaci K1
  okolních reakcí. B (pořadí položky v rámci dne, první = denní klíč) nepotřebuje přepočet
  klíče, ale u ADP Weekly by dodatečně přidaná dřívější položka převzala denní klíč, její actual
  by šel do pozdějšího řádku a ten by vznikl podruhé. Přesun řádku na skutečný čas (C) by
  vyžadoval přeměření reakce a predikcí; to je samostatné issue.
- **Důsledky:**
  - Posunutý release má dál původní čas a reakce se měří od něj (BOJ 30. 10. o 32 min dřív).
    Platí to stejně jako před #1298; oprava je samostatné issue.
  - Párování potřebuje celou stránku nebo feed týdne: řádek „chybí ve feedu“ jen tehdy, když
    feed pokrývá jeho den. Den UTC na dvou týdnech FF (sobota večer ET) by se mohl spárovat
    falešně; řada se dvěma releasy v tu dobu v datech není.
  - Nasazení vyžaduje pořadí: zastavit news-engine, přepočet klíče, spustit nový. Návrat na
    starší image vrací denní klíč přes `--revert` (ADMIN-MANUAL kap. 12).
