# ADR-0019: Definice hloubky sentiment vlny

**Stav:** navrženo (PR s labelem `needs-decision`)
**Datum:** 2026-07-29
**Issue:** #292 (SPEC 5.6)

## Kontext

SPEC 5.6 pinnuje stavová pravidla (RiskOn ⇔ close > MA5 > MA10 ∧ hloubka
vlny ≥ potvrzovací práh; práh = průměrná hloubka historických vln opačného
směru), ale **nedefinuje, co přesně je „hloubka vlny"**. Bez pinnuté
definice nejdou napsat golden testy a adaptivní práh by nebyl
reprodukovatelný.

## Rozhodnutí

**Hloubka vlny = max |close − MA10| přes dny vlny.**

Proč tato definice:

- Měří, **jak daleko se režim natáhl od dlouhodobého průměru** — přesně to,
  co má potvrzovací práh srovnávat („je tahle vlna aspoň tak výrazná jako
  bývaly ty protisměrné?").
- Jednotka = body SentIndexu, stejná škála jako práh → přímé porovnání.
- Je monotónní v průběhu vlny (max se jen zvětšuje) — stav se během vlny
  nemůže „odpotvrdit" kvůli poklesu hloubky, jen kvůli pádu MA podmínky.
  To drží hysterezi a brání blikání stavu.

Zvažované alternativy: amplituda close (max−min během vlny) — měří rozkmit,
ne vzdálenost od průměru, a u plochých vln u vrcholu klame; kumulativní
plocha nad MA10 — směšuje hloubku s délkou (délka je ve schématu zvlášť).

Doplňující upřesnění pinnutých pravidel (obojí v `sentwaves.py`, golden
testy v `engine/tests/test_sentwaves.py`):

- Nerovnosti podmínky jsou **ostré** (rovnost nestačí).
- Práh se počítá **walk-forward**: jen z vln opačného směru DOKONČENÝCH před
  začátkem aktuální vlny — budoucí vlny nesmí kalibrovat minulý stav
  (SPEC 5.6 kalibrační split; totéž pak platí pro track record #298).
- Přechod směru bez neutrálního dne mezi vlnami je povolený (vlna se uzavře
  posledním dnem své podmínky).

## Důsledky

- Implementace je sdílená v `gexlens_engine.compute.sentwaves` — news-engine
  (WavesJob ukládá vlny + publikuje stav) i API (`/sentiment/state`) počítají
  toutéž funkcí; žádný dvojí výklad.
- `sentiment_waves` se plní full-replace přepočtem z denních close —
  reprodukovatelné z čisté řady, bez inkrementální údržby.
- Historie zatím má 2 denní close (index běží od 28. 7.) — stav bude Neutral,
  dokud se nenaplní MA10 okno (~10 obchodních dní). Případný backfill denních
  close z historicky skórovaných eventů (#277 dataset) je samostatná úvaha.
