# ADR-0034: Walk-forward protokol optimalizace parametrů setupů (#794 fáze 3)

**Stav:** přijato (2026-09-09; navazuje na ADR-0030 metriku, ADR-0033
parameter store a rozhodnutí uživatele v #794: autonomie stupeň 1)

## Kontext

Fáze 0–2 epicu #794 daly metriku (denní ΣR, Sharpe), validovaný replay
(parita 99,6–100 %) a verzované parametry. Fáze 3 má nad tím postavit
smyčku, která parametry **navrhuje** — a analýza #794 (sekce 2c, 2d) říká,
že bez kázně out-of-sample a bez zábran proti overfittingu smyčka na malém
vzorku spolehlivě najde šum a škodí. Tohle ADR fixuje protokol, aby se dal
měnit jen vědomě.

## Rozhodnutí

### 1. Prostor kandidátů je malý a předem deklarovaný

`configs/walkforward_grid.json`: pojmenovaní kandidáti = platná verze store
s přepsanými klíči, každý s důvodem (`why`). Přidání kandidáta je změna
v PR, ne ad hoc. Počet kandidátů vstupuje do Bonferroniho korekce (bod 4).
Start: 6 kandidátů z hypotéz kalibrací #394/#434 a R-mechaniky #302.

### 2. Vstup = replay produkčním detektorem, výstup = denní ΣR

`scripts/backtest_setups.py` (`build_minutes` + `replay`) nad archivem
(snapshots/, derived/ mimo retenci, ADR-0029), stejná mechanika jako živě
(`mechanics_version` aktuální). Seance = expirace řetězu (0DTE). Bloky:
per symbol a **portfolio** (ES+NQ sečtené per seance, ADR-0030).

### 3. Walk-forward

In-sample okno **20 seancí** vybere kandidáta s nejlepší metrikou
(**Sharpe**, ADR-0030; při nulovém rozptylu Σ R), hodnotí se na dalších
**5 seancích** out-of-sample, okno se posune o 5. OOS řada „strategie" je
slepenec voleb foldů. **Při shodě metriky vyhrává baseline** — churn
parametrů láme srovnatelnost a je sám náklad. In-sample výkon se nereportuje
jako výsledek.

### 4. Zábrany proti overfittingu — návrh vzniká jen při splnění všech

| Zábrana | Hodnota | Proč |
|---|---|---|
| minimální počet OOS seancí | 20 | SE anualizovaného Sharpe ≈ √252/√N; pod 20 je rozdíl SR 2 vs. 0 nerozlišitelný |
| stabilita volby | kandidát vyhrál ≥ 50 % foldů | kandidát, který vyhrává jen občas, je šum okna |
| minimální zlepšení | Ø Δ ≥ 0,1 R/den vs. baseline (OOS) | pod tím se parametry nemění, i kdyby byl rozdíl „významný" |
| významnost | párový test denních OOS rozdílů, p ≤ 0,05 / počet alternativ (Bonferroni) | korekce na mnohonásobné testování nad celým prostorem |

Nulový rozptyl rozdílů s kladným průměrem = jistý rozdíl; normální
aproximace p-hodnoty (n ≥ 20 zaručuje bod 1 tabulky).

### 5. Výstup je návrh, ne zápis (autonomie stupeň 1)

Report (markdown, volitelně JSON) do `data/reports/walkforward-<datum>.md`:
souhrn per blok, folds, podíly voleb, verdikt. Splní-li kandidát zábrany,
report nese tělo pro `POST /setups/params` (`note` = walk-forward + verdikt)
— **odeslat ho může jen člověk**. Stupeň 2 (auto-promotion v mezích) přijde
až s champion–challenger stínovým detektorem (fáze 4) a meta-track-recordem
smyčky (fáze 5).

### 6. Epochy vstupů

Změna zdroje CumΔ (#615/#1018) nebo rozšíření řetězu (#616) mění vstupy;
walk-forward nesmí učit přes takovou hranici. Do zavedení „epochy" ve store
platí: po takové změně se report čte jen od data změny (`--from` v příštím
kroku) a kandidáti závislí na CumΔ se přeměří.

## Důsledky

- Noční běh: `scripts/walkforward-nightly.ps1` (Task Scheduler nebo ručně)
  → report v `data/reports/`; nic v DB se nemění.
- Golden test protokolu: `engine/tests/golden/walkforward_794.json`
  (ručně spočtené folds, shoda → baseline, žádný návrh při Ø Δ 0).
- Poctivý záporný výstup je legitimní: „žádný kandidát nesplnil zábrany" je
  očekávaný stav v prvních týdnech (dnešní baseline má záporný Sharpe,
  #794 sekce 5) — smyčka neumí vyrobit edge, který v datech není.

## Souvisí

#794 (epic), ADR-0030 (metrika), ADR-0033 (parameter store), #394/#434
(ruční kalibrace = prototyp), #302 (R-mechanika), ADR-0029 (učicí data).
