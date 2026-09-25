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

- **2026-09-25 — spouštěč Task Scheduleru „23:05“ by v zimě běžel ve 22:05 (#1277): `New-ScheduledTaskTrigger` ukládá offset.**
  `StartBoundary` vzniká jako `…T23:05:00+02:00` = „synchronizovat napříč časovými pásmy“ → Windows drží čas v UTC
  a po konci letního času úloha poběží o hodinu dřív (u pauzy Globexu = do otevřeného trhu). Týká se i walk-forward
  a docker úklidu (#1278). Druhá past téhož dne: plánovaný čas 23:05 padl přímo do okna deploye (logy enginu
  23:04–23:08), přestože komentář tvrdil „deploy 23:15“. → Po `New-ScheduledTaskTrigger` přepsat
  `StartBoundary = ([datetime]$t.StartBoundary).ToString('s')`; čas úlohy, která zastavuje Docker, ověřovat podle
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
