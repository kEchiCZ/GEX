# ADR-0044: Očekávaný pohyb před ohlášeným releasem a registr předem stanovených hypotéz

- Stav: **přijato** (26. 9. 2026). Směr schválil uživatel 26. 9. (fáze 3–4 epicu #1296) a téhož
  dne potvrdil všechny body k rozhodnutí v doporučených variantách: 1A, 2A (5 hlavních řad + Retail
  Sales a ISM Services se štítkem), 3 kontrolní body 10/20/30, 4 H3 až po ověření, 5 REGISTERED_AT
  1. 10. 2026, 6 škálování denním rozsahem, 7 automatický backfill
- Souvisí: #1296 (epic), ADR-0043 (news_anomaly, předobchodní upozornění, stav etap před
  publikací), ADR-0028 (volatilitní režim z denního rozsahu), ADR-0023 (seance), ADR-0034
  (walk-forward), #794, #453, #1284 (přepínače Telegramu), #1290 (proklik do grafu), #1298–#1301
  (datové chyby nalezené výzkumem)

## Kontext

Výzkum fází 1–2 (`scripts/build_release_reactions.py`, `scripts/measure_release_predictability.py`)
zpracoval 955 USD releasů (FF High/Medium) × ES a NQ od 30. 7. 2024 do 25. 9. 2026. Walk-forward:
vzorek (IS) do 6. 1. 2026, mimo vzorek (OOS) po něm, korekce Benjamini-Hochberg.

- **Velikost pohybu je robustní.** Výchylka za 5–15 min je nad mediánem stejné minuty běžného dne
  u CPI 23/23, NFP 24/24, FOMC 18/18, PPI 22–23/23 a PCE 21–22/23 a drží i v OOS.
- **Pořadí velikosti řad** drží z IS do OOS (ρ 0,88–0,92), absolutní bp se ale mezi obdobími
  posouvají — velikost je nutné přepočítat na dnešní volatilitu.
- **Směr bez signálu** všude kromě jádra inflace na 5–15 min, a i tam jen kandidát (IS 7/7,
  OOS 6/7 → dohromady 13/14). Opačný směr (chladnější → růst) v OOS nedrží. Před releasem
  nic nepředpovídá směr (0 testů po BH).
- **Metodická korekce `verdicts()`** (26. 9., před fází 3, ne ladění kritérií): první verze
  rozhodovala podle q z celého vzorku, který obsahuje OOS — OOS se použil k objevu i k ověření.
  Objev teď stojí jen na IS (BH přes IS p-hodnoty rodiny), OOS jen jednostranně ověřuje ve směru
  IS. Přegenerovaný report: z „prošlo“ vypadl jediný signál (Core PPI → NQ 3 seance, který už
  adversariální kontrola vyvrátila), 4 kandidáti jádra inflace (5/15 min) zůstali; u velikosti
  ES 15 min vypadly Retail Sales a Core Retail Sales (18 → 16 prošlých jednotek).

## Rozhodnutí

1. **Upozornění před releasem 60 a 15 min předem, ES a NQ zvlášť** (`kind: release_preview`,
   kanál `alerts`, zvoneček s proklikem na zprávy releasu, Telegram, přepínač „Očekávaný pohyb
   před releasem“ ve skupině Setupy a burza, výchozí zapnuto, bez výjimky z tichých hodin —
   releasy padají do 13:30–20:00 Praha).
   - Spouští ho FF kalendář: USD scheduled události, shluk = všechny s dopadem High/Medium
     (`raw.impact`, backfill `raw.impactName`) ve stejné minutě, **aspoň jedna High**.
   - Rodina podle headline shluku (`releases.SERIES_RANK`, pořadí převzaté z výzkumu).
   - Rodiny s upozorněním: **CPI, NFP, FOMC, PPI, PCE**, navíc **Retail Sales a ISM Services**
     se štítkem „slabší řada“. **Claims ne** (bod k rozhodnutí 2).
2. **Velikost = medián–p75 výchylky za 15 min** (max. high/low proti close minuty před
   releasem, `reactions.measure_excursion`) z minulých releasů rodiny, **přepočtená na dnešní
   volatilitu násobkem za každý release**: násobek = výchylka / `vol_ref`, `vol_ref` = medián
   denního rozsahu (high − low do settle, `gammacliff.session_ranges`, stejná míra jako ADR-0028)
   20 seancí před seancí releasu / close minuty před releasem. Očekávaná výchylka = medián a p75
   násobků × dnešní `vol_ref`. Kvantily lineární interpolací (`statistics.quantiles`, jako
   výzkum). Pod 8 releasy rodiny text říká „málo historie (n = …)“.

   Měření IS → OOS na 16 buňkách (8 rodin × ES/NQ, OOS n = 188), průměrná |log| chyba mediánu:

   | varianta | chyba | OOS ≤ p50 | OOS ≤ p75 |
   |---|---|---|---|
   | bez škálování | 0,185 | 56 % | 84 % |
   | doslovný „poměr aktuální vol. k vol. při releasech“ (medián bp × vol dnes / medián vol) | 0,205 | — | — |
   | **násobek × medián denního rozsahu 20 seancí (zvoleno)** | **0,117** | 56 % | 79 % |
   | násobek × rv20 (potřebuje back-adjust rollů, #1301) | 0,118 | — | — |
   | násobek × intradenní realizovaná volatilita | 0,139 | — | — |
   | násobek × výchylka ve stejnou denní dobu | 0,173 | — | — |

   Zvolená varianta se **odchyluje od doslovného znění schválení** („poměr aktuální realizované
   vol. k té při historických releasech“) — doslovná podoba vyšla hůř než žádné škálování
   (bod k rozhodnutí 6).
3. **Úrovně**: call zeď, put zeď, flip a těžiště z posledních `levels` 0DTE dne releasu
   (nejbližší expirace ≥ seance releasu) před okamžikem upozornění. Dělí se na „v dosahu“ (± p75
   v bodech od ceny) a „dál“. Úrovně starší než 10 min nesou čas.
4. **Směr**: jediný směrový řádek je **H1** u shluku s headline jádra inflace (Core CPI m/m,
   Core PPI m/m, Core PCE Price Index m/m): „H1 – OVĚŘUJE SE: jádro inflace teplejší než odhad
   → historicky za 15 min níž ve 13 ze 14 (živě k z n)“. Pravděpodobnost až po ověření, po
   zamítnutí řádek zmizí. **Opačný scénář (chladnější → výš) se neuvádí nikdy**, jiná směrová
   tvrzení jsou zakázaná (hlídá test nad texty všech rodin). Ostatní upozornění končí „Směr:
   bez prokazatelného efektu.“
5. **Časování**: vlastní smyčka news-enginu à 60 s (`release_preview_loop`): T−15 odejde mezi
   T−15:00 a T−14:00; v `reaction_loop` à 300 s + délka cyklu by chodila až o 5+ min později.
   Mimo okno releasu je tik jeden SELECT nad `ix_news_events_ts`. Stav etap = řádky
   `release_previews`, zapisují se **před publikací** (riziko je ztráta, ne duplicita, vzor
   ADR-0043). Po pozdním startu, kdy jsou splatné obě etapy, odejde jen T−15 a T−60 se uloží
   s `sent = false`.
6. **Učící smyčka**:
   - `release_moves` — naměřená fakta za shluk × instrument (rodina, headline, znaménko
     překvapení, `vol_ref`, výchylka 15 min, výnos 15 a 60 min, medián denní doby pro M1).
     Měří `ReleaseMovesJob` v `reaction_loop` po uplynutí 60 min + 2 min zápisu barů **a jen
     shluk, jehož headline má actual** — bez actual se release nekonal (fantom po přesunu,
     viz Důsledky) nebo ho FF ještě nevyplnil; po 24 h bez actual job jednou zaloguje varování.
     V historii mají actual všechny headliny (210 shluků rodin s upozorněním v kopii kalendáře
     k 26. 9.), pravidlo tedy nic skutečného nevyřadí. Živě 14 dní zpět; neúplné měření (bary
     dorazily pozdě) se zkouší nejvýš 1× za hodinu do 3 dní, oprava překvapení se propíše
     do 7 dní, pak se řádek nemění.
   - **Historie** = tatáž funkce přes CLI
     `python -m gexlens_news backfill-release-moves [--from YYYY-MM-DD] [--include-live]`:
     přeměří jen shluky **před `REGISTERED_AT`**; živé jen s `--include-live` (varování v logu)
     a i pak platí zmrazení níže. Prázdná tabulka se dopočítá sama od začátku archivu barů
     (28. 7. 2024) — zkouší se v každém cyklu, dokud je prázdná, takže selhaný první běh se
     zopakuje.
   - `release_hypotheses` — stav registru, přepisuje se po každé změně živého řádku, ale
     **vyhodnocený úsek je zmrazený**: releasy do posledního vyhodnoceného kontrolního bodu
     (u rozhodnuté hypotézy do rozhodnutí, u „pokračovat“ do toho bodu), jejich zásahy
     a stav se přebírají z předchozího řádku; přepočet jen přičítá releasy mimo úsek.
     Oprava dat nebo přeměření v úseku se nepropíše (WARNING v logu s počtem). Smazání
     řádků `release_hypotheses` by zmrazení ztratilo — tabulku nikdy nemazat ručně.
   - Registr hypotéz je v kódu (`gexlens_news/release_hypotheses.py`) a v tomto ADR, **ne v DB**
     — dodatečná změna kritérií by v DB šla udělat bez stopy.
   - Agregáty velikosti se **nepersistují**: medián a p75 z ~25 řádků se počítají při sestavení
     upozornění (bod k rozhodnutí 1).
7. **Datový model**: tři nové tabulky v `ensure_sentiment_schema` (`create_all`, jen aditivně,
   žádný ALTER existujících tabulek). **Před nasazením záloha PG** (`pwsh scripts/backup-postgres.ps1`).
8. **API a UI**: `GET /stats/releases/hypotheses` (jen čte tabulku a registr; bez řádku = „ověřuje
   se“ s n = 0) a sekce „Releasy — předem registrované hypotézy“ ve Statistikách.

## Registr hypotéz

**REGISTERED_AT = 2026-10-01 00:00 UTC.** Releasy před tímto okamžikem se živě nepočítají nikdy.
První živý kandidát je NFP 2. 10. 2026.

| ID | pravidlo | instrumenty | historicky (výzkum, popisně) | v upozornění | tempo, kdy n = 10 |
|---|---|---|---|---|---|
| H1 | headline Core CPI/PPI/PCE m/m s překvapením +1 (actual > forecast); zásah = výnos 15 min < 0 | ES, NQ | 13/14 a 13/14 | hned jako „ověřuje se“, pravděpodobnost po ověření | ~0,54/měsíc → ~3/2028 |
| H3 | rodina CPI bez ohledu na překvapení; zásah = výnos 60 min > 0 | jen ES | 21/24 | ne (bod k rozhodnutí 4) | 1/měsíc → ~7/2027 |
| M1 | rodiny CPI, NFP, FOMC, PPI, PCE; zásah = výchylka 15 min nad mediánem 15min výchylek ±30 min stejné denní doby (ET) za 20 předchozích seancí (≥ 200 vzorků) | ES, NQ | 107/111 a 107/111 | dovětek až po ověření, jen u rodin M1 (slabší řady ne) | ~4,3/měsíc → ~12/2026 |

Společné definice:

- **Jednotka** = shluk releasu (USD scheduled High/Medium ve stejné minutě), headline podle
  `SERIES_RANK`, rodina podle headline. ES a NQ mají každý vlastní stav.
- **Výnos h** = close posledního baru v [T, T+h) proti close minuty před T
  (`reactions.compute_reactions`); měřitelný jen s barem přesně v minutě před T a aspoň h − 1
  bary v okně.
- **Překvapení** = znaménko (actual − forecast) headline × polarita řady (sloupce `forecast`
  a `actual` v `news_events`, jako výzkum). Text upozornění ukazuje odhad tak, jak ho vypisuje FF
  (`raw.forecast`) — ten se může od čísla v DB lišit (NFP 4. 9.: „58K“ × 55000).
- **Nehodnotí se**: nulové nebo chybějící překvapení, nulový výnos, chybějící měření (zavřený
  trh, díra v barech), u M1 chybějící baseline denní doby, shluk bez actual headline (release
  se nekonal — neměří se vůbec).
- **Vstupy kritérií mají v registru vlastní kopie** (ne sdílené konstanty upozornění
  `releases.ROBUST_FAMILIES` ani news_anomaly `reactions.MIN_BASELINE_SESSIONS`, `TOD_*`):
  řady H1 (`H1_SERIES`), rodina H3, rodiny M1 (`M1_FAMILIES`), baseline M1 (20 seancí, ±30 min,
  ≥ 200 vzorků, medián). Ladění anomálií ani změna rodin s upozorněním je nezmění; test hlídá
  i to, že rodiny hypotéz job měří.

## Kritéria (pevná — po registraci se nemění)

- **Kontrolní body** n = 10, 20, 30 hodnocených živých releasů; rozhoduje se **jen v nich**, mezi
  nimi stav trvá („ověřuje se, další kontrola při n = …“).
- **Ověřeno**: dolní mez Wilsonova 95% intervalu (z = 1,96) > 50 % — tj. ≥ 9/10, ≥ 15/20, ≥ 21/30.
- **Zamítnuto**: horní mez < 50 % (≤ 1/10, ≤ 5/20, ≤ 9/30), nebo n = 30 bez ověření (futilita).
- **Rozhodnutí je konečné** a vynucené v kódu (zmrazený úsek, viz Rozhodnutí 6); další releasy
  se jen přičítají k zobrazenému k/n. Zmrazený je i výsledek kontrolního bodu bez rozhodnutí
  („pokračovat“) — pozdější oprava dat nesmí zpětně „ověřit“ bod, který už proběhl. Revize =
  nová hypotéza s novým ID a datem registrace. Test `test_strazce_registru` drží konstanty:
  datum registrace, kontrolní body, z = 1,96, hranici 50 %, vstupy kritérií, horizonty (výchylka
  15 min, výnos 15 a 60 min), historické počty a režim v upozornění.
- **Pravděpodobnost** smí do upozornění jen u ověřené hypotézy.

Simulace 40 000 běhů (podíl ověření podle skutečné úspěšnosti):

| varianta | falešné ověření při 50 % | při 60 % | 70 % | 80 % | 93 % |
|---|---|---|---|---|---|
| A — kontrola po každém releasu od n ≥ 10 | 8,6 % | 34 % | 73 % | 97 % | 100 % |
| **B — kontrolní body 10/20/30 (zvoleno)** | **3,7 %** | 22 % | 63 % | 95 % | 100 % |

## Ověření nad reálnými daty (26. 9. 2026, produkce jen čtení, zápis do scratch SQLite)

- **Backfill** `ReleaseMovesJob` od 28. 7. 2024: 166 shluků × ES/NQ = 332 řádků za ~50 s na
  hostiteli, 0 chyb. Shoda s datasetem výzkumu (headline shluku): výchylka 15 min 320/320
  a výnos 15 min 320/320 na 0,01 bp (max. rozdíl 0,000 bp), výnos 60 min 318/318, headline
  0 rozdílů. Bez měření 6 řádků (CPI 14. 7., PPI 15. 7., Retail Sales 16. 7. 2026 — díra
  v barech #1300); bez výnosu 60 min NFP 3. 4. 2026 (Velký pátek, zkrácená seance); bez
  `vol_ref` 18 řádků z prvních 20 seancí archivu (červenec–srpen 2024).
- **Statistika velikosti** (násobek p50/p75 · medián `vol_ref`): CPI ES 0,401/0,485 · 101,6 bp,
  NQ 0,399/0,459 · 159,5 bp; NFP ES 0,274/0,380; FOMC ES 0,253/0,443 (n 17); PPI ES
  0,230/0,287; PCE ES 0,142/0,211; ISM Services ES 0,217/0,267; Retail Sales ES 0,087/0,148.
- **Ukázky** (CPI 11. 9., NFP 4. 9., FOMC 16. 9. v T−60 a T−15): 361–469 znaků (s dovětkem M1
  po ověření až 520), pod `MESSAGE_BUDGET`; dnešní volatilita před nimi byla 0,66–0,81× obvyklé
  při releasu, očekávaná výchylka proto pod historickým mediánem (CPI ES 22–26 bodů = 29–35 bp
  místo 39–59 bp bez přepočtu). Řádek výchylky nese body první (úrovně jsou v bodech), původ
  čísla je na druhém řádku; desetinná tečka jako u cen.
- **Replay hypotéz** za 26. 6.–25. 9. 2026 (registrace posunutá jen pro replay): M1 ES i NQ
  11/11 (ověřeno by bylo při n = 10), H1 ES i NQ 1/1 (CPI 11. 9.), H3 ES 2/2 — ostatní
  jádra inflace v okně měla nulové nebo chladnější překvapení, případně chybějící bary.

## Body k rozhodnutí (varianty, doporučení)

1. **Noční job statistik velikosti.**
   - **A (doporučeno, implementováno):** bez nočního jobu — statistika se počítá při sestavení
     upozornění z `release_moves`. + nic nezastará (fakta jsou v PG ≤ 5 min po uplynutí 60 min),
     o tabulku a job méně; − API agregáty neukáže bez počítání (dnes je UI nepotřebuje).
   - **B (znění zadání):** noční přepočet do tabulky `release_move_stats` (rodina × symbol).
     + API čte hotové agregáty; − druhá cesta k témuž datu, zastarání až o den.
2. **Rodiny s upozorněním.**
   - **A (doporučeno, implementováno):** 5 robustních + Retail Sales a ISM Services se štítkem.
   - **B:** jen 5 robustních — méně zpráv, ale Retail Sales/ISM jsou velké releasy.
   - **C:** i Claims — jen shluky s headline Claims bez souběžného tier-1; +30 zpráv měsíčně,
     velikost nad běžným dnem jen v 39–42 z 62 případů.
3. **Kontrolní body** 10/20/30 (**doporučeno**, falešné ověření 3,7 %) vs. průběžná kontrola od
   n ≥ 10 (8,6 %, příklad ze zadání).
4. **H3 v upozornění po ověření** — dnes ne (směrová tvrzení mimo H1 zakázaná). Doporučení:
   rozhodnout až při ověření (~7/2027), H3 může být jen projev poklesu implikované volatility
   v býčím období 2024–2026.
5. **REGISTERED_AT = 1. 10. 2026** — vyžaduje merge tohoto ADR před tímto datem; při pozdějším
   merge posunout datum (ne zpětně), dokud žádný živý release nebyl vyhodnocen.
6. **Škálování na dnešní volatilitu**: násobek za release × medián denního rozsahu (**doporučeno,
   implementováno**) vs. doslovné znění schválení (poměr mediánů — horší než bez škálování).
7. **Automatický backfill prázdné tabulky** (implementováno, ~1 min v `reaction_loop`, dokud je
   tabulka prázdná) vs. jen ruční CLI — doporučeno ponechat automatický, aby nasazení
   nepotřebovalo krok navíc; CLI zůstává pro přepočet historie po opravě dat.

## Zamítnuté varianty

- **Scénáře P(↓|+)/P(↓|−) u všech řad** — u většiny řad šum, budí falešnou důvěru.
- **Doslovný poměr mediánů vol** — horší než žádné škálování.
- **rv20** — stejně přesné, ale potřebuje back-adjust rollů (#1301).
- **Výchylka ve stejnou denní dobu jako škálovač** — chyba 0,173.
- **Výpočet uvnitř `reaction_loop`** — zpoždění T−15 až 5+ min.
- **Přesné časovače na T−60/T−15** — přeplánování při změně kalendáře, restart je ztratí.
- **Hypotézy v DB** — dovolily by měnit kritéria dodatečně.
- **Průběžná kontrola po každém releasu** — dvojnásobné riziko falešného ověření.
- **Nearest-rank percentil (`reactions.percentile_abs`) pro medián/p75** — u n ≈ 25 skáče
  o celý řádek; výzkum i měření IS → OOS počítaly lineární interpolaci.

## Mimo rozsah (vlastní issue)

- upozornění **po** releasu (aktivní scénář, vícedenní výhled);
- kalibrace velikosti (odhad × skutečnost z `release_previews`) v API a UI;
- Claims; H3 v upozornění;
- rozpor `conventions.py` (#280: směr NFP a dalších řad podle překvapení) s výzkumem, který směr
  mimo inflaci neprokázal.

## Důsledky

- Uživatel dostane ke každému velkému releasu velikost, která se ověřila IS i OOS, a úrovně v jejím
  dosahu; směr jen u jádra inflace a s označením stavu ověřování.
- Ověření H1 potrvá zhruba 1,5 roku (teplejší jádro ~0,54× měsíčně, 27 z 69 shluků má nulové
  překvapení kvůli zaokrouhlení FF na 0,1); do té doby upozornění pravděpodobnost směru nenese.
- Šum na Telegramu: 7 rodin × 2 etapy × 2 instrumenty ≈ 32 zpráv měsíčně (vypnutelné přepínačem).
- Chybějící releasy (#1298) a vadné bary (#1299, #1300) snižují n nebo kazí jednotlivé výchylky;
  po opravě se CLI backfill spustí znovu (idempotentní, jen historie před registrací; živé
  hypotézy opravou nezmění vyhodnocený úsek).
- **Změnu času releasu kalendář do DB nepropíše** (#1298: `dedup_hash` = titulek + den, insert
  `ON CONFLICT DO NOTHING`, `update_actuals` mění jen actual/forecast/previous). Přesun v rámci
  dne nechá starý čas — upozornění T−60/T−15 i měření poběží podle něj. Přesun na jiný den nechá
  v DB fantomový řádek — v původní čas odejde falešné upozornění; měření a hypotézy ho
  nezapočtou (bez actual). Oprava #1298 musí klíč rozšířit o čas a starý řádek ošetřit (smazat
  nebo označit), jinak fantom zůstane v kalendáři. Chybějící událost v kalendáři job nepozná.
- Úrovně v T−60 jsou okamžité hodnoty 0DTE a do releasu se mění; dosah ± p75 je odhad, ne hranice
  (výchylka je nad p75 zhruba ve 21–23 % releasů).
