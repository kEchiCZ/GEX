# ADR-0058: Politika rozlišení sběru extended expirací — minutově do 5 dnů, dál po 5 minutách

- **Stav:** přijato 1. 11. 2026. Vlastník v chatu 1. 11. ~08:50 SEČ (07:50 UTC) zvolil „A“
  k protinávrhu z 31. 10. („0-5dte by byly všechny data, 6-30dte by se ukládalo po 5 minutách“).
  Zapsáno v Rozhodnuto 1. 11. v epice #1385.
- **Datum:** 2026-11-01
- **Mění:** rozhodnutí #616 fáze 4a z 22. 8. 2026 (extended expirace do 7 dnů každou minutu).
  ADR-0029 (věčný archiv učicích dat) se nemění: nic se nemaže ani nepřevzorkovává.
- **Implementace:** #1338 část D-b (výchozí `tasty_extended_near_days` 7 → 5).
- **Podklad:** rozbor #1338 z 30. 9. (issuecomment-5925345027), měření 25.–30. 10.
  (issuecomment-6548326647), skript `scripts/measure_extended_near_days_1338.py`.

## Kontext

Extended expirace (#616 fáze 4a) jsou expirace ES a NQ mimo množinu IBKR do horizontu 30 dnů.
Engine je skládá z kotací tastytrade, greeks bere z dxFeed, jen když jsou čerstvé, jinak je
dopočítá BS modelem z mid kotace, a `volume` nechává prázdné,
protože toky a CumΔ jsou výhradně z IBKR. Zapisují se do stejných partic
`snapshots/{sym}/{expiry}/{den}.parquet` jako IBKR expirace.

Kadenci zápisu určuje `tasty/extended.cadence_due`: expirace nejvýš `near_days` kalendářních dnů
od dnešního data UTC se zapisuje každou minutu, vzdálenější jen v minutách dělitelných
`far_interval_min` (5). Od 22. 8. platila hranice 7 dnů.

Snapshots od #616 rostou zhruba 10× rychleji, než počítá ADR-0029 (~6 GB/rok). Měření partic
za ne 25. až pá 30. 10. 2026 (5 seancí, jen čtení):

| | MB za seanci | podíl |
|---|---|---|
| snapshots celkem | 231 | 100 % |
| IBKR expirace ES a NQ (jediné s `volume`) | 19 | 8 % |
| extended DTE 0–5 | 123 | 53 % |
| extended DTE 6–7 (dosud každou minutu) | 56 | 24 % |
| extended DTE 8–30 (už po 5 min) | 33 | 14 % |

Odhad pro jinou hranici: řádky extended expirací nad hranicí a do 7 dnů se omezí na minuty
dělitelné 5, bajty úměrně řádkům.

| hranice `near_days` | MB za seanci | úspora | rok (250 seancí) |
|---|---|---|---|
| 7 (do 1. 11.) | 231 | — | ~58 GB |
| **5 (toto ADR)** | **186** | **−19 %** | **~47 GB** |
| 4 | 164 | −29 % | ~41 GB |
| 3 (původní doporučení rozboru) | 140 | −39 % | ~35 GB |

## Rozhodnutí

1. **Hranice minutového zápisu je 5 kalendářních dnů.** Výchozí hodnota v kódu
   (`config.py`, `tasty_extended_near_days` = 5, env `GEXLENS_TASTY_EXTENDED_NEAR_DAYS`);
   produkční `.env` klíč nenese. Dny se počítají jako expirace − dnešní datum UTC, ne obchodní
   dny: v pátek pokrývá minutový zápis expirace do středy, v pondělí do pátku.
2. **Expirace 6–30 dnů se zapisují jen v minutách dělitelných 5.** Interval
   (`GEXLENS_TASTY_EXTENDED_FAR_INTERVAL_MIN` = 5) i horizont 30 dnů se nemění.
3. **Mění se jen kadence zápisu.** Subskripce extended expirací (pásmo kolem ceny), široký OI
   archiv (#828), rozpočet subskripce (ADR-0027) a IBKR expirace zůstávají beze změny. IBKR
   expirace a všechny minuty s `volume` se zapisují každou minutu jako dosud.
4. **Platí jen dopředu.** Historie se nemaže ani nepřevzorkovává (ADR-0029). Dny před nasazením
   mají DTE 6–7 po minutách, od nasazení po 5 min. Když se expirace přiblíží na 5 dnů, přejde
   sama na každou minutu. Minuty zapsané po 5 min se zpětně nedoplní, minulé kotace řetězu
   nejde stáhnout.
5. **Hranice 3 dny se zamítá.** Ušetřila by −39 %, ale vzala by minuty DTE 4–5. Bezztrátová
   komprese C1 z #1338 přijde samostatně po ověření mechaniky v6 a předtím se přeměří. Podle
   rozboru 30. 9. (−26 %) by s ní hranice 5 dala ≈ 138 MB za seanci.

## Kdo čte `data/snapshots` (ověřeno v kódu 1. 11. 2026)

| Čtenář | Co čte | Dopad hranice 5 |
|---|---|---|
| API `/snapshots/{symbol}/{expiry}` a `/replay/{symbol}/{expiry}/{date}` (heatmapa, přehrávání, Daily) | všechny minuty vybrané expirace | DTE 6–7 má hodnotu jen každých 5 min, jako dosud DTE 8–30 |
| API `/profile/{symbol}/{expiry}` (profil k okamžiku nebo za okno) | poslední minutu ≤ `ts`, objem okna jen z `volume` | u DTE 6–30 snímek nejvýš 4 min starý |
| API `/profile/{symbol}/aggregate` (Σ profil) a `/chain/{symbol}/{expiry}` | poslední minutu dne | u DTE 6–30 nejvýš o 4 min dřívější |
| API `/flow` (Opt Vol, `_opt_vol_series`) | jen řádky s `volume` (IBKR) | žádný |
| engine `emrespect.straddle_reference` (EM ze straddlu) | 0DTE expiraci | žádný (DTE 0) |
| engine `storage/fa_validation`, `storage/fa_calibration` (FA validace, kalibrace α a ρ) | `volume` stran, tasty strana bez objemu = „nevíme“ | žádný |
| engine `storage/cumdelta_compare` | deltu v okně aktivního řetězu (IBKR) | žádný |
| engine `storage/retention` | jen cesty; snapshots chrání ADR-0029 | žádný |
| news-engine `crowd` (PCR) | nejbližší expiraci s dnešní particí, poslední minutu, `volume` | žádný |
| `scripts/backtest_setups.py`, `walkforward_setups.py`, `compare_mechanics_v5_v6.py`, `replay_parity.py`, `measure_replay_bar_1641.py` | opční toky z `volume` | žádný (vstup bitově shodný, rozbor 30. 9.) |
| `scripts/recompute_fa.py`, `recompute_fa_rho.py`, `measure_fa_ratio_1612.py`, `measure_structured_volume.py` | `volume` | žádný |
| `scripts/measure_gamma_edge_breaks.py` (#601) | nejbližší široký řetěz ≥ den (2 DTE a víc) | žádný, pokud řetěz 2–5 dnů existuje |
| `scripts/measure_nq_coverage.py`, `measure_hard_data_1381.py`, `fix_snapshot_duplicates.py` | jednorázová měření a oprava | počty řádků DTE 6–7 od 1. 11. nesrovnatelné s dřívějšími |

Učení, backtest, walk-forward, FA, EM a PCR čtou jen IBKR expirace nebo `volume`. Minutová data
extended expirací čte jen heatmapa a profil vybrané expirace.

## Důsledky

- Tempo snapshotů klesne z ~231 na ~186 MB za seanci (~47 GB/rok). Varování o volném místě
  (#773) přijde později, ale kapacitu D: to samo neřeší; archiv mimo D: (#1338 E) se rozhoduje
  do 15. 12. 2026.
- Heatmapa a profil expirace 6–7 dnů mají od nasazení pětiminutové rozlišení. Kdo potřebuje
  minutová data vzdálenější expirace pro výzkum, dostane je až od 5. dne před expirací.
- Ubude každominutových přepisů partic DTE 6–7, a tím i práce `SnapshotWriter` na konci seance.
- Ověření: partice extended expirací 6–30 dnů mají po nasazení řádky jen v minutách dělitelných 5,
  do 5 dnů každou minutu, IBKR expirace beze změny. Tempo za první celý týden se přeměří
  skriptem `scripts/measure_extended_near_days_1338.py` proti 231 MB za seanci (cíl ≈ 186 MB).
