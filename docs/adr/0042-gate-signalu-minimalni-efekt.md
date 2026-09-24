# ADR-0042: Gate signálů vyžaduje minimální velikost reakce (|Ø| ≥ 1 bp)

- Stav: přijato (24. 9. 2026, rozhodnutí uživatele — #1265 varianta A)
- Souvisí: sentiment SPEC 6.2 (gate), ADR-0020 (signal engine), ADR-0036 (váhy), #1264 (H1), #657

## Kontext

Gate SPEC 6.2 (`n ≥ 30 ∧ Wilson LB > 0,50`) měří jen **spolehlivost směru**, ne jeho
**velikost**. U obřích bucketů LB těsně nad 0,5 projde i zkreslení o zlomek procentního bodu
s nulovým výnosem. 23. 9. 2026 prošel NQ bucket OTHER/imp 1/none/RiskOff (n 13 464,
LB 0,5004, Ø −0,03 bp) a každá obecná negativní zpráva během dne založila signál:
81 z 86 signálů NQ od 14. 9. vzniklo v jediném dni. Po nočním přepočtu (LB 0,4997) signály
ustaly. Signály v ostatních dnech (GEOPOLITICS imp 3, Ø desítky bp) byly v pořádku.

## Rozhodnutí

Gate = `n ≥ 30 ∧ Wilson LB > 0,50 ∧ |ret_mean_bp| ≥ 1,0` na primárním okně.
**Jeden zdroj pravdy (#1267):** prahy a čistá funkce `gate_open()` jsou v
`gexlens_engine/compute/signal_gate.py`. `ModelStatsJob` výsledek zapíše do sloupce
`news_model_stats.gate_open` (tabulka je plně odvozená; migrace `ADD COLUMN`, přepočet běží
i při startu news-engine). Výběr režimového bucketu v `SignalJob`, drift hlídka (#403)
i zvýraznění/progres ve Stats čtou sloupec, pravidlo znovu nepočítají. `/news/stats` vrací
prahy (`gate`) jen pro text a progres; frontend nemá vlastní kopii.

Práh 1 bp ≈ 2× round-trip náklad ES (tick 0,25 b ≈ 0,37 bp + poplatek), u NQ víc než 5×.
Znaménko Ø dál určuje směr (SPEC 6.3), práh je symetrický.

## Zvažované varianty

- **Drift-adjusted brána (#657 bod 1)** — LB > base rate směru. Tuto vadu neřeší: short
  base rate je pod 0,5, takže by bránu pro short spíš otevřela. Zůstává samostatně v #657.
- **Práh + hystereze** — stabilnější na hraně, ale druhý parametr. Odloženo, dokud se
  kmitání neukáže i s prahem efektu.

## Důsledky

- Změna brány = nová mechanika signálů → vzorek H1 (#1264) začíná nasazením této změny.
- Buckety s |Ø| < 1 bp, které dnes gate procházejí (např. ES all MACRO_GROWTH/2/neg_small
  −0,95 bp, NQ gamma_negative FED/1 −0,22 bp), přestanou dávat signál a vypadnou z drift hlídky.
