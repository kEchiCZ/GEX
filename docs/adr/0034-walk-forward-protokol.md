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
(`mechanics_version` aktuální). Seance = expirace řetězu (0DTE) v okně
aktivního řetězu, viz doplněk 22. 10. 2026. Bloky: per symbol a **portfolio**
(ES+NQ sečtené per seance, ADR-0030).

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

*Výjimka ADR-0057 bod 7 (16. 10. 2026):* pro přepnutí CumΔ na tisky (#1018) se
historie od 21. 9. 2026 do nasazení v6 bere ze stínové řady `cumdelta_dx` — je
to tatáž mechanika jako 1A (univerzum aktivního řetězu, strana od burzy, delta
z tasty greeks). Platí s branou parity stín ↔ živá řada ≥ 5 seancí po bumpu;
když neprojde, walk-forward v6 učí jen nad živou řadou.

**Doplněk 11. 9. 2026 (#1112 bod A) — změny mechaniky se dávkují.** Oddělená
„epocha vstupů" vedle `mechanics_version` se **nezavádí**: CumΔ vstupuje do
všech šablon (potvrzení odrazu T1, divergence T2/T7, kvantilová brána T4 — *oprava
16. 10. podle inventury ADR-0057: T2 čte CumΔ jen v kontra-režimu, T7 vůbec; čte ho i T5*),
takže by šlo jen o další verzi mechaniky pod jiným jménem. Místo toho platí
pravidlo o pořadí:

1. Každé zvednutí `SETUP_MECHANICS_VERSION` (změna sémantiky stopů/cílů,
   filtru šablony **nebo zdroje vstupní řady**) nuluje vzorek pro confidence
   (ADR-0033 2B, n ≥ 30), pro stínové brány (#1064, ≥ 100 v nejmenší skupině)
   i pro walk-forward (≥ 20 OOS seancí). Ty vzorky potřebují 4–6 týdnů; při
   kadenci v1–v5 (5 verzí za 7 týdnů) se nenaplní nikdy.
2. Proto se změny mechaniky **hromadí do jednoho bumpu v okně mezi
   měřeními**: verdikt měření (např. #1018) se vysloví v termínu, ale nasazení
   počká, až doběhnou rozběhnutá okna (#1064, #1081), a nasadí se společně
   s dalšími odsouhlasenými změnami (např. zapnutí brány z #1064).
3. Výjimka je jen oprava chyby, která data znehodnocuje (precedens v5, #859 —
   Max Pain zamrzlý na ranní hodnotě): ta se nasazuje hned a rozběhnutá
   měření se explicitně restartují s poznámkou v jejich issue.

**Doplněk 30. 10. 2026 (#1402) — bump v6 nese bránu nad čistým R.** Do dávkového bumpu v6
(#1403) jde i konvence nákladů z dodatku ADR-0030 2026-10-30: od v6 stojí brána šablon,
n potřebné a ØR čistě v Knihovně na čistém R po nákladech mikra (poplatek mikra z parametrů
+ skluz 1 tick na stranu), do v5 na hrubém R. Přepínač je `NET_R_GATE` =
`SETUP_MECHANICS_VERSION >= 6` vedle `CUMDELTA_PRINTS`, kontext setupu nese
`risk_rules_version` 4. Vzorek brány se čte jen z aktuální mechaniky
(`SetupEngine._load_realized` → `realized_since(mechanics_version=…)`), takže po bumpu
začíná každá buňka od n = 0. Dokud nenasbírá 30 uzavřených setupů v6, je verdikt
`insufficient` a setupy buňky jsou stín, pokud ji nepustí Zkouška; zkouška zahájená na v5
na nové mechanice končí (ADR-0038 dodatek 2026-10-01). Podle bodu 1 bump nuluje i vzorky
confidence a walk-forwardu.

**Doplněk 22. 10. 2026 (#1464, nález #1081) — seance = okno aktivního řetězu.**
Adresář expirace E nese i minuty, kdy E psal sekundární runtime (levels jednou
za 3 min), ale živý `SetupEngine` běží jen nad aktivním řetězem. Replay je
dosud přehrával: v řezu 25 expirací do 7. 10. vzniklo mimo okno 36 % setupů ES
(112 z 315, ΣR +15,7) a 44 % NQ (180 z 411, ΣR −20,8). Noční report zároveň
počítal neuzavřenou expiraci jako OOS den. Platí (rozhodnutí A v #1385):

1. **Seance E = minuty v [settle předchozí expirace, settle E)** —
   `settle.history_expiry_settle` (`expiry_end`), předchozí expirace =
   předchozí obchodní den podle `is_trading_session` (pondělí bere páteční
   večer a neděli, po svátku den před ním). Je to pravidlo živého rollu od
   #1331; minuta settle E patří už další expiraci.
2. **Okna před nasazením #1331 (7. 10. 2026) začínají aktivní kadencí** —
   první minutou levels, po které jdou další bez mezery (`live_start`, tři
   minuty po sobě). Živý řetěz tehdy roloval až s novým dnem UTC nebo
   restartem; večer po settle (typicky 20:00–00:00 UTC) psal E jen sekundár
   a živě nad ní žádný setup nevznikl. Replay by je skládal z každé třetí
   minuty: v řezu 25 expirací ES 5 setupů (2 %, ΣR −5,0), NQ 10 (4 %,
   ΣR −2,0) ve 23 večerech.
3. **Expirace, jejíž settle proti okamžiku běhu (UTC, ne datum) ještě
   nenastal, se vynechá** — rozhoduje okamžik, ne místní datum, takže platí
   i v týdnech posunu času (26. 10.–1. 11. je settle dál 20:00 UTC = 21:00
   SEČ, od 2. 11. 21:00 UTC). Report dne D proto končí OOS dnem D jen po
   settle D a fold z reportu D má v reportu D+1 tutéž OOS ΣR.
4. Hlavička reportu uvádí per symbol počet vynechaných minut a expirace bez
   settle; JSON nese `run_at` a `skipped`.

Zdroj CumΔ (`build_cum_series` / `attach_cum_delta`, #1590) a opční toky se
oknem nemění — je to jen filtr minut. Po opravě je v témž řezu mimo okno
i ve večerech před #1331 0 setupů (ES 224, ΣR +23,9; NQ 246, ΣR +44,8).
Walk-forward 22. 10. má 46 místo 49 OOS seancí: chybí 22. a 23. 10. (bez
settle) a 17. 9. — expirace 20260917 živě aktivní nebyla (pipeline z 20260916
rolovala rovnou na 20260918), seance je proto mezera, ne minuty 16. 9.
pod cizím datem.

**Doplněk 25. 10. 2026 (#1641, #1642) — hranice okna podle kódu rollu a bar
cesty s vlastním razítkem.** Bod 1 doplňku 22. 10. („minuta settle E patří už
další expiraci“) živému rollu neodpovídá. Roll v `__main__` proběhne až po
cyklu minuty settle: ten je posledním cyklem vypršelé pipeline a nová
pipeline (s vlastním `SetupEngine`) vzniká nejdřív v minutě po něm. Na
produkci začínají levels E v den předchozího settle ve 20:01 a v den settle E
končí minutou 20:00 (ES i NQ 20.–23. 10.). Platí proto:

1. **Seance E = rozhodovací minuty (settle předchozí expirace, settle E]**
   (`active_window`). Minuta settle E nese bar končící v settle (19:59 v
   letním čase USA). Nový setup v ní nevznikne (`born_after_settle`), otevřený
   skončí timeoutem za close tohoto baru a `closed` = settle, shodně se živým
   enginem (`test_engine_timeout_za_close_v_settle_ne_za_cenu_cyklu`).
2. **Cesta ceny hodnotí bar rozhodovací minuty N s jeho razítkem N − 1**
   (`path_bar`, totéž dělal replay sond). Dřív šel bar s razítkem N: bar 19:59
   se nehodnotil vůbec, timeout bral close baru 19:58 a zásah úrovně měl
   `closed` o minutu později než živě.
3. **Replay sond T9 (`--probes`) bere totéž okno.** Dřív počítal začátek
   z kalendářního dne před expirací, takže pondělní expirace přišla o páteční
   hodinu po settle a expirace po svátku začínala až večer svátku.

Dopad na replay (měření 25. 10., výchozí `SetupParams`, mechanika v5,
posledních 25 uzavřených expirací 21. 9.–23. 10. 2026, před = skript
z `main`, po = oprava; rozhodnutí v #1385;
`scripts/measure_replay_bar_1641.py --before 82bcefc --to 20261023 --db …`):

| | setupy | ΣR | timeout | bez konce |
|---|---|---|---|---|
| ES | 298 → 299 | +18,7 → +24,2 | 20 → 21 | 3 → 1 |
| NQ | 300 → 305 | +48,4 → +53,5 | 19 → 19 | 0 → 0 |

- Timeout bere close jiného baru: R se změnilo u 20 setupů ES a 19 NQ.
- `closed` o minutu dřív posune konec cooldownu a blokace směru
  (`direction_block_minutes`, `max_stops_per_direction`). Vznik dalšího
  setupu se posune o 1–8 minut, nebo setup přibude či zmizí: ES 18 setupů jen
  před a 19 jen po, NQ 4 a 9; kromě jednoho jde o `trend_continuation`.
  Největší kaskáda je NQ 19. 10.: čtvrtý long stop skončil ve 13:30 místo
  13:31, blokace 90 min skončila v 15:00 a long z 15:00 vyšel (+1,46 R). Výhra
  smazala počítadlo stopů, takže večer vznikly další 4 setupy, které dřív
  blokace zakázala; den dává +3,35 R z rozdílu +5,1 R.
- Minuta 19:59: setup ES 28. 9. dřív zůstal bez konce, teď končí timeoutem za
  close baru 19:59 (−0,11 R). Setup ES 29. 9. z 19:59 nevznikne, protože
  minuta 19:59 hodnotí bar 19:58.
- Zbylý setup bez konce (ES 9. 10., z 8:00) leží v expiraci, jejíž minuty
  končí ve 14:18; je to mezera v datech, oprava ji nemění.

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
