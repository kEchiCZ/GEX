# ADR-0021: Track record — pinnuté detaily mechanického backtestu (#298)

## Stav
Navrženo (needs-decision)

## Kontext
SPEC 7.3 předepisuje noční job s mechanickými equity křivkami (stavová
strategie vs. buy & hold, signálové strategie NEWS/COMBINED), point-in-time
vstupy (S11) a vyloučení kalibračního období (5.6). Několik detailů ale
nechává otevřených.

## Rozhodnutí

1. **Začátek vyhodnocovacího období** = konec první periody, ve které
   existují aspoň **3 uzavřené vlny v každém směru**. Adaptivní práh
   potvrzení (5.6) je do té doby 0 (stav stojí jen na MA podmínce) — období
   s neaktivním prahem je kalibrace, ne vyhodnocení. Konkrétní datum se tak
   odvozuje z dat (walk-forward), ne z pevného kalendáře.
2. **Stavová strategie**: RiskOff = **flat** (default). Short při RiskOff je
   konfigurovatelný přepínač jobu (`short_riskoff`), zatím vypnutý — SPEC ho
   uvádí jako volbu, ne default.
3. **Vstup/výstup**: stav dne d−1 (z closes ≤ d−1) → pozice platí pro den d
   od **open**; den změny pozice se skládá ze dvou úseků (stará pozice
   close→open, nová open→close). Signálové obchody: vstup na open prvního
   1min baru ≥ ts signálu, výstup na close prvního baru ≥ expiry_ts
   (při díře v datech poslední dostupný bar před expirací); běžící obchody
   se nereportují.
4. **Denní seance** = UTC partice archivu barů (open prvního a close
   posledního 1min baru dne); rozjetý dnešek se do křivky nezapisuje.
5. **Souhrn v UI**: CAGR a max drawdown z křivky pro všechny strategie;
   hit-rate jen pro signálové strategie (z `signal_outcomes`) — u stavové
   strategie a buy & hold není trade, nad kterým by dávala smysl.
6. **Zápis**: plný přepis `track_record` za symbol při každém běhu — vstupy
   jsou immutable (S11), takže přepis je deterministický; přírůstkový zápis
   by jen komplikoval opravy děr.

## Důsledky
- Track record se objeví, až historie vln unese adaptivní práh; do té doby
  job loguje „kalibrace ještě běží" a tabulka je prázdná.
- Signálové křivky budou zpočátku řídké (signály se generují od otevření
  Wilson gate 29. 7. 2026) — křivka drží hodnotu mezi obchody.
- Bez exekučních nákladů a skluzu: čísla jsou horní odhad, slouží výhradně
  jako sebe-kontrola systému (SPEC 7.3), ne jako obchodní doporučení.
