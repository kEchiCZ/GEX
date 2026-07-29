# ADR-0016: Fuzzy dedup přes token Jaccard místo simhashe

**Stav:** navrženo (PR s labelem `needs-decision`)
**Datum:** 2026-07-29
**Issue:** #274 (souvisí: #351)

## Kontext

SPEC sentiment 3.3 zavádí exaktní dedup (normalizovaný titulek, rolling okno
10 min + `dedup_hash` titulek+den) a jako vědomé omezení uvádí, že nechytí
přeformulovanou story; jako follow-up jmenuje **simhash**.

Vyhodnocení na provozních datech (29. 7. 2026, 1658 zpráv z ~24 h provozu,
všechny páry v okně 36 h; skript i plné výsledky v komentáři
[#274](https://github.com/kEchiCZ/GEX/issues/274#issuecomment-5115305643)):

1. **Simhash (64bit, unigram+bigram) neodděluje.** Pravé reformulace mají
   Hamming 2–10, falešné páry 7–11 — pásma se překrývají v obou směrech
   („Durable Goods" vs „**Core** Durable Goods" H=7, zatímco pravý pár
   „Boeing bigger/wider loss" H=10). Neexistuje práh s použitelnou precision.
2. **Token Jaccard ≥ 0.9 je bezpečný:** 24 párů, ručně ověřeno, 0 falešných.
   Pásmo 0.83–0.88 už míchá pravé reformulace s falešnými merge (různé firmy
   v šablonových titulcích „X Q2 Earnings Call Highlights", Core-prefix
   makro události).
3. Nejčastější falešný kandidát jsou `scheduled` eventy — položky kalendáře
   se liší jedním tokenem, ale jsou to **různé události**, ne reformulace.
4. Hlavní objem propuštěných duplicit (~19/den) nejsou reformulace, ale
   exaktní republikace přes půlnoc UTC — řeší samostatné issue #351, ne
   fuzzy vrstva.

## Rozhodnutí

1. Simhash se **nenasazuje** (odchylka od textu SPEC 3.3, podložená měřením).
2. Fuzzy vrstva = **token Jaccard ≥ 0.9** nad týmž rolling oknem, v
   `RollingDeduplicator` (news-engine). Shoda se chová stejně jako exaktní:
   jiný zdroj → merge do `raw.merged_sources`, týž zdroj → duplicita.
3. Fuzzy se pouští jen na `kind ∈ {headline, broker}`; `scheduled` eventy
   nikdy (konstrukčně eliminuje třídu Core-prefix falešných merge).
4. Práh je konstanta `DEFAULT_JACCARD_THRESHOLD = 0.9`; `jaccard_threshold=None`
   vrstvu vypíná.

## Důsledky

- Zisk ~5 sloučených reformulací/den nad rámec exaktního dedupu (měřená
  precision 100 %); reformulace v pásmu 0.83–0.88 zůstávají vědomě nechycené
  — cena za nulové falešné merge.
- Prahy odvozené z jediného dne provozu → **přeměřit po ~týdnu** (stejný
  skript), zejména až se zapne delší okno z #351, které fuzzy porovnání
  rozšíří na víc kandidátů.
- Lineární sken okna (množinové průniky) je při oknech v minutách až
  hodinách levnější než LSH index; při případném řádovém růstu objemu zdrojů
  přehodnotit.
