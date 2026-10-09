# Poučení z chyb a oprav (lessons learned)

Živý log toho, co se v GEXLens pokazilo, jaká byla **skutečná** příčina a co ji odhalilo — aby se
totéž nestalo podruhé. Není to changelog ani seznam bugů (ty jsou v issues); je to seznam **vzorců
chyb**, hlavně diagnostických a provozních.

**Jak používat**
- **Před laděním** každého problému projít sekce 1–3 (symptom se často opakuje pod jiným jménem).
- **Po každé opravě** s netriviální příčinou přidat záznam: `datum — symptom → příčina → co ji
  odhalilo → prevence`, odkaz na issue. Nejnovější nahoru v rámci sekce.
- Když se poučení opakuje nebo zobecní, **povýšit** ho na pravidlo v `AGENTS.md` (1–2 řádky) nebo
  na automatickou kontrolu (test, skript v `scripts/`, hook) — a tady nechat jen odkaz.
- Jeden fakt na záznam, stručně. Nepsat sem, co už říká kód, ADR nebo git log.

---

## 1. Provoz (Docker, deploy, git)

- **2026-10-09 — `uv run --env-file .env` vypsal řádek `.env` do konzole (#1486).**
  Při ručním backfillu proti produkční PG uv narazil na hodnotu s mezerami bez uvozovek a varování
  „Failed to parse environment file `.env` at position N: …“ obsahovalo **celou hodnotu řádku** (osobní
  kontakt). Do repa ani issues se nedostala, jen do výstupu session. → `uv run --env-file` (a jiné
  nástroje, které při chybě citují vstup) na `.env` nepoužívat; potřebnou proměnnou exportovat bez
  výpisu: `export GEXLENS_PG_PASSWORD="$(grep -m1 '^GEXLENS_PG_PASSWORD=' .env | cut -d= -f2-)"`.
  Formát `.env` ověřovat jen počty shod (`grep -c`), nikdy výpisem. Hodnoty s mezerami v `.env` dávat
  do uvozovek, jinak je každý parser čte jinak.
- **2026-10-06 — `await asyncio.to_thread(...)` v minutovém cyklu = cyklus stojí (#1337).**
  Měření disku (bind mount, až 121 s) a purge (~2,5 min) běžely ve vlákně, takže event loop žil, ale
  cyklus na ně čekal — 29. 9. chyběly cykly ES 21:31–21:32 UTC. Purge navíc měl pevných 21:30 UTC, což
  je od 1. 11. (CST) 15:30 CT, tedy otevřený trh. Odhalilo srovnání děr v minutových cyklech s časem
  purge a logem „pomalý bind mount“. → Pomalou údržbu odpalovat přes `background.BackgroundJob` (jeden
  běh naráz, výsledek se vyzvedne v další minutě, výjimka do logu) a čas jobu vázat na hodiny trhu
  (`marketclock`), nikdy na pevné UTC; test na léto i zimu.
- **2026-09-25 — spouštěč Task Scheduleru „23:05“ by v zimě běžel ve 22:05 (#1277): `New-ScheduledTaskTrigger` ukládá offset.**
  `StartBoundary` vzniká jako `…T23:05:00+02:00` = „synchronizovat napříč časovými pásmy“ → Windows drží čas v UTC
  a po konci letního času úloha poběží o hodinu dřív (u pauzy Globexu = do otevřeného trhu). Týká se i walk-forward
  a docker úklidu (opraveno v #1278). Druhá past téhož dne: plánovaný čas 23:05 padl přímo do okna deploye (logy enginu
  23:04–23:08), přestože komentář tvrdil „deploy 23:15“. → Po `New-ScheduledTaskTrigger` přepsat
  `StartBoundary` na místní čas — spouštěče zakládat jen přes `scripts/lib/LocalTrigger.ps1` (`New-LocalWeeklyTrigger`); čas úlohy, která zastavuje Docker, ověřovat podle
  skutečných časů v `data/logs/`, ne podle komentářů, a úlohy vzájemně vylučovat (čekání na běžící deploy).
- **2026-09-24 — naplánované úlohy tiše padaly 2 týdny (0x80070002): cesta k pwsh z WindowsApps nese číslo verze.**
  `(Get-Command pwsh).Source` = `…\Microsoft.PowerShell_7.6.5.0_…\pwsh.exe`; po aktualizaci Store balíčku
  na 7.6.6 cesta zmizela a „GEXLens walk-forward" (noční report pro #794/#1081) končil „soubor nenalezen"
  od 9. 9. Nikdo si nevšiml — úloha hlásila `Ready`, log jen přestal růst.
  → Do úloh dávat **App Execution Alias** `%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe` (přežije verzi);
  po registraci úlohu spustit nanečisto a číst `LastTaskResult`; při kontrole stavu úloh nečíst `State`,
  ale `Get-ScheduledTaskInfo` (poslední výsledek + čas) a mtime logu.
- **2026-09-23 — PostgreSQL 190 % CPU v RTH (#1257): `NOT IN (subquery)` přestal být hashovaný.**
  Job news-engine s `id NOT IN (SELECT event_id …)` běžel 23 min; do té doby milisekundy. Příčina není
  v kódu jobu jako takovém, ale v růstu tabulky: hash 264 k řádků se přestal vejít do `work_mem` 4 MB
  a plánovač spadl na Materialize + Filter per řádek (O(N·M)). Odhalilo `pg_stat_activity` (3 backendy
  s týmž dotazem = 1 dotaz + 2 paralelní workery) a `EXPLAIN` (`NOT (SubPlan)` bez slova `hashed`).
  → Anti-join psát jako `NOT EXISTS`, ne `NOT IN`; `work_mem` nastavit v compose; při „něco je pomalé"
  nejdřív `docker stats` + `pg_stat_activity` — pomalý test výkonu byl symptom, ne příčina.
- **2026-09-19 — falešný P1 „tichý tasty stream v RTH" (#1228): byla sobota.** Z „0 eventů DXLink",
  „IBKR bez kotací" a „KPI rth_minutes 0" složen incident, přitom trh byl zavřený; den v týdnu převzat
  ze souhrnu kontextu, nikdo ho neověřil. Odhalilo `outside_us_rth(now)` v kontejneru.
  → Před každým tvrzením o RTH/incidentu ověřit den a stav trhu z kódu (`marketclock`), ne z hlavy.
  „Po reconnectu přišlo N snapshotů" není důkaz obnovy — resubscribe vždy vrátí poslední známé hodnoty.
- **2026-09-10 — naplánovaný deploy enginu se nikdy nespustil (Task Scheduler 0x80070002).**
  Úloha registrovaná s `-Execute "pwsh.exe"` bez plné cesty; Scheduler nepoužívá PATH uživatele.
  → `-Execute (Get-Command pwsh).Source` + `-WorkingDirectory` (vzor `scripts/register-walkforward-task.ps1`);
  po registraci úlohu spustit nanečisto a zkontrolovat `LastTaskResult = 0`; ráno po plánovaném běhu
  první věc ověřit log, ne až když něco chybí.
- **2026-09-10 — deploy skript padal na „Could not find file" při zápisu do `data/logs`.** Příčina
  mimo skript: Windows Defender „Řízený přístup ke složkám" blokoval `pwsh.exe` (event 1123).
  Poznávací znak: `Out-File`/`New-Item` selže i pro existující adresář, zatímco bash/docker/git zapisují.
  → Bezpečnostní nastavení mění jen uživatel; do té doby deploy ručně přes bash.
- **2026-09-10 — engine OOM o půlnoci v DiskWatch (#1105).** `rglob("*")` přes celý datový strom
  v okamžiku, kdy news-engine po nočních jobech narostl a VM neměla rezervu.
  → Nikdy nematerializovat celý strom partic každý tick; po nočních jobech uvolnit paměť; při
  podezření na únik jednu noc `tracemalloc`.
- **2026-09-07 — záloha partic před `--apply` se tiše nepovedla (#1047).** Parsování seznamu souborů
  vrátilo 0 položek, ale řetěz `&&` pokračoval k `--apply`, protože `wc` skončil s exit 0.
  → Zálohu dělat jako **samostatný krok** s explicitní kontrolou počtu (`[ n -gt 0 ] || exit`);
  destruktivní krok pouštět až po přečtení výsledku zálohy, nikdy v jednom řetězu.
- **2026-08-27 — druhý pád Docker Desktop daemonu při buildu frontendu (12 min výpadek).** Den
  předtím totéž (23 min přes US open, snapshoty nenávratné). Čtyři buildy prošly, pátý daemon shodil —
  pád je intermitentní, úspěšné buildy nic nedokazují. Recovery: Stop-Process Docker Desktop +
  backend, `wsl --shutdown`, start; kontejnery `unless-stopped` naběhnou samy, catch-up srovná kumulativy.
  → **Na produkčním daemonu se nebuildí mimo pauzu Globexu (23:00–24:00 CEST).** Přes den jen
  mergovat, nasazení patří večernímu oknu (`scripts/deploy-engine-offhours.ps1`).
  Detekce výpadku: mtime datových souborů + curl na porty (docker CLI při pádu lže/visí).
- **2026-08-26 — background deploy řetěz tiše nenasadil.** Dlouhý řetěz commit→PR→merge→build→up
  skončil exit 0 a „HTTP 200", ale build nic nevyrobil a kontejner běžel na starém image
  (jediný signál: „Container Running" místo „Started").
  → Po **každém** deployi ověřit čerstvost artefaktu: `docker images <img> --format '{{.CreatedAt}}'`
  + `docker inspect <ctr> --format '{{.State.StartedAt}}'`. HTTP 200 nedokazuje nic — starý kontejner
  odpovídá stejně.
- **2026-08-25 — `git checkout main -- .` zahodil rozpracovanou práci, 2× za den.** Podruhé se místo
  opravy zamergoval jiný commit a po dvou „nasazeních" bylo v UI stále totéž.
  → Na úklid před commitem nikdy; pro přepnutí větve `git stash` nebo commit rozdělané práce.
  **Merge ≠ nasazeno ≠ funguje**: po nasazení ověřit výsledek v běžící aplikaci (data z API i v UI).
- **`scripts/merge-when-green.sh` se kotví na SHA při spuštění.** Po čerstvém pushi hlásí fail starého
  commitu, nebo hned po pushi vidí 0 checků → počkat ~60 s / `gh pr checks`, pak spustit.
- **Frontend: `npm run build`, ne `tsc --noEmit`.** CI staví `tsc -b`, který kontroluje i testy.
- **Ladil jsem test, ale kód na disku chyběl (#1108).** Skript s více úpravami spadl v půlce; hodina
  hledání, proč se změna „neprojevuje". → Před laděním padajícího testu `git status --short` + grep
  klíčového identifikátoru; vícekrokové úpravy psát idempotentně.

## 2. Diagnostika (zastavil jsem se u první hypotézy)

- **2026-10-08 — `urllib.robotparser` hlásil SEC `/cgi-bin` jako povolené (#1474).**
  Robots.txt SEC má jedinou skupinu `User-agent: *`, kterou přerušují prázdné řádky a komentáře.
  Standardní knihovna skupinu ukončí prázdným řádkem, takže `Disallow: /cgi-bin` tiše zahodila a sonda
  doporučila feed, který robots.txt zakazuje. Odhalil to nezávislý ověřovatel ruční kontrolou souboru.
  → Robots.txt vyhodnocovat podle RFC 9309: skupinu ukončí až další `User-agent`, platí nejdelší
  shoda a 5xx = zakázáno vše. Používat `robots_allows` ze `scripts/news_candidates_probe.py`
  s testem na strukturu SEC, ne `urllib.robotparser`.

- **2026-10-08 — stínová brána ngram hlavy (#1131) ukazovala falešný průchod: baseline z remízy, žádný nulový model denní doby.**
  Job řadil baseline podle průměru kategorie, takže celá kategorie měla jedno skóre. Hranice horního
  decilu padla dovnitř FED (2 821 řádků) a řádky vybralo pořadí z PG bez ORDER BY. Baseline proto
  mezi běhy skákala 0,91–1,66 a brána střídala průchod s neprůchodem (ES 29 : 13 ze 42 běhů,
  v termínu 26. 9. průchod 1,039 proti 0,869). Se shodami spočtenými jako očekávaná hodnota je hlava
  ve sdruženém liftu pod baseline. Odhalila to až celá tabulka historie běhů, ve které baseline
  skákala, zatímco lift hlavy stál; dřív se četl jen poslední běh. Druhá past: sdružený lift skóre,
  které se mění v čase (walk-forward průměry), vyšel 1,55, ale v každém týdnu jen ≤ 1,16. Míchal
  pořadí uvnitř dne s rozdíly mezi dny. Třetí past: model měl mezi rysy hodinu a zdroj. Uvnitř vrstev
  seance × hodina měl ρ −0,01 a nulový model zdroj × hodina bez textu byl lepší než on. Stejnou
  remízu a stejnou chybějící kontrolu měl i offline experiment #749, na kterém stál závěr
  „velikost ANO“ (oprava liftu #1467).
  → Ranking metrika se shodnými skóre počítá shody očekávanou hodnotou a nikdy nespoléhá na pořadí
  řádků. U stínové brány se čte celá historie běhů, ne poslední číslo. Skóre, které se mění v čase,
  se hodnotí i uvnitř seance. Model s rysy denní doby nebo zdroje se srovnává s nulovým modelem
  z týchž rysů bez textu.

- **2026-10-08 — měření zdí × podíl outright (#1019) dalo třikrát jiný verdikt, než se metoda ustálila.**
  První běh: „nepotvrzeno" — jenže práh výsledku „drží" ležel na úrovni, která spouští dotek (víc než
  polovina doteků „držela" už na dalším baru), a RTH s nocí se sčítaly, ač se skupiny lišily
  složením (86 % × 55 % RTH). Oprava se šířkou bariéry zvolenou až po prvním běhu dala zase
  „nepotvrzeno", ale s původní šířkou by síto prošlo. Pravdivý verdikt byl „neprůkazné". Odhalili to
  dva nezávislí ověřovatelé, ne já. → U event study: práh výsledku nesmí ležet na spouštěči události;
  vrstvy, které zadání jmenuje, jsou primární analýza (stratifikace), ne popisný rozpad; kontrola
  vůči nulovému modelu z polohy události; když se parametr mění po prvním běhu, povinně tabulka
  citlivosti a výslovná poznámka v reportu. Ověřovatele pustit znovu po každé změně metody.

- **2026-10-08 — živá CumΔ šla dva měsíce proti ceně a srovnání zdrojů to četlo jako „jiná veličina“ (#1018).**
  Midpoint test (SPEC 4.5) porovnával `snapshot.last` — obchod starší než snímek — s aktuálním bid/ask:
  při růstu ceny vyjde call „prodej“ a put „nákup“, obojí záporná delta. Srovnání midpoint × tisky
  (8. 9.) našlo jen „řady spolu nekorelují, NQ hladiny 5/5 záporně“ a uzavřelo to jako nález o datech,
  protože obě řady neměly „pravdu“. Odhalila to až **kotva**: korelace každé řady s pohybem ceny futures
  a s CVD futures (agresor od burzy) — midpoint záporně 26/26 seancí, tisky kladně. Týž běh ukázal,
  že nástroj počítal ploché víkendové partice jako seance a nafukoval „shodu znaménka“.
  → Dvě neověřené řady se nesrovnávají jen mezi sebou: přidat nezávislou měřenou kotvu (cena, CVD)
  a ptát se, kterým směrem každá čte. Odhad strany ze snímku cen nikdy nebrat jako agresora.
  Seznam seancí v měřicím nástroji vždy přes `settle.is_trading_session`.

- **2026-10-08 — feed Yahoo `rssindex` vracel dva týdny HTTP 404 a nikdo to nevěděl (#1451).**
  Zdraví collectoru hlídalo zdroj `rss_news` jako celek. Dokud odpovídaly CNBC a MarketWatch, byl
  zdroj „ok“ a mrtvý feed psal jen `warning` do logu každou minutu. Zdroj přitom přišel o 94 % zpráv,
  které už nejdou dohledat. Našlo se to až přes tvrdá data (`news_events` podle `raw.feed`, E-0.4a).
  Navíc mrtvý feed spolu s 304 u ostatních vyhodil výjimku a backoff zpomalil i zdravé feedy.
  → Souhrnné zdraví nesmí maskovat selhání části: stav držet per feed (`feed_failures`), trvalé
  selhání hlásit hranovým alertem (`FeedWatch`) a výjimku pro runner házet, jen když selže všechno.
  U každého nového zdroje s víc URL se ptát, kdo uvidí smrt jednoho z nich.

- **2026-09-26 — o víkendu chodila upozornění „data nechodí“ z IBKR i tasty (#1307): hlídače soudily zavřený trh z dat.**
  `oi_refresh_failed` à 30 min pro ES i NQ, `spot_fallback` po nočním reconnectu DXLink, `strikes_stalled`
  po sobotním rollu, dřív i `connection_stall` ~216× za víkend. Každý hlídač poznal zavřený trh jen
  nepřímo, nebo vůbec: publikační okno OI se počítalo pro každý kalendářní den, spot fallback bral
  „mlčí i tasty“ (čerstvost podle času PŘÍJMU, snímek po resubskripci vypadá živě), bary „spot se
  nehýbe“ (v pre-openu se hýbe), repair/greeks žádnou bránu neměly. Opakování dělal chybějící hranový
  stav: každý retry = nový alert, Telegram deduplikuje jen 10 min. Odhalil to log produkce srovnaný
  s rozvrhem a oi_eod (sobotní snímek = kopie pátku, páteční OI až v neděli večer).
  → Každý hlídač výpadku dat má bránu „očekávají se data?“ z `marketclock.is_market_closed`
  (při zavřeném trhu se nekrmí) a stav nasbíraný mimo seanci (repair kola, backoff) zahodí na hraně
  otevření. Datová proxy doplňuje rozvrh jen tam, kde
  rozvrh neví (svátky). Alert, který se opakuje s každým retry, je hranový. Nový hlídač se testuje
  na sobotě, neděli 16:59/17:00 CT, denní pauze a konci DST, ne jen v seanci. (Třetí výskyt vzorce
  po #968 a #1228 — kandidát na pravidlo v AGENTS.md.)
  Druhé kolo (review téhož dne): „otevřený trh“ ≠ „data se čekají“. OI publikuje CME jen v obchodní
  dny do 07:00 CT, takže neděle 17:00–19:00 CT je otevřený trh bez publikace a noc před oknem
  předpublikační stav — hrana alertu per UTC den se tam spotřebovala a skutečná porucha po okně
  se už neohlásila. Stejně tak brána jen v jednom hlídači nestačí: `disconnect` z API, 10197/354
  a `degraded_start` při sobotním rollu chodily dál. A tvrzení „po otevření se čte hned“ platilo
  jen v testu, který čítač natahoval ručně — sekvence se testují minutu po minutě.
  Svátek rozvrh nezná a `feed_crosscheck` „oba mlčí uvnitř US RTH“ se tam opakuje à 15 min
  (Vánoce ~26×) — otevřené k rozhodnutí v #1307.
- **2026-09-25 — zvonek 516–596 „anomálií“ za 14 dní na šum, žádná na CPI/FOMC (#1291, ADR-0043).**
  Tři vrstvy: (1) `importance` v `news_events` je po regexovém klasifikátoru, ne od zdroje — přepisuje
  i FF impact (USD PPI High → 1, „FOMC Member Speaks“ Low → 3), takže „významnost“ podle importance
  u kalendáře lže oběma směry; (2) kontaminace okna (jiná zpráva s importance ≥ 2) při ~2 800 zprávách
  denně zasáhla 82,5 % oken — strukturálně blokovala skutečné makro releasy a pouštěla poslední šum
  před klidem; (3) close-to-close výnos míjí whipsaw (FOMC 16. 9.: ES −0,3 bp, výchylka −19 bp).
  Popis v issue („+19 bp za 60 min“) navíc neseděl na kód (okno 5 min, hodnota byla `ret_15`).
  Odhalilo: replay starého pravidla nejdřív ověřený proti logu news-enginu (9/9 běhů), až pak čísla.
  → Význam zprávy u scheduled brát z `raw` zdroje, ne z přepsaného sloupce; pohyb hodnotit na shluku
  zpráv (jedno upozornění = shluk × instrument), ne na jednotlivé zprávě; reakci měřit výchylkou.
  Replay pravidla vždy nejdřív ověřit proti produkčnímu logu a tvrzení z issue proti kódu.
- **2026-09-23 — backfill svíček SPY „TimeoutError po 60 s" (#1253): živá subskripce nikdy neztichne.**
  Sběr `Candle{=1m}` končil „3 s ticha", ale DXLink po historii dál posílá updaty právě tvořící se
  minuty (u SPY v RTH každou chvíli) → ticho nenastalo, strop 60 s vše zahodil. Druhá vrstva: `until`
  se sekundami pustil rozdělanou minutu mezi „díry", takže rekonstrukce ES/NQ ji hlásila každou minutu
  znovu a nikdy nedoplnila. 14. 9. tentýž symptom připsán souběhu handshaků (zámek pomohl jen náhodou).
  Odhalilo: log s časem tokenu (0,3 s) a nic dalšího 60 s → čas mizí až ve sběru, ne v připojení.
  → U streamů rozlišovat „nová data" od „jakákoli zpráva"; při stropu vracet částečný výsledek a
  logovat **fázi**, ve které se čas ztratil; okno s rozdělanou minutou vždy zarovnat na celé minuty.
- **2026-09-23 — test tiskl klíč z `.env` (#1254).** `Settings()` v testu načetl skutečný `.env`,
  assert `== ""` padl a pytest hodnotu vypsal do výstupu; v CI bez `.env` prošlo.
  → Root `conftest.py` vypíná `env_file` pro všechny sady; asserty na tajemství psát jako
  `assert not value` (při pádu se hodnota netiskne).
- **2026-09-09 — #576: závěr „hypotéza se nepotvrzuje" stál na vadné metrice.** Korelace ≈ 0 z 35 seancí,
  přitom tabulka obsahovala anomálii (cliff_share 0,028 na OPEX), která byla chybou měření: |NetGEX|
  0DTE řetězu se před settle vynuluje (call/put se odečtou). Po opravě na hrubou gammu korelace +0,3.
  → Nejdřív prověřit anomálii, která metriku diskvalifikuje, pak teprve statistika. Podezřelou hodnotu
  nenechávat jako „vedlejší nález" pod závěrem, který na ní stojí.
- **2026-09-09 — hlavička NQ ukazovala 7573.17: ne záměna spotu, ale demo dataset (#1096).** Hodnota
  = poslední close demo dne (`heatmap/demo.ts`), který `useDayData` vrací jako fallback; odvozené
  hodnoty (cena, gamma badge, settle watch) neznaly zdroj. Totéž číslo bylo ráno na devu s popiskem
  „demo data" — porovnat čísla dřív než hádat.
  → Fallback/demo data nesmí prosakovat do UI jako fakt: každá odvozená hodnota zná `day.source`.
  Chybu na produkci řešit hned (issue + příčina), ne „až se bude opakovat".
- **2026-09-09 — „zelená zeď pod cenou" v módu Vol OTM = zamrzlý spot, ne chyba masky.** TWS
  1100→1102 zabil `reqRealTimeBars`, watchdog jen alertoval, resubscribe běží až po plném reconnectu;
  `filledSpots` forward-fillovaly poslední close.
  → Při podivné OTM/ITM klasifikaci porovnat `max(ts_min)` z `/api/bars` s `last_tick_ts` z `/api/status`.
- **2026-09-07 — falešný poplach „engine 2,5 dne stál" (#1054).** Snapshoty končily v pátek, bary šly
  dál se `source=ibkr` → „stall pipeline". Ve skutečnosti vypnuté PC; bary doplnil pondělní backfill
  z IBKR historical (zdroj nerozlišuje živé od doplněných → #1055). Hodina práce + P1 k zavření.
  → Před vyhlášením incidentu vyloučit vypnuté PC / zavřený trh (zeptat se). „Řada X pokračuje" není
  důkaz běhu, dokud nevím, zda se X nedoplňuje zpětně (mtime partice). `LastBootUpTime` na Windows
  s Fast Startup o vypnutí nic neříká.
- **2026-09-02 — metrika ve `/status` neměřila to, co jsem myslel (#982).** `tasty_symbols: 7 560`
  přečteno jako velikost subskripce; byl to počet symbolů v cache od startu (včetně odhlášených);
  skutečná subskripce `tasty_symbol_breakdown.total`.
  → U každé metriky nejdřív dohledat v kódu, co počítá. Tvrdý strop dala sonda mimo produkci
  (`scripts/tasty_probe.py sizecap`), ne dohady.
- **2026-08-27 — DXLink smyčka smrti po restartu v RTH (#916).** Plná subskripce (~90 s po rate
  limitu) > default `ping_timeout=20 s` → klient spojení sám zabil, každý reconnect začínal znovu.
  Fix `ping_interval=25, ping_timeout=120` + log dokončení subskripce (#917).
  → Liveness limity transportu nesmí být přísnější než nejdelší legitimní blok práce. **Dokončení
  dlouhé operace vždy logovat** — nedoběhnutá subskripce nikde nechyběla. Restart enginu v RTH
  = plná subskripce do zatíženého serveru → rate limit skoro jistý.
- **Obvinil jsem cizí systém (#845).** „dxFeed neposílá data pro ES" — sonda ukázala, že posílá;
  server odmítal naše subskripce („subscription rate is too high") a ERROR zprávy jsme zahazovali.
  → Než obviním externí zdroj, ověřit ho izolovaně a zkontrolovat, zda nezahazujeme jeho chyby.
- **Funkce už existovala (#834).** Tvrzení „projekce mrazí gamma pole", přitom `gexfield.gamma_field`
  budoucí sloupce počítá a klient je používá; flag viděn, zapisovatel nedohledán.
  → Grepnout **všechny** zapisovatele i čtenáře symbolu, ne jen první výskyt.
- **Ukvapený závěr z jednoho vzorku.** CPU 69 % z jednoho `docker stats` = minutová špička; ustálený
  stav 3 %. → Metriky se špičkami měřit opakovaně.
- **Chybná srovnávací báze (#616).** Stale share 8,9 → 15,5 % vypadalo jako regrese; vzrostl počet
  striků o 54 % a míchaly se řetězy s různou kadencí. → Ověřit, že obě čísla měří totéž.
- **Implementace mířila vedle (#828, #849).** Široké pásmo dané extended expiracím (aktivní tam
  z definice není); archivní striky přidávané jen mimo grid (většina v gridu je s OI = 0).
  → Po implementaci změřit dopad na **cílovou metriku**, ne jen že kód běží.

## 3. Práce s daty uživatele a obchodní logika

- **2026-10-09 — tisíce reakcí bez minutové fáze, i když bary existují (#1494).** Backfill zpráv
  (17. 8.) proběhl dřív než backfill barů (3. 9.): `ReactionJob` spočítal jen denní fázi a event
  už nikdy nevybral, protože pending dotaz bere jen eventy bez jakéhokoli řádku (ochrana #655).
  Na díru se přišlo až při měření mega caps (#1491): hlavní výsledky Q2 ve vzorku chyběly.
  → Kdo ukládá „spočítáno“ podle existence řádku, musí mít cestu, jak přepočítat, když vstup
  přibude později (`scripts/backfill_minute_reactions.py` po každém backfillu barů). Před měřením
  nad historií zkontrolovat pokrytí: podíl NULL po měsících a u největších událostí vzorku.

- **2026-10-08 — walk-forward spadl po změně signatury `replay` a replay měřil setupy, které živý engine nedetekuje (#1081, #1464).**
  #1366 přidal do `scripts/backtest_setups.py` `replay` povinný `symbol`; testy a jediný volající
  v témže souboru prošly, noční `walkforward_setups.py` spadl na `TypeError` — `scripts/` mypy
  nehlídá a o pádu věděl jen log. Při kontrole reportů se navíc ukázalo, že `build_minutes` bere
  všechny `levels` expirace, i ty, které den předem píše sekundární runtime (kadence 3 min):
  36 % (ES) a 44 % (NQ) setupů replaye vzniklo tam, kde živý `SetupEngine` neběží.
  → Při změně signatury funkce ze `scripts/` grepnout volající ve všech skriptech a mít na každého
  nočního volajícího test přes skutečnou funkci. Replay má přehrávat **jen minuty, kdy živý engine
  nad daným vstupem běžel** — data partice nejsou totéž co „seance enginu", hranici odvodit z rollu.

- **2026-10-07 — paper, sondy T9 a replay backtestu hodnotily jinak než setupy (#1345, #1369): oprava jednoho vyhodnocovače nestačí.**
  #1320 převedl živé setupy na cestu ceny, ale tři další vyhodnocovače téže otázky („zasáhla cena
  stop/cíl?“) dál hodnotily vzorek cyklu: paper účet bez barů rovnou čáru zamrzlého spotu, sondy jen
  poslední bar dávky, replay každou minutu bez děr a bez timeoutu (setup otevřený v settle skončil
  `active` bez R a do walk-forwardu nevstoupil). Replay sond navíc vynechal minuty bez profilu — stop
  v nich se ztratil.
  → Po opravě pravidla vyhodnocení najít **všechny** kopie téže otázky (grep `evaluate_bar`) a převést
  je naráz na jednu funkci; výjimky zdokumentovat jen dočasně s issue. Data pro detekci (profil)
  a data pro vyhodnocení (bar) filtrovat zvlášť.

- **2026-10-07 — odložené reakce jedné uzavírky jako nezávislé vzorky (#1311): sloučení podle času zprávy minulo pseudoreplikaci.**
  #1293 slučoval vzorky se shodným `ts_event`, ale víkendové zprávy mají každá jiný čas a přitom
  tentýž výnos — základní cena je poslední bar před uzavřením a okno začíná prvním barem po
  otevření. `n` deferred bucketů bylo 2,5–5× nafouknuté a gate stál na pseudoreplikacích.
  → Nezávislost vzorku odvozovat z **toho, co výnos určuje** (základní cena a začátek okna), ne
  z identity zprávy. Klíč takové skupiny ukládat jako měření (`closure_open_ts` z barů), ne
  odhadovat z kalendáře.
- **2026-10-07 — FA validace a kalibrace α nad předpublikačním OI (#1314): dedup zamkl bod spočítaný z neúplných dat.**
  Joby běžely po každém úspěšném OI archivu, tedy i po prvním v 00:00 UTC, kdy dnešní snímek nese
  ještě včerejší čísla. ΔOI ≈ 0 a denní dedup pak bod zamkl — pozdější obnova po publikaci (#463)
  přepsala archiv, ale bod už ne. Druhá cesta téhož výsledku: symbol bez kalibrace po bodu s mediánem
  ≤ 0 dostal `fa_alpha` s α 0 (větev „α beze změny“ zapsala `state.alpha if state else 0.0`), runtime
  ho převzal a FA vrstva NQ byla vypnutá.
  → Výpočet, jehož výsledek dedup zamyká, spouštět až nad **finálními** vstupy (`oi_final`), ne nad
  prvním úspěšným čtením. „Beze změny“ u neexistujícího stavu znamená stav nezakládat, ne zapsat nulu.

- **2026-10-07 — settle kvartálního data podle trading class (#1366): datum expirace nestačí k určení jejího settle.**
  Pravidlo „3. pátek kvartálního měsíce = SOQ 9:30 ET“ platí pro standardní kvartální třídu, ale po
  rollu front kontraktu sbírá pipeline na totéž datum týdenní sérii nového kontraktu (EW3/QN3), která
  se vypořádá v 16:00 ET. Engine ji ukončil ráno a kvartální pátek zůstal bez živého řetězu. Totéž
  datum, dva různé settle — rozhoduje trading class. Historické řádky třídu nenesly, takže pro ně
  bylo potřeba pravidlo dovození (kořenový ticker po rollu = týdenní série) a uvést ho v reportu.
  → Hranici expirace počítat z identity řetězu (datum + trading class + podklad), ne ze samotného
  data. Údaj, na kterém hranice závisí, ukládat do záznamu už při vzniku (`context.trading_class`);
  dovozování pro historii držet v jedné funkci (`settle.history_expiry_settle`) a hlásit ho.
- **2026-10-07 — roll expirace v settle (#1331): „zastavit pipeline v okamžiku settle“ by potichu ztratilo stav dne.**
  Přímočaré provedení varianty A (zastavit pipeline v orchestrátoru dřív, než cyklus minuty settle
  poběží) by sondy T9 nechalo v DB navždy `active` (otevřené drží jen v paměti a timeout dělá první
  cyklus po settle) a agregát `map_state` by se nezapsal: vzorky RTH jsou v paměti, zápis čekal na
  settle + 5 min a nová pipeline vzorky nemá. Druhá past: IBKR `reqSecDefOptParams` vrací dnešní
  expiraci i po jejím settle — bez filtru by nová pipeline vzala tutéž vypršelou expiraci a roll by
  se točil každou minutu. Odhalil to audit všech modulů v `run_minute` po jednom: „kdo něco dělá
  v settle a odkud bere stav?“, ne testy (každý modul zvlášť procházel).
  → Restart pipeline v hraničním čase plánovat **po** cyklu té hranice (poslední cyklus starého stavu
  uzavře, co k hranici patří; nové záznamy blokuje invariant `born_after_settle`). Zápis ze vzorků
  v paměti nedávat za odklad, který přežije restart; modul po odkladu musí pracovat nad partice/DB
  idempotentně. Výběr „aktuální“ položky ze seznamu zdroje filtrovat týmž predikátem jako roll
  (`expiry_expired`), jinak roll a výběr nesouhlasí. Hranici počítat jedním helperem
  (`settle.expiry_settle`), ne třemi kopiemi, které se rozjely (16:00 ET vs. SOQ).

- **2026-10-01 — zkouška setupu (#1323) by pouštěla setupy, které se do jejího rozpočtu nezapočtou: dva zdroje času.**
  Engine razí `created_ts` časem cyklu zaokrouhleným na minutu, API razilo začátek zkoušky přesně.
  Setup z cyklu, ve kterém engine novou verzi aplikoval (14:06:00 < začátek 14:06:20), zkouška
  pustila přes bránu, ale čerpání (setupy od `started_at`) ho nikdy nezapočetlo — strop rozpočtu
  neplatil a alert „zkouška skončila“ lhal. Testy procházely, protože začátek dávaly vždy minuty
  před `now`. Odhalilo oponentní review (repro se začátkem `now + 20 s`). Druhá díra téhož druhu:
  čerpání se čte jen z aktuální mechaniky, takže zvednutí `SETUP_MECHANICS_VERSION` by vyčerpané
  zkoušce vrátilo rozpočet bez rozhodnutí uživatele.
  → Kde rozhodnutí (přebití) a jeho účetnictví (čerpání) filtrují tentýž čas, musí platit invariant
  „co rozhodnutí pustí, to se započte“ (`gate_overridden` ⇒ `created_ts` ≥ `started_at`) jednou
  predikátní funkcí pro obě strany (`TrialCell.in_force`); testovat i časy uvnitř minuty a změnu
  verze, ne jen „začátek o hodinu dřív“. Stav, který zvyšuje riziko, nesmí obnovit změna, kterou
  udělal systém (verze mechaniky), jen člověk.
- **2026-10-06 — vyřazení setupů nad zamrzlým spotem ze statistik (#1346): kritérium nejde spočítat z řádku a kvartální větev přepočtu vstup vůbec nekontrolovala.**
  Uživatel rozhodl historické setupy „vstup mimo bary“ vyřadit stejně jako vznik po settle (#1324).
  Kritérium ale na rozdíl od `born_after_settle` stojí na barech v partici: chybějící
  `context.entry_bar_ts` nestačí (nemá ho celá v5 před #1320, 686 řádků „beze změny“) a čtení barů
  v API/enginu u každého čtení historie by bylo pomalé (bind mount) a mohlo by se rozejít s tím, co
  uživatel schválil. Přepočet #1320 navíc kvartální expiraci (18. 9., 23 setupů) vrátil jako
  „neověřitelný“ **dřív**, než kontroloval vstup — setup nad spotem v ten den by zůstal ve statistikách
  (v produkci tam žádný nebyl, ověřeno doplněnou kontrolou).
  → Rozhodnutí, které stojí na datech mimo řádek, zapsat jako **trvalou značku** v řádku
  (`context.excluded`, skript dry-run → schválené CSV → zápis, nic nemazat) a čtenáři se ptají jen
  značky přes **jeden predikát** (`counts_in_stats` = ne po settle a bez značky), který nahradil
  `born_after_settle` u všech čtenářů najednou. V klasifikátoru s časnými návraty kontrolovat, zda
  časný návrat nepřeskakuje kontrolu, na které závisí jiný konzument (vstup na settle nezávisí, tak se
  ověřuje před větví settle).
- **2026-10-06 — setupy nad zamrzlým spotem (#1346): #1320 opravil vyhodnocení, ale vznik dál bral spot.**
  Dávka cyklu bez baru dávala `MinuteInputs` se spotem jako O = H = L = C; při výpadku streamu spot
  stál a detektory nad ním vytvořily setupy, jejichž vstup trh v té minutě neměl (ES 1004, NQ 1048 pod
  vlastním stopem, ES 1049). Odhalil to přepočet #1320 verdiktem „vstup mimo bary“. → Pravidlo „spot
  místo baru jsou vymyšlená data“ platí pro každého konzumenta minuty, nejen pro toho, kde se chyba
  ukázala: při opravě vstupu jedné cesty projít všechny, které tentýž náhradní vstup čtou (vznik,
  historie detektoru, feature log). Minuta bez baru se přeskočí, opožděný bar zpracuje další dávka.
- **2026-10-01 — oprava barů 8.–15. 9. pro přepočet setupů (#1320): stráž a razítko platily jen pro nové zápisy jedné cesty.**
  Hluboký backfill `scripts/backfill_bars.py` razil bary IBKR historical jako živé `ibkr`. Konvenci `ibkr_hist`
  (#1055) dostal jen `UnderlyingBackfiller` v enginu. Při opravě 9. 9. se ukázalo, že všech 147 (ES)
  a 150 (NQ) starších barů `ibkr_hist` je z ESZ6/NQZ6, zatímco engine měřil U6: bloky 04:30–06:22
  a odpoledne (ES 14:43–15:16, NQ 14:42–15:18) se skokem o basis. Zapsalo je doplnění před stráží
  kontraktu #1232. Stráž chrání jen nové zápisy, staré partice nikdo neprošel. Odhalil to přepočet setupu ES 1068 („skok ceny … jiný kontrakt?“),
  potvrdil sken skoků close > 0,4 % mezi sousedními bary. Bary IBKR historical U6 seděly na živé minuty
  s mediánem odchylky 0,000 %. Bloky přepsala volba `--replace-wrong-contract` (jen `ibkr_hist`
  mimo toleranci od ověřeného staženého baru); přepočet pak u ES 1068 i NQ 1064 potvrdil stop,
  opravil se jen čas uzavření.
  Medián odchylky přes celý den navíc pustí partici, ve které engine během dne přepnul kontrakt,
  proto oprava dne ověřuje kontrakt i po blocích: nejbližší měřená minuta **téže seance** po stranách
  bloku. Roll přichází s novým discovery (restart, nová seance), takže minuta za denní pauzou o kontraktu
  bloku nic neříká; první verze ji brala a na rolovací den by přepsala správné bary nové seance.
  Přepis dat, která v partici jsou, potřebuje ověřenou minutu po obou stranách, doplnění díry stačí jedna.
  → Konvenci původu dat a zápisovou stráž držet v **jedné funkci**, kterou volají engine i skripty
  (cesta `--days` skriptu teď bere `UnderlyingBackfiller.backfill_day` + `contract_mismatch`;
  hluboký backfill bez `--days` razí `ibkr_hist`, ale stráž kontraktu zatím nemá). Po zavedení
  stráže projít i data zapsaná před ní (sken skoků), jinak stará chyba přežije v archivu.

- **2026-09-30 — setupy 1003 a 1004 zapsané jako cíl místo stopu, timeouty za cenu z jiného času (#1320): vyhodnocení bralo, co přišlo v cyklu, ne cestu ceny.**
  `SetupEngine._evaluate_open` hodnotil jen bary z dávky cyklu a bez nich agregát spotu v okamžiku cyklu.
  3. 9. 14:17–15:30 UTC stál IBKR stream i spot (NQ 29317, ES 7709): feature log ukazuje 72 minut
  O = H = L = C. Stop ve 14:56 (NQ 1003) a 14:34 (ES 1004) engine minul a první živý bar v 15:30 nad cílem
  zapsal jako cíl (−3 966 $ na 1 kontrakt u 1003). Bary té doby dnes v partici jsou (zdroj `ibkr`, tedy
  zápis před #1055 — doplnil je až některý pozdější backfill), engine je ale v době vyhodnocení neměl
  a nikdo je nevyžádal: `BarsStallDetector` bez pohybu spotu nečítá, takže `stalled`/`recovered`
  a re-backfill po návratu streamu nepřišly. NQ 1373 (27. 9. večer, IBKR stál, spot = mid kotace
  z tasty jednou za minutu): spot 30 793,625 stop 30 790,73 minul, bar ve 23:06 má low 30 790,50
  v IBKR historical i v dřívější rekonstrukci `tasty_candle`. Timeout bral cenu minuty, kdy se zjistil:
  restart 10. 9. ve 20:50 (1106–1110), pondělí po Labor Day 7. 9. 06:31 (1031: −0,74 R místo −0,08 R),
  cyklus 20:00 se spotem už po settle (1407–1409: mid 7 736,875 místo close 19:59 7 732,50) nebo se
  zamrzlým spotem (1016: 29 510,5 místo 29 530,75). Na zamrzlém spotu setupy i vznikaly (ES 1004:
  vstup 7 709 = close baru 14:18, trh v minutě vzniku 7 710,5–7 714,75; NQ 1048 8. 9.).
  → Výsledek obchodu je funkce **cesty ceny**, ne vzorku v okamžiku cyklu. Úrovně se hodnotí nad bary
  v pořadí bez vynechané minuty, od baru vstupu (jeho close je entry). Chybějící minuta běžícího trhu
  je díra, na kterou se čeká, ne rovná čára; spot místo baru jsou vymyšlená data. O doplnění díry si
  říká ten, kdo ji potřebuje (`SetupEngine.request_bars`), ne detektor, který ji nemusí vidět. Čas
  uzavření patří baru, který rozhodl, nebo hranici (settle), nikdy okamžiku zjištění; vedlejší efekty
  uzavření (série stopů, cooldown) v pořadí barů. Živý engine a offline přepočet sdílejí jednu čistou
  funkci (`compute/setups.walk_setup_path`) i začátek cesty (`context.entry_bar_ts`), jinak se
  rozejdou. Oprava výsledku přepisuje i MFE/MAE ze stejné cesty (stop s MAE pod rizikem si protiřečí).
  Při rozboru porovnat feature log (co engine viděl) s particí barů (co se stalo): O = H = L = C proti
  svíčce je podpis zamrzlého vstupu, vstup mimo rozsah barů minuty vzniku je setup nad zamrzlým spotem.

- **2026-09-29 — brána šablon dávala ES a NQ různé verdikty téže šablony (#1325): komentář tvrdil sdílení, které konstrukce nezajistila.**
  `SetupEngine.point_values` nesl komentář „sdílený slovník všech instancí“, ale `field(default_factory=dict)`
  dal každé instanci vlastní slovník a `__main__` žádný společný nepředal, takže každá instance znala jen svůj
  symbol. `affordable_results` proto bral řádky s `affordable` z obou symbolů a dopočtené řádky před pravidly
  jen z vlastního. ES a NQ tak tutéž šablonu ve stejnou chvíli hodnotily nad jiným vzorkem (NQ pass při
  LB +0,004, o 5 min později ES block) a verdikt NQ kmital. Všechny obchodovatelné setupy v5 do nasazení
  opravy (NQ trend_continuation; k 30. 9. 06:47 UTC osm, z toho 1397 a 1414 po settle) prošly jen díky tomu:
  replay opravené brány dává u všech block. Testy prošly, protože slovník plnily ručně
  (`engine.point_values["ES"] = 50.0`) a měly jedinou instanci. Odhalil to přepočet `template_gate_n` z kontextu: u 263 z 266 řádků seděla hypotéza „každá
  instance zvlášť“, hypotéza „sdílený slovník“ u žádného.
  → Tvrzení komentáře o sdílení nebo životnosti stavu (sdílený, singleton, per proces) ověřit **v místě
  konstrukce** (`grep` konstruktoru a všech zapisovatelů), ne v místě použití. Když na něm stojí rozhodnutí,
  test staví objekty jako produkce (víc instancí, nic se neplní ručně) a porovná **vstup** rozhodnutí per
  instance, ne jen verdikt (`test_brana_es_a_nq_maji_vlastni_vstup_a_verdikt`). Nejlepší sdílený mutable
  stav je žádný: brána má klíč šablona × symbol a hodnotu bodu vlastního symbolu instance zná
  (`runtime.multiplier`).

- **2026-09-29 — 81 setupů v5 vzniklo po settle vlastní expirace a příští minutou skončilo timeoutem (#1324): pipeline roluje expiraci až s novým UTC dnem.**
  `expiry_expired` porovnává expiraci s `now.date()` v UTC, takže 0DTE řetěz běží po settle (v létě 20:00 UTC)
  ještě 4 h až do půlnoci UTC — přes poslední hodinu Globexu, denní pauzu i večerní otevření. `SetupEngine`
  se na živost expirace neptal (hlídala ji jen šablona T3 přes `minutes_to_expiry`), takže detektory nad
  vypršelým řetězem vyráběly setupy 0–238 min po settle a timeout podle expirace setupu (#259) je příští
  minutou zavřel. Škoda: 1 obchodovatelný setup s pushem (1397 NQ), nafouknuté `n` brány šablon a poplatky
  v souhrnu #1319 (~810 $ v jednotkách aplikace). Odhalil to rozbor stínových setupů: maximum 238 min po
  settle sedělo na roll o půlnoci UTC v logu enginu („vypršela — roll na novou“). Tentýž vzor mají sondy T9
  (docstring `probes.probe_settle` mluví o „dead-chain okně“).
  → Invariant vzniku záznamu vázaného na expiraci hlídat na **místě vzniku** jedním čistým predikátem
  (`compute/setups.born_after_settle`) a **v jeho offline zrcadle** (kandidát v `replay` backtestu
  a walk-forwardu, jinak se live a replay rozejdou; přeskočit kandidáta, ne ořezat minuty — ořez by
  změnil i vyhodnocení setupů otevřených před settle). Týmž predikátem vyřadit historii ze čtení, která
  z ní počítají (brzdy, brána, kalibrace confidence, sebekontrola, kouč, souhrn; gamma útes #1331), řádky
  v tabulce označit a počet vyřazených ukázat; historické řádky nemazat. Hranici brát z ADR
  (`expiry_settle_ts`, ADR-0039), ne z nejbližší funkce. Invariant je jen pojistka: dokud pipeline
  roluje podle UTC kalendářního dne (další výskyt „kalendářní den místo obchodního“), nevznikají mezi
  settle a půlnocí UTC žádné setupy. Příčinu řeší #1331 (roll v settle mění všechny moduly pipeline,
  proto samostatné rozhodnutí).

- **2026-09-29 — týdenní brzda sčítala 84 dní místo týdne (#1322): jeden dotaz pro dvě okna, filtr jen u jednoho.**
  `_load_realized` čte uzavřené setupy od `min(week_start, now − 84 dní)`, aby měla data i brána šablon.
  `brake_state` pak podle `session_bounds` vymezil jen den a do `week_r` přičetl všechno. Kontext od 28. 9.
  ukazoval týden −2,0 R ze ztrát z pátku a další tři stopy by brzdu −6 R držely až do prosince. Testy
  prošly, protože jejich vstup obsahoval jen řádky téhož týdne. K tomu dva chybné texty: docstring „třetí
  stop“ (kód zastaví šablonu po druhém) a „do settle“ v alertu i nápovědě (brzdy končí otevřením Globexu
  v 17:00 CT). Odhalil to přepočet `realized_week_r` z PG: 84denní součet seděl u 265 z 266 řádků.
  → Když jeden dotaz slouží několika oknům, **každé okno si vymezí jeho konzument** a test mu podstrčí
  řádky mimo okno (minulý týden, řádek přesně na hranici). Text o okně (docstring, alert, nápověda)
  odvozovat z kódu hranice, ne z paměti.

- **2026-09-28 — „celkem" na obrazovce Setupy zaseknuté na 200 obchodech; ES ukazovalo +690 $ místo −2 338 $ (#1319).**
  Frontend sčítal souhrn (Σ P/L, EV, účet, denní bilanci, Stats → Výkon i režimovou tabulku, Deník hledal
  setup u minuty) nad odpovědí `GET /setups/{symbol}`, která má `LIMIT 200 ORDER BY created_ts DESC` — stránku
  pro tabulku. „Celkem" tak bylo **klouzavé okno** posledních 200 setupů: každý nový setup vytlačil nejstarší
  a u ES zrovna vypadávaly velké ztráty ze začátku v5, takže bilance „vylezla z mínusu do plusu". Číslo
  vypadalo věrohodně; prozradil ho až počet obchodů, který přestal růst, a nezávislý přepočet z PG. Druhý
  nález: řazení jen podle času nebylo stabilní (31 dvojic téhož symbolu se shodným `created_ts`).
  → Stránka dat **nikdy** není podklad agregace: souhrn počítá server nad celou množinou (čistá funkce
  `compute/setup_summary.py` + `GET /setups/summary`), stránka nese `total_count` a UI píše „N z M";
  řazení stránky vždy s unikátním druhým klíčem (`id`). Při review hledat `reduce` / `filter().length`
  nad výsledkem endpointu s limitem.

- **2026-09-27 — víkendové klíče OI archivu jako „předchozí den“: pondělní ΔOI proti neděli, FA body „pátek → sobota“ (#1309 část 2).**
  OI archiv má klíč podle UTC dne pořízení, takže nese i sobotu a neděli; jejich obsah závisí na hodině:
  sobota a neděle před otevřením Globexu jsou kopie pátku, neděle po otevření už pondělí (produkce 18.–21. 9.:
  sobota = pátek na všech stranách, neděle = pondělí na všech sekundárních expiracích). `latest_day_before`
  bral poslední klíč bez ohledu na den, takže `/oidelta` v pondělí dávalo ΔOI = 0 (u 0DTE ES 21. 9. hlas
  „převaha put“ místo „převaha call“) a o víkendu ukazovalo kopii proti originálu; FA validace i kalibrace α
  v sobotu uložily bod „pátek → sobota“ (ΔOI ≈ 0, medián 0) a dedup dne pak zablokoval poctivý bod
  „pátek → pondělí“. Oprava: `latest_trading_day_before` pro všechny ΔOI (API, T6, FA) a FA/α jen v obchodní
  den. Druhý nález téhož srovnání: strike mimo včerejší obálku počítal `/oidelta` jako „+celé OI“ — sekundár
  má 160 striků, aktivní ~560, takže pondělí proti pátku by bez průniku lhalo jinak. Stejným vzorem běžely
  i `vol_concentration` (26. 9. 00:05 UTC nad 10 snímky s páteční volume po sobotním rollu), `greeks_bs_fallback`
  bez brány zavřeného trhu a vyhodnocení verdiktů dne o víkendu. → Pravidlo „Obchodní den, ne kalendářní“
  v AGENTS.md; jeden predikát `settle.is_trading_session` (sjednocen se `sentwaves.is_weekday`, převedeny
  i `emrespect`, `deepbars`, `scenario_auto._settle_close`, `marketclock.outside_us_rth`, `gexforward`
  a frontend `expiry.ts` → `tz.isTradingSessionIso`), svátky #1308.
  Srovnání dvou archivů = jen strany měřené v obou. Dvě poučení z revize opravy: (1) dedup odvozených bodů
  podle klíče dne musí chybně vzniklý řádek umět nepočítat (`fa_validation.exists` ignoruje víkendový
  `next_day`), jinak oprava kódu starou chybu zakonzervuje — pondělní bod 28. 9. by se vůbec nespočítal;
  (2) brána zavřeného trhu u hlídače s epizodou hodiny **pozastavuje**, stav epizody (ohlášení, pokusy
  remediace #877 C) nuluje jen skutečný návrat — vynulování při zavření by z trvalé poruchy udělalo novou
  epizodu s dalšími zásahy každou denní pauzu.

- **2026-09-27 — automatický scénář dne vznikal v sobotu i v neděli s „termínem settle“ toho dne (#1309).**
  Scénáře #6/#7 (sobota) a #8/#9 (neděle 15:15 CEST) přišly jako upozornění `scenario_created`
  a vyhodnocovač by nedělní uzavřel v neděli 22:15 CEST, na settle neexistující seance. Příčina:
  `trading_session_date(now)` vrací o víkendu kalendářní den bez seance a generátor i vyhodnocovač na něj
  bez otázky navázaly `us_open_ts` a `settle_ts`. Sobotní #6/#7 skončily bez výsledku (a nedělní #8/#9
  by skončily stejně) jen proto, že víkendové okno do 20:00 UTC nemá bary — nedělní partice začínají
  otevřením Globexu ve 22:00 UTC; s bary by šla falešná trefa do track recordu. Sobotu našla diagnóza #1307,
  neděli uživatel. → Kdo na den váže open, settle nebo publikaci, ptá se
  `settle.is_trading_session`, ne jen `trading_session_date`, a má test na sobotu i neděli. Starší řádky
  se neopravují ručně: kód je uzavře bez výsledku a statistiky je vynechají. Čtvrtý výskyt vzorce
  „kalendářní den místo obchodního“ po #1241 (PDC z nedělní partice), #1307 (okno OI pro každý
  kalendářní den) a `/oidelta` pondělí proti neděli (#1309 bod 3); od #1309 části 2 je to pravidlo v AGENTS.md.

- **2026-09-26 — „významná zpráva“ měla dvě definice a klasifikátor s lidmi nesouhlasil (#1293, #1305): regex nad slovy kdekoli.**
  Předobchodní souhrn vypsal mezi „zásadními“ článek o dani z mezd lékařky a tarifní refundaci drobné firmy;
  měření na 400 zprávách (dva hodnotitelé, κ 0,81) dalo mezi importance a relevancí κ ≈ 0,05 a importance 3
  zásadní u 9 % zpráv. Příčiny: spouštěč stačil kdekoli v titulku i shrnutí (zmínka ≠ předmět), falešné shody
  (`opec` v „alopecia“, `cpi` v doméně, Colin Powell), zdroj se nerozlišoval (Yahoo i CNBC = `rss_news`)
  a kalendář dostával importance regexem (High PPI → 1), takže upozornění četlo `raw.impact` a graf
  `importance` — dvě definice téhož. Odhalilo to až ruční hodnocení vzorku, testy regexy jen potvrzovaly.
  → Pravidlo, na kterém stojí alert, filtr nebo model, **změřit proti lidskému hodnocení** (vzorek se
  shodou hodnotitelů, holdout z jiného týdne) a držet ho **jednou čistou funkcí** volanou všude
  (`compute/news_significance.py`, ADR-0045); golden test nese i vzory chyb, ne jen šťastné případy.
  Při reklasifikaci pozor na vedlejší efekt: vlastní pokrytí téže události by zkontaminovalo reakci
  (proto K1 — jen jiná kategorie).
  Recenze pak našla tři pasti za samotnou klasifikací: (1) řádky FF jednoho releasu (CPI m/m, Core, y/y)
  mají tentýž čas i výnos a model je počítal jako nezávislé vzorky — gate se otevíral na pseudoreplikacích
  (stejně deferred zprávy jedné uzavírky); (2) klouzavé okno vah filtrovalo `computed_at`, takže hromadné
  zpětné vyhodnocení po reklasifikaci by 90 dní vážilo historii; (3) regex `\bmay\b` jako názor chytal měsíc
  May, `\d+ … stocks` cenovou úroveň „$100 as stocks“, typografický apostrof míjel „here's“.
  → **n = počet nezávislých měření** (slučovat vzorky se stejným měřeným oknem), okna kalibrace podle času
  události, ne času zápisu; regexy na slova s více významy testovat i na protipříkladech.

- **2026-09-26 — „konečné“ rozhodnutí hypotézy a „pevná“ kritéria platila jen v textu ADR (#1296): přepočet od nuly a sdílené konstanty.**
  `release_hypotheses` se po každé změně počítala celá znovu a CLI backfill přeměřoval i živé releasy — oprava
  dat by zpětně překlopila „ověřeno“ na „ověřuje se“; M1 bral rodiny a baseline ze sdílených konstant upozornění
  a news_anomaly, takže jejich ladění by potichu změnilo registrované kritérium. → Co má být neměnné, musí
  vynutit kód: vyhodnocený úsek se přebírá z předchozího stavu (ne přepočítává), registr má vlastní kopie vstupů
  a strážní test na ně. Slib v ADR bez mechanismu, který ho drží, je nález.
- **2026-09-26 — walk-forward „prošlo“ signál, který OOS nepotvrzuje nezávisle (#1296): OOS použitý k objevu i k ověření.**
  `verdicts()` ve výzkumu releasů rozhodoval podle BH q z **celého** vzorku a chtěl „směr IS = směr celku“; celý
  vzorek ale obsahuje OOS, takže silný OOS pomohl signálu přes práh objevu a pak ho „ověřil“. Jediný „prošlý“
  signál (Core PPI → NQ 3 seance) adversariální kontrola vyvrátila; po opravě (q jen z IS p-hodnot, OOS jen
  jednostranně ve směru IS) z „prošlo“ vypadl. → Ve walk-forward stojí objev (výběr, BH, směr) **jen na IS**,
  OOS jen ověřuje; kritéria zapsat do docstringu/ADR před pohledem na výsledky a opravu metodiky označit jako
  korekci, ne ladění. Živé hypotézy (ADR-0044) mají kritéria v kódu a strážní test, ne v DB.
- **2026-09-25 — markery zpráv během dne mizely, k upozornění nešla v grafu najít zpráva (#1290): „posledních N“ jako zdroj časové osy.**
  Graf bral `/news?limit=100`; při toku ~150 zpráv/h to pokrylo ~30–40 min (upozornění ve 14:03 na zprávy
  z 13:00, graf začínal 13:42). Tamtéž druhá tichá chyba: marker se pároval na osu shodou popisku `HH:MM`,
  takže na 5m a delších TF zmizela každá zpráva mimo hranici koše a zprávy z pauzy CME neměly sloupec.
  Odhalil to proklik z upozornění, ne test (testy měly osu 1m a pár zpráv). → Data časové osy načítat
  podle rozsahu (seance), nikdy „posledních N“ — strop počtu bez hlídání rozsahu tiše uřízne čas; na osu
  mapovat časem (`bucketStartsMs` + binární hledání), ne formátovaným popiskem; testovat i na 5m a s > 100 řádky.
  Review téže změny našlo tentýž symptom podruhé: cache „den načtený" živého dne přežila pauzu odběru
  (vypnutá vrstva, Daily, jiný den) a minutové dotažení bralo jen posledních 30 min. → Příznak „načteno"
  u živých dat platí jen po dobu odběru: při (znovu)zahájení odběru zneplatnit, dotažení počítat od
  posledního úspěšného, ne od „teď"; test s pauzou delší než okno.
- **2026-09-25 — „souhrn zpráv za víkend po otevření“ byl hotový a uživatel ho zahodil (#1291 Q2): trader potřebuje čas se připravit.**
  Upozornění, které přijde až s gapem, popisuje, co už se stalo; hodnotu má jen před otevřením. Druhá past
  v téže změně: aktualizace 15 min před nedělním openem padá na 23:45 = do výchozích tichých hodin Telegramu
  (23:00–06:00), takže by na mobil nikdy nedošla. Třetí: replay v měřicím skriptu měl vlastní SELECT zpráv
  bez `sentiment_dir` a simulace ukázala sklon ⚪ u všech 35 víkendových zpráv — vypadalo to jako chyba
  klasifikátoru. → U upozornění vázaného na trh se nejdřív ptát, **kdy** má být užitečné (před/po události),
  a jeho čas odvozovat od otevření v CT (`settle`/`marketclock`), ne od pevných hodin; nový druh alertu
  s pevným časem proti tichým hodinám ověřit; replay stavět na týchž sloupcích a mapování jako job
  (`EVENT_COLUMNS`, `event_from_row`), ne na kopii dotazu. Čtvrtá: slučování téže story podle času
  (první výskyt zůstane) vydalo kopii z opožděného feedu s dřívějším `ts_event` za novou zprávu
  aktualizace → u dedupu napříč etapami musí mít přednost už ohlášená zpráva, ne nejstarší čas.
- **2026-09-25 — živý GEX žebřík a podíl outright stály až hodinu (#1273): ruční výčet polí ve flushi.**
  Handlery WS kanálů `ladder.*` (#244) a `printvol.*` (#1007) data ukládaly, ale flush v `useDayData`
  vyjmenovával pole `LiveMinute` ručně a nová pole do výčtu nikdo nepřipsal — dva měsíce bez chyby
  v konzoli, UI jen drželo poslední známý stav do hodinového refetche. Druhá vrstva: `appendMinute`
  při každém flushi přepočítával kumulativ tisků od předchozího sloupce, jenže kanály jedné minuty
  chodí ve 2–4 flushích (a finální bar M−1 až v cyklu M) → pozdější flush přírůstek smazal.
  → Přenos „všech polí" psát rozprostřením (`...partial`), ne výčtem; operace nad minutou musí být
  bezpečné pro opakovanou aplikaci téže minuty s jinou podmnožinou kanálů (test na to je v #1273).

- **2026-09-24 — signály NQ jen z jednoho dne (#1265): gate LB > 0,5 bez velikosti efektu.**
  81 z 86 signálů NQ za 10 dní vzniklo 23. 9. z bucketu OTHER/imp 1 (n 13 464, LB 0,5004, Ø −0,03 bp);
  po nočním přepočtu LB klesl na 0,4997 a signály ustaly. Odhalil to mezistav H1 (#1264): 86 řádků
  mělo jen 42 různých `ts` a 85 bylo z jednoho dne. → Statistická brána nad velkým n potřebuje i práh
  velikosti efektu (ADR-0042); vyhodnocení track recordu dělat nad shluky (stejný `ts` = jedno
  pozorování) a hlídat počet seancí, ne jen n řádků.
- **2026-09-10 — špatné poučení z rozboru seance: „širší stop".** Short vyhozen o 2 body před pohybem
  k cíli → návrh stopů „za další konfluenci". Stop chrání kapitál a nezvětšuje se podle toho, co trh
  udělal; další zeď může být stovky bodů daleko a R:R zmizí.
  → Poučení = přísnější vstup (potvrzení odmítnutí 5m close, pullback k hraně, R:R ≥ 2), stop těsný.
- **2026-08-10 — hromadný DELETE smazal uživateli jeho anotace.** Po testu na devu smazány všechny
  anotace symbolu za den; mezi nimi dvě nakreslené uživatelem mezitím, záloha PG 3 dny stará.
  Uživatel pracuje v aplikaci **souběžně** — cokoli zapíšu nebo smažu, je jeho živý stav.
  → Zapamatovat `id` (a payload) záznamů, které vytvořím, po testu mazat výhradně ta. Nikdy hromadný
  DELETE nad kolekcí, kterou jsem celou nevytvořil. Persistovaný stav UI (localStorage) po testu
  vrátit a říct to.

## 4. Co funguje (vzory k opakování)

- **Diagnostika, která rozliší příčiny**: `status_fields()` u CVD rozlišil tři stavy, které v datech
  vypadaly stejně; ERROR log DXLinku odhalil problém starý měsíce.
- **Sonda mimo produkci**: hypotézu ověřit v izolované subskripci/skriptu (`scripts/*_probe.py`),
  než sáhnu na běžící systém.
- **Test proti neopravenému kódu** (`git stash` + spustit test): potvrdí, že test chytá to, co má.
- **Tag `pre-<issue>` na image před deployem** enginu — vratná cesta za sekundy.
