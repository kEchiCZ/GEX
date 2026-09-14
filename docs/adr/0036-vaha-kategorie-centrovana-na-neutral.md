# ADR-0036: Váha kategorie centrovaná na neutrál 1,0 (revize SPEC 5.3)

- **Stav:** přijato (uživatel 14. 9. 2026, #1150 varianta B)
- **Datum:** 2026-09-14
- **Souvisí:** #1150, #453, #1136, #640, ADR-0026, sentiment-SPEC-v1.md §5.3

## Kontext

SentIndex byl pro NQ od 10. 9. a pro ES od 14. 9. 2026 v každé minutě
identicky 0,000, přestože eventy se skórem chodily (7denní okno ~2 400
eventů). Příčina: `load_events` násobí `sentiment_score × w_cat` a váha se
odvozovala z Wilsonovy dolní meze hit-rate predikcí jako `max(0, 2·LB − 1)`.
Žádná dvojice (kategorie, predictor) nemá LB nad 0,5 (rozsah 0,25–0,50, tedy
predikce na úrovni mince), takže **všech 40 řádků `news_weights` mělo váhu
0,0** a index byl součtem samých nul.

Chyba byla v návrhu, ne v datech: chybějící váha (bucket pod 20 vzorků)
znamenala neutrál 1,0, spočtená váha na úrovni mince znamenala 0. Index tedy
umíral postupně, jak buckety překračovaly práh vzorků — **čím víc dat, tím
mrtvější index**. NQ dojel dřív, protože jeho buckety naplnily práh dřív.

Druhá chyba: `load_weight_map` vracel `dict[category → weight]`, ale primární
klíč tabulky je `(category, predictor, window_min, symbol)`; pro každou
kategorii existují dva řádky (rule, llm) a vyhrál libovolný poslední.

## Rozhodnutí

1. **Mapování centrované na 1,0.** `weight_from_hit_rate(LB)` =
   `clamp(1 + GAIN·(2·LB − 1), WEIGHT_MIN, WEIGHT_MAX)` s konstantami
   v `predictions.py`: `WEIGHT_EDGE_GAIN = 2.0`, `WEIGHT_MIN = 0.25`,
   `WEIGHT_MAX = 2.0`. LB = 0,5 dává **přesně 1,0** — totéž co chybějící
   váha, takže překročení `MIN_SAMPLES_FOR_WEIGHT` nedělá skok. Edge nad
   mincí zesiluje (až 2×), pod mincí tlumí (nejméně na čtvrtinu), ale
   **nikdy na nulu ani do záporu** — záporná váha by otáčela znaménko
   (přefitování), nulová vymaže kategorii z indexu (tato chyba).
2. **Váha per (kategorie, predictor).** `load_weight_map` vrací mapu
   s klíčem dvojice; event se váží vahou toho, kdo mu dal skóre
   (`news_events.sentiment_source`), přes `event_weight`. Event bez zdroje
   nebo bez řádku = 1,0. Jediná dvě čtecí místa (`SentIndexJob.load_events`,
   `sentiment_backfill.load_scored_events`) jdou přes tuto funkci.
3. **Retro přepočet.** CLI `python -m gexlens_news recompute-sentindex
   --from YYYY-MM-DD [--to YYYY-MM-DD]` přepočte 1min partice
   `derived/sentiment/{SYMBOL}/{den}.parquet` a svíčky `sentiment_daily`
   toutéž mechanikou jako živý job (řada se počítá celá znovu z eventů,
   partice se přepisuje, svíčka upsertuje). Dnešek jen do „teď", zbytek
   doplní živý job. σ a close_z (#640) se od `--from` dál smažou a dopočtou,
   protože `refresh_z` jinak historii nechává immutabilní. Jednorázově
   spuštěno pro 10.–14. 9. 2026.

## Důsledky

- **Měřítko indexu se mění** proti historii před 14. 9.: dosud vážily
  kategorie 1,0 (bez řádku) nebo 0,0 (s řádkem), nově 0,25–2,0. Partice
  starší než 10. 9. se nepřepočítávají (retence 14 dní je stejně smaže);
  `sentiment_daily` před 10. 9. zůstává v původním měřítku. σ z předchozích
  100 seancí (#640) tedy přechodně mísí obě měřítka — close_z bude
  po dobu ~100 seancí podhodnocené nebo nadhodnocené podle směru posunu.
  Vědomě přijato: správnost dopředu má přednost před bitovou shodou historie
  (stejná zásada jako ADR-0026).
- Signální větev (#453) dostane nenulový index; „stav trvale Neutral" se
  vyhodnotí znovu až po pár dnech nových dat.
- Tendence (#1136) čte per-symbol partici — po retro přepočtu je hlas
  sentindex nenulový i zpětně pro 10.–14. 9.
- `PredictionJob.recompute_weights` a schéma `news_weights` se nemění; noční
  přepočet vah použije nové mapování při dalším běhu.
