# Walk-forward parametrů setupů — kontrola reportů (#1081)

> E-0.8 Fáze 0 (#1387) epiky #1385 · #794 fáze 3 · noční reporty `data/reports/walkforward-<datum>.md`
> (úloha „GEXLens walk-forward", `scripts/walkforward-nightly.ps1` → `scripts/walkforward_setups.py`)
> 9. 9. a 24. 9.–6. 10. 2026 · protokol ADR-0034 (IS 20 / OOS 5 seancí, metrika Sharpe, 6 kandidátů
> z `configs/walkforward_grid.json`, Bonferroni α = 0,05 / 6 = 0,0083, ≥ 20 OOS seancí, vítěz
> ≥ 50 % foldů, Ø Δ ≥ 0,10 R/den) · vstup dávkového bumpu v6 E-1.14b (#1403).

## Verdikt

- **Návrh verze parametrů: ne.** Žádný report zábrany ADR-0034 nesplnil; parameter store zůstává
  na verzi 1 a z walk-forwardu nejde do bumpu v6 nic.
- **Poslední platný report (6. 10.; mechanika v5, baseline store v1, 37 OOS seancí v 8 foldech):**

  | blok | Sharpe OOS strategie / baseline | Ø Δ R/den | t | p | vítěz foldů | verdikt |
  |---|---|---|---|---|---|---|
  | ES | 1,75 / 1,38 | +0,208 | 0,51 | 0,61 | max_rr_4 75 % | rozdíl nevýznamný |
  | NQ | 0,26 / 0,93 | −0,305 | −0,62 | 0,54 | acceptance_3 38 % | volba nestabilní |
  | portfolio ES+NQ | 2,23 / 1,67 | +0,350 | 0,59 | 0,56 | max_rr_4 38 % | volba nestabilní |

- **Řada 10 reportů 24. 9.–6. 10. je stabilní:** ES Ø Δ +0,07 až +0,21 R/den, p 0,61–0,87;
  NQ vždy záporné (−0,12 až −0,40 R/den — výběr podle in-sample na NQ škodí); portfolio
  +0,32 až +0,40 R/den, p 0,54–0,68, vítěz nejvýš ve 43 % foldů. p nikde nepřiblížilo práh 0,0083
  (t ≈ 0,5 proti potřebným ≈ 2,6).
- **ES × `max_rr_4` (cíl 4× risk místo 3×)** se volí konzistentně (50 → 75 % foldů), ale jeho
  OOS zisk je v šumu. Kandidát zůstává v gridu; nic se neuvolňuje (#1081: poctivý záporný výstup
  je legitimní, prahy se kvůli němu nemění).

## Platnost měření — tři vady, kvůli kterým čísla walk-forwardu zatím neměří živou mechaniku

1. **Replay přehrává i minuty, kdy expirace byla jen sekundárním řetězem.** `build_minutes` bere
   všechny `levels` expirace, jenže sekundární runtime (`instruments.py:335`, kadence
   `next_expiry_sweep_every` = 3 min) je píše už den předem; živý `SetupEngine` běží jen nad aktivním
   řetězem (`instruments.py:1108`). Za posledních 25 expirací (baseline defaulty, bez Max Pain)
   vzniklo v těchto minutách **120 z 312 setupů replaye ES (38 %) a 170 z 388 NQ (44 %)**;
   u NQ tvoří −26,2 R z celkových +4,1 R. Stejně tak poslední expirace každého nočního běhu
   (report ve 23:30 SELČ, před jejím settle) je jen den sekundárních minut. Oprava: #1464.
2. **CumΔ v replayi je midpoint** (`derived/{sym}/flow`, sloupec `cum_delta`), který jde proti ceně
   (#1018). S bumpem v6 se vstup mění (1A, #1459/#1460) — nová epocha vstupů (ADR-0034 §6),
   walk-forward pod v6 se měří od začátku.
3. **Výpadky nočního běhu:** 10.–23. 9. neběžel (Task Scheduler ukazoval na zmizelou cestu `pwsh`,
   opraveno 24. 9.); **7. 10. spadl** `TypeError: replay() missing … 'symbol'` — #1366 (PR #1382)
   přidal do `replay` povinný `symbol` a volajícího ve walk-forwardu minul (`scripts/` mypy nehlídá).
   Opraveno v tomto PR s regresním testem (`test_walkforward_predava_symbol_do_replaye`). Výpočet
   o nic nepřišel — každý běh počítá celou historii.

Na verdikt „ne" vady nemají vliv (opravené měření by mohlo návrh jen přinést, ne vzít), ale
**případný „NÁVRH" se do opravy #1464 neschvaluje** — byl by změřený na populaci setupů, které živý
engine nedetekuje.

## Další krok

- Noční běh pokračuje (od 8. 10. s opravou); report se čte jen kvůli řádku „NÁVRH".
- #1464 (okno replaye = doba aktivního řetězu, jen seance po settle) před prvním walk-forwardem
  pod v6; #1459 zahrne replay do inventury spotřebitelů midpoint znaménka.
- Kdy bude walk-forward pod v6 vypovídat, rozhodne #1459: bude-li replay číst tiskovou CumΔ
  (`derived/{sym}/cumdelta_dx`, od 27. 8. 2026), poběží nad archivem od 27. 8. hned po bumpu
  (~29. 10.–1. 11.); jinak až po 20 IS + 20 OOS seancích pod v6, tedy nejdřív koncem prosince 2026.
