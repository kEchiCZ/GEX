# ADR-0026: Sentiment pipeline per symbol (revize SPEC 6.5)

- **Stav:** přijato
- **Datum:** 2026-08-12
- **Souvisí:** #579, #453, #563, sentiment-SPEC-v1.md §6.5

## Kontext

SPEC 6.5 sliboval: „zapnutí NQ = konfigurační přepínač, ne refactoring."
Diagnostika v #453 doložila, že přepínač je rozbitý na dvou místech:

1. **Stav by se pro NQ nepřepočítal.** `GEXLENS_NEWS_SIGNAL_SYMBOLS=ES,NQ`
   rozšíří jen smyčku v `SignalJob`; stav ale přitéká z jediné ES instance
   `WavesJob`. NQ signály by vznikaly proti ES režimu a `inputs` snapshot
   (SPEC 6.3 — každý signál zpětně vysvětlitelný) by o tom mlčel.
2. **Váhy nejsou schema-ready.** Komentář v `sentindex_job.py` tvrdil, že
   per-symbol vážení jde zapnout bez změny schématu; `news_weights` ale
   sloupec `symbol` nemá — váhy se počítají přes oba symboly dohromady,
   takže „NQ index" by byl jen kopie ES křivky.

## Rozhodnutí

1. **Váhy per symbol.** `news_weights` dostává sloupec `symbol` (součást
   primárního klíče). `PredictionJob.recompute_weights` počítá váhy z outcomes
   daného symbolu; `load_weight_map(engine, symbol)` je jediné čtecí místo.
   Migrace: plný přepočet (tabulka je celá nahrazovaná nočním jobem, žádná
   historie se nepřenáší).
2. **SentIndex per symbol.** `SentIndexJob` počítá řadu pro každý symbol
   z týchž eventů, ale s vahami symbolu. Parquet layout:
   `derived/sentiment/{SYMBOL}/{den}.parquet`; historické ploché soubory
   `derived/sentiment/{den}.parquet` zůstávají jako ES legacy — čtení
   (API) padá na legacy cestu, když per-symbol partice neexistuje. Denní
   OHLC do `sentiment_daily` per symbol (skutečné hodnoty, ne kopie).
3. **Stav per symbol.** Instance `WavesJob` per symbol; WS `sentiment.state`
   nese `symbol` (už nesl). `SignalJob.run` dostává mapu `states` a signál
   generuje proti stavu SVÉHO symbolu; `inputs` nově nesou `state_symbol`.
4. **Backfill per symbol.** `backfill-sentiment-daily` CLI projde všechny
   symboly; NQ řada se dopočte z historických eventů s NQ vahami.
5. **Rozdíl reakcí NQ − ES** se neukládá duplicitně: obě reakce trvale leží
   v `news_reactions`, rozdíl definuje SQL view `news_reaction_spread`
   (per event × okno). Materializace by porušila zásadu jednoho zdroje
   pravdy; view je „uložení" ve smyslu AC — dotaz jedním selectem.
   Od ADR-0031 (#998) je `news_reactions` široká tabulka (řádek per
   event × symbol) a view je nad ní definované jako UNION ALL per okno —
   sloupce view zůstávají stejné.
6. **Mimo rozsah:** `ReviewJob` (fronta je o klasifikaci zprávy, symbol
   nemá), `TrackRecordJob` (SPEC 7.3 definuje stavovou strategii nad ES;
   NQ křivka až po nasbírání NQ track recordu), kadence LLM (klasifikace
   zprávy je jedna, měření reakcí je práce nad DB).

## Důsledky

- `GEXLENS_NEWS_SIGNAL_SYMBOLS` default se mění na `ES,NQ` — tím se plní
  AC #579 „signály samostatně pro ES i NQ". Vypnutí NQ = odebrat z výčtu.
- ES výstupy se nemění (regresní kritérium: ES řada, vlny i stav beze změny
  proti stavu před migrací — váhy ES se nově počítají jen z ES outcomes,
  což JE drobná změna hodnot: dosud je ředily NQ outcomes; správnost má
  přednost před bitovou shodou).
- API `/sentiment/*` už `symbol` nesou; frontend chip a panel se řídí
  zobrazeným instrumentem (konvence #114/#115: zvoneček globální, karty
  per instrument).
