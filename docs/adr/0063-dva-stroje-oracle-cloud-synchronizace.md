# ADR-0063: Dva stroje (PC a Oracle Cloud ARM), vždy jeden aktivní — role, synchronizace na záloze, přepnutí, zálohy za aktivním strojem

- **Stav:** **přijato 19. 11. 2026** (vlastník v chatu k PR #1861: „1. Schvaluji, 2. A, 3. A,
  4. Ano“ — ADR jako celek, přesun souborů chybějících ve snímku zdroje do karantény (bod 4),
  zpožděná data IBKR na serveru ne (bod 8, varianta A), denní restart Gateway v 16:05 CT
  (bod 11)). Body zapisují rozhodnutí vlastníka z 12., 13. a 19. 11. 2026 v epice #1094 (sekce
  Rozhodnuto). Plán epiky vlastník schválil 19. 11. komentářem „schvaluji“ (#1094
  issuecomment-7021302156).
- **Datum:** 2026-11-19
- **Mění:** SPEC kap. 8 „Bezpečnost: Vše lokální; API bind na localhost“ (bod 9). Dočasně
  R5 a R1 na serveru do zaplacení subskripce druhého username (bod 8, konec po #1856).
  Doplňuje ADR-0024 (dodatek 19. 11.: navázání denního stavu), ADR-0047 bod 1 a 4 (bod návratu,
  aditivní změny) a ADR-0029 (věčný archiv se přenáší a zálohuje, nemaže; komprese #1855).
- **Implementace:** epika #1094, Fázovaný plán: F1 #1798 (image arm64), F2 #1799, F3 #1800,
  F4 #1801 (záloha a obnova), F5 #1802 (server), F6 #1803 (brána), F7a #1804, F7b #1805
  (zálohy za aktivním), F8 #1567, F9 #1806, F10 #1807, F11 #1808 (přepnutí), F12 #1809
  (migrace), F15 #1847, F16 #1848, F17 #1849, F18 #1850, F19 #1851, F20 #1852, F21 #1853,
  F22 #1854, F23 #1855, F24 #1856, F25 #1857, F26 #1858. Tento ADR je F0 #1797.
- **Podklad:** prověrka bezpečnosti #542 (ADMIN-MANUAL kap. 14), příprava serveru #1099
  (`compose.server.yml`), měření v #1094 (Kontext a tvrdá data k 19. 11.).

## Kontext

Vlastník chce GEXLens provozovat i na Oracle Cloud a dál ho moci kdykoli pustit na PC
(Zadání #1094, 10.–19. 11.): na aktivním stroji vše jako dnes, druhý stroj jen pro čtení,
přepnutí tlačítkem v aplikaci skoro v reálném čase, data a zálohy za aktivním strojem, Oracle
plně nahraditelný a návrat k dnešnímu provozu kdykoli.

Tvrdá data (19. 11. 2026):
- **Oracle Always Free:** `VM.Standard.A1.Flex` 1 500 OCPU h + 9 000 GB h měsíčně, což pro
  Always Free tenancy odpovídá **2 OCPU / 12 GB**; 200 GB block storage včetně boot volume;
  10 TB odchozích dat měsíčně. Instanci Oracle smí zastavit (reclaim) jako **nečinnou**, když za 7 dní
  platí současně: CPU p95 < 20 %, síť < 20 % a u A1 paměť < 20 % (dokumentace Oracle
  „Always Free Resources“, ověřeno 19. 11.).
- **Zátěž:** aktivní stack 5,3 GB RAM (9. 9., `docker stats`) ≈ 44 % z 12 GB; v prohlížení
  odhadem ~10 % (změří F6 #1803). PC i5-10300H 4C/8T s limity `engine` 2.0 a `news-engine` 1.5
  CPU; Oracle má 2 jádra Ampere → **CPU je hlavní riziko** (brána F6).
- **Data:** `data/` 15 GB (`snapshots/` 12 GB, `derived/` 1 GB, `logs/` 2 GB), růst
  **~5,2 GB/měs.** (ES 2,70 + NQ 2,19 + derived 0,35 GB za 31 dní; ADR-0029 počítal ~6 GB/rok).
  D: 58 GB volných z 259 GB, externí disk T7 (E:) 1 224 GB volných z 1 863 GB.
- **IBKR:** engine žádá jen živá data (`reqMarketDataType(1)`, `ibkr/connection.py:322`)
  a zpožděná odmítá (10167 je chyba spojení, `DELAYED_DATA_ERROR_CODES`). Bez subskripce
  chodí per request 354 (`ibkr/subscription.py`) a data nepřijdou, spojení zůstane
  `Connected`. Přihlášení stejným username odhlásí jinou relaci (ADMIN-MANUAL kap. 13.3).
- **Image jen amd64** (`.github/workflows/images.yml` bez `platforms:`); base image jsou
  multi-arch, `gnzsnz/ib-gateway:stable` má `linux/arm64` jako experimentální.
- **Záloha dnes jen PG** (`backup.py`, noční `backup-postgres-nightly.ps1` #1566); parquety,
  které ADR-0029 drží jako nenahraditelné, nezálohuje nic.
- **Past #446/#992:** host a port IBKR jsou uložené v PG a přebíjejí `.env`; obnova dumpu
  z jiného stroje by je přepsala.

## Rozhodnutí

### 1. Poskytovatel a velikost
Oracle Cloud Free Tier, home region Frankfurt, účet **Always Free bez PAYG** (Rozhodnuto 13. 11.:
provoz zdarma). Jedna instance `VM.Standard.A1.Flex` 2 OCPU / 12 GB, Ubuntu 24.04 aarch64,
boot volume 200 GB (celý limit, server drží data i repozitář záloh, bod 11). Zdroj, který není
*Always Free-eligible*, se nezakládá. **Žádná umělá zátěž** proti nečinnosti: aktivní stack má
paměť ~44 %, stroj v prohlížení řeší bod 11.

| Varianta | + | − |
|---|---|---|
| **A Oracle Always Free (zvoleno)** | 0 Kč; 12 GB RAM pokryje aktivní stack s rezervou; ARM image vzniknou v CI | 2 OCPU je pod PC; bez podpory a s nižší prioritou kapacity; pravidlo nečinnosti; IB Gateway na arm64 experimentální |
| B Oracle PAYG s víc OCPU | víc CPU, vyšší priorita | placené; vlastník zamítl 13. 11., jen po bráně F6 |
| C plán B netcup RS 1000 G12 (x86) | x86 jako PC, Gateway bez rizika arm64 | placené, 8 GB RAM; použije se jen při nesplnění brány F6 se stejnými skripty |

### 2. Dva rovnocenné stroje, vždy jeden aktivní
PC a server mají stejný kód, image a skripty. Role stroje je **aktivní** nebo **prohlížení**:

| | Aktivní | Prohlížení |
|---|---|---|
| Sběr (engine, news-engine, IB Gateway / TWS) | ano | stojí |
| Zápis uživatelských dat (deník, anotace, verdikty, setupy, Settings) | ano, vše jako dnes | ne: API vrátí 409 „zapisuj na aktivním stroji“, UI prvky vypne |
| Noční joby, obchodní upozornění Telegram, zdroje zpráv | ano | ne |
| Hlídače výpadku dat | ano, s bránou `is_market_closed` | ne (data se na stroji neočekávají) |
| Automatická záloha | ano | ne |
| Upozornění noční údržby a dohledu | ano | ano |
| Čtení (heatmapa, replay, Stats, backtesty) | ano | ano, data z poslední synchronizace |

Zdůvodnění (Rozhodnuto 19. 11.):

| Varianta | + | − |
|---|---|---|
| **A dva stroje, jeden aktivní (zvoleno)** | PC funguje jako dnes; server přibývá vedle něj; návrat k dnešnímu provozu = server zastavit | přepnutí a synchronizace se musí postavit (body 4–6) |
| B Oracle jediný sběr, PC jen prohlížeč (12. 11.) | jednodušší, jeden zdroj pravdy | PC by bez obnovy nesbíralo; nahrazeno 19. 11. („dál ho moci pustit na PC“) |
| C dva souběžné sběry | žádné přepínání | data PG (setupy, deník, nastavení) nejdou bezpečně sloučit; kvóty zdrojů zpráv (Alpaca WS 1 spojení, Finnhub, Gemini) a Telegram by se zdvojily |

### 3. Stav a nastavení stroje mimo synchronizaci
Role, epocha (bod 6), adresa IBKR (host, port), přepínač „IBKR na serveru“ (bod 8) a adresa
druhého stroje žijí v `data/local/` na každém stroji zvlášť. Nezálohují se do synchronizace
a obnova je nepřepíše (řeší past #446/#992, #1847, #1848). **Výchozí role bez stavu = aktivní**,
takže PC se po nasazení chová jako dnes; server dostane roli prohlížení už při bootstrapu
(#1802).

### 4. Synchronizace = záloha + obnova
Synchronizace stojí na téže záloze, kterou stroj dělá každou noc (restic):
1. snímek **teď** na zdroji: celá DB (`pg_dump`) a `data/` bez `logs/` a bez `local/` (bod 3),
   manifest se sha256 (obsah zálohy #1799; úplný výčet vyloučených částí včetně `ticks/`,
   `backup/tmp/` a `*.tmp` je v ADMIN-MANUAL kap. 6);
2. přenos jen rozdílu (deduplikace restic);
3. obnova na cíli s **výměnou DB**: nová DB vedle běžící, ověření, přejmenování; předchozí DB
   zůstává jako pojistná a nemaže se bez dumpu v repu s tagem `pred-obnovou` (#1800).
   `data/` se přepíše změněnými soubory. Soubor, který ve snímku zdroje chybí (zdroj ho
   přesunul, např. do `quarantine/`), se na cíli **nemaže, ale přesune** do
   `quarantine/sync-<datum>/` a vypíše ve výsledku synchronizace. Jinak by se přesunutá
   podezřelá partice na cíli vrátila do UI (Rozhodnuto 19. 11., varianta A; zamítnuto B nechat
   na místě a C smazat; #1800, #1851).

Kanálem je **repozitář restic na serveru** (sftp v tailnetu), takže synchronizace nezávisí
na úložišti záloh PC (bod 7):
- **„Stáhnout data ze serveru“** (server aktivní, PC v prohlížení): snímek do repa serveru →
  PC obnoví přímo z něj;
- **„Nahrát data na server“** (PC aktivní, server v prohlížení): PC zálohuje přímo do repa
  serveru → server obnoví ze svého repa.

Podmínky: obě strany ve **stejné verzi všech služeb** (engine, news-engine, api, frontend),
cíl není aktivní a nemá novější sběr než zdroj; jinak se synchronizace odmítne s důvodem.
Přenosy spouští vždy PC (služba `ops`, #1804, #1851). Každá synchronizace je zároveň test
obnovy.

| Varianta | + | − |
|---|---|---|
| **A záloha + obnova přes restic (zvoleno)** | jeden mechanismus pro zálohu, synchronizaci i nový server; obnova se zkouší při každé synchronizaci; přenos jen rozdílu | snímek a obnova trvají minuty, ne sekundy (řeší předsynchronizace v bodě 5) |
| B vlastní protokol (rsync partic + proud pg_dump) | méně vrstev | druhá cesta k týmž datům vedle zálohy; obnova se nezkouší; konzistence PG × parquety ručně |
| C průběžná replikace (PG streaming + smyčka rsync) | cíl stále skoro aktuální | oba stroje musí být stále online (PC se vypíná); replika PG je jen pro čtení až do povýšení; složitý provoz bez přínosu pro ruční přepnutí |

**Jiná verze PC v prohlížení** se srovná tlačítkem „Aktualizovat PC na verzi serveru“ v bloku
Stroj; požadavek do 1 min provede úloha hostitele PC, protože kontejnery nemají docker socket
(#542, #1807, #1852).

| Varianta | + | − |
|---|---|---|
| **A tlačítko v bloku Stroj (zvoleno 19. 11.)** | vlastník rozhoduje, kdy se PC mění; jedno kliknutí | úloha hostitele navíc |
| B automaticky při startu PC | bez kliknutí | změní verzi PC bez vědomí vlastníka, i uprostřed vývoje |
| C příkaz v terminálu | nic nového v UI | vyžaduje terminál, z aplikace nejde |

### 5. Přepnutí aktivního stroje
1. **Předsynchronizace** za běhu zdroje (bez mezery v datech);
2. zdroj do prohlížení: dopíše minutu, uloží checkpoint denního stavu (dodatek ADR-0024),
   na serveru se zastaví Gateway;
3. **dorovnání** posledních minut a DB (malý rozdíl po předsynchronizaci);
4. cíl aktivní: naváže denní stav (#1849), naběhne přes tasty a na IBKR přejde po přihlášení;
5. ověření (`/instance`, první minuta zapsaná na cíli).

Mezera v datech **≤ 4 min** (cíl ~2–4 min). Dorovnání (krok 3) proto bere jen partice dnešní
seance (obě UTC partice, #1002) a DB, ne průchod celého `data/`: ten by na bind mountu Docker
Desktop (~20 ms na soubor, AGENTS.md) trval minuty. Jak výběr udělat (restic na hostiteli,
nebo seznam cest), změří a rozhodne #1808. Chyba v kterémkoli kroku = zdroj se vrací ke
sběru, cíl zůstává v prohlížení. Přepíná se **jen z PC ručně** (Rozhodnuto 19. 11.).
Přepnutí v pauze Globexu nebo o víkendu je doporučení, ne blokace; minuty mezery se
nedokreslují (ADR-0024 bod 2).

| Varianta | + | − |
|---|---|---|
| **A jen z PC ručně (zvoleno)** | přenosy spouští PC; vlastník vidí průvodce a výsledek | výpadek serveru mimo domov se řeší až u PC (hlásí ho dohled #1806) |
| B automatické převzetí sběru PC | bez zásahu | riziko dvou aktivních při výpadku spojení; PC bývá vypnuté |
| C přepínání z mobilu | odkudkoli | mobil přenos nespustí (bod 4); bez průvodce na PC |

### 6. Pojistka proti dvěma aktivním strojům: epocha
- Každá aktivace nastaví novou epochu = **max(vlastní, poslední známá druhého) + 1**.
- Aktivní stroj, který uvidí druhý aktivní stroj s **vyšší** epochou, se sám přepne do
  prohlížení a pošle Telegram; při **shodné** epoše ustoupí server.
- Server začíná v prohlížení už z bootstrapu (#1802); výchozí „aktivní bez souboru“ platí
  pro PC (bod 3).
- Vynucené přepnutí při nedostupném druhém stroji (výpadek Oracle) zvedne epochu tak, že
  se vrácený stroj po spojení stáhne do prohlížení (#1808).

### 7. Zálohy za aktivním strojem
Noční restic snapshot dělá **jen aktivní stroj**, služba `ops` je stejná na PC i na serveru
(#1804). Tajemství v záloze nejsou nikdy, jen jejich jména.
- **Aktivní PC** → zálohuje do repozitáře na **T7 (E:)**, a to **jen do adresáře, který povolí
  a nastaví vlastník** (`GEXLENS_BACKUP_REPO`). Bez nastavení se nezálohuje nic; kořen disku
  se do kontejneru nepřipojuje; adresář ani repo služba nezakládá; mimo adresář se na T7
  nezapisuje. Odpojený T7 = noc se přeskočí, po 72 h bez snapshotu Telegram.
- **Aktivní server** → zálohuje do svého repa; PC v 7:00 (nejelo-li, hned po zapnutí)
  zkopíruje snapshoty do repa na T7, je-li připojený. Server pošle Telegram, když je kopie
  starší než 72 h (#1805).
- Časy jsou pevné v pásmu Europe/Prague (Rozhodnuto 19. 11., varianta A): záloha 23:15,
  kopie 7:00. Bez průběžných kontrol; přeskočená záloha 23:15 se během dne nedohání, kopie
  7:00 proběhne po zapnutí PC, nejelo-li v 7:00; upozornění po 72 h. V týdnech posunu letního
  času padne 23:15 do otevřeného trhu, dnešní partice doplní další noc (#1804).
- **Ručně:** Settings → Úplná záloha → jeden `.tar` s `RESTORE.md` a manifestem, místo vybírá
  vlastník (#1799, #1801). Cloud až dojde místo (#1811).

| Varianta (úložiště PC) | + | − |
|---|---|---|
| **A repo na T7 (zvoleno 19. 11.)** | 1,2 TB volných; D: se neplní; záloha mimo systémový disk | odpojený T7 = přeskočená noc (Telegram po 72 h) |
| B repo na D: + kopie na T7 | záloha i bez T7 | D: (58 GB) zaplní za ~4 měsíce: repo ~ data 15 GB a s daty roste ~2 × 5,2 GB/měs. |

| Varianta (časy) | + | − |
|---|---|---|
| **A pevné časy (zvoleno)** | předvídatelné; bez zátěže přes den | po vynechané noci až další den |
| B průběžné kontroly a dohánění | méně vynechaných nocí | služba běží a hlídá T7 celý den; zápis na T7 v nečekaný čas |

### 8. IBKR na dvou strojích
- **PC = hlavní username** (TWS / IB Gateway na PC jako dnes), **server = druhý username**
  (#737, `IBKR_DATA_USERID` v `compose.server.yml`). Hlavní username na server nepatří:
  přihlášení na serveru by odhlásilo PC i mobil (ADMIN-MANUAL kap. 13.3).
- **Do zaplacení subskripce druhého username IBKR market data serveru nedodá:** engine žádá
  jen živá data, bez subskripce přijde 354 a zpožděná data engine odmítá (10167). Market data
  má převzít tasty fallbackem (křížová kontrola, spot; ADMIN-MANUAL kap. 11), jen se zapnutým
  tasty; že k převzetí dojde i při spojení `Connected` bez dat, ověří F6 krok A (#1803). Tam se
  ověří i discovery řetězu přes IBKR bez subskripce; bez ní platí cache discovery ≤ 14 dní
  a jen do rollu.
- Se zapnutým IBKR bez subskripce bude detektor shluku 354 (#417) hlásit každou seanci a řetěz
  poběží přes `chain_fallback`. Kdyby to F6 krok A potvrdilo, nechá se do zaplacení IBKR na
  serveru vypnuté přepínačem níže.
  **Dodatek 24. 11. 2026 (F6 krok A, #1803):** krok A to potvrdil (354 u každého požadavku,
  OI z IBKR 0 %; ADMIN-MANUAL kap. 14), vlastník přesto zvolil **IBKR na serveru do zaplacení
  zapnuté** (Rozhodnuto 24. 11. v #1094, varianta A): discovery řetězu z IBKR funguje i bez
  subskripce a je potřeba pro roll (#1809), zprávy brokera chodí a po zaplacení data poběží
  hned. Cena: ~320–640 chyb 354 za minutu (Gateway ~25 % jádra, ve špičce sweepu až ~92 %)
  a upozornění #417 ve zvonku serveru nejvýš 1× za 15 min. Varianta „jen discovery a zprávy
  bez kotací“ je nový kód (přepínač níže, #1847) — issue, pokud subskripce nebude do rollu.
- **Dočasná odchylka od R5** (IBKR primární zdroj, ADR-0025) **a od úplnosti dat R1** (bez
  kumulativního denního objemu a zpráv IBKR) **jen na serveru** v roli aktivní. Konec:
  vlastník zaplatí subskripci druhého username a #1856 přeměří bránu s IBKR. Do té doby
  server sbírá degradovaně (fallback tasty, ADMIN-MANUAL kap. 11); PC je beze změny.
- **Přepínač „IBKR na serveru: zapnuto / vypnuto“** (nastavení stroje, bod 3): vypnuto =
  Gateway stojí a server jede jen přes tasty s limity fallbacku (denní objem, cache discovery,
  zprávy IBKR) (#1850, #1853).
- Market data lines: nové 0, limit 100 a `batch_size` beze změny (ADR-0001 bod 4).

| Varianta | + | − |
|---|---|---|
| **A druhý username (zvoleno 19. 11.)** | PC ani mobil se serverem neruší; souběh relací | do zaplacení subskripce server bez dat IBKR (dočasná odchylka R5/R1) |
| B týž username s tlačítkem „Znovu připojit“ | data IBKR hned | každé přihlášení serveru shodí PC nebo mobil; vlastník přepíná ručně |
| C server trvale bez IBKR | nic se neplatí | trvalá odchylka od R5 a R1 na serveru |
| D přijímat zpožděná data IBKR (typ 3) do zaplacení | data IBKR hned a zdarma | zpožděné hodnoty by se v téže minutě míchaly s živými z tasty a heatmapa by ukazovala minulost jako přítomnost (point-in-time); engine zpožděná data dnes odmítá a musel by nést zpoždění v každém bodě → **zamítnuto 19. 11.** (vlastník: A) |

### 9. Přístup jen v tailnetu (odchylka od SPEC kap. 8)
Tailscale na serveru, PC i mobilu. UI obou strojů je dostupné jen v tailnetu přes
`tailscale serve` (HTTPS na `*.ts.net`, `GEXLENS_ALLOWED_ORIGINS` podle adresy).
`GEXLENS_BIND_ADDR` zůstává loopback, na veřejné IP je **0 portů včetně 22**, SSH jen přes
Tailscale; ověření vždy `nmap` zvenku (ADMIN-MANUAL kap. 14, past s řetězcem `DOCKER`).
SPEC kap. 8 „Vše lokální; API bind na localhost“ tím na serveru platí ve smyslu „jen v síti
vlastníka“: loopback bind beze změny, přístup zvenku jen přes tailnet. Žádná telemetrie
beze změny. Veřejná IP s 80/443 a Let's Encrypt ani Cloudflare Access nevznikají.

### 10. Nahraditelnost a návrat
- **Nový server** = `bootstrap.sh` (#1802) + `git clone` + obnova ze zálohy nebo
  synchronizace. Totéž platí pro plán B (x86) se stejnými skripty.
- Image amd64 + arm64 z jednoho workflow (#1798); nasazení stejným tokem jako dnes (PR → CI →
  image → nasazení v pauze Globexu po dotazu vlastníka, AGENTS.md a ADMIN-MANUAL kap. 3); oba
  stroje ve stejné verzi všech služeb (#1807).
- **Návrat k dnešnímu provozu:** do první migrace (#1809) stačí server zastavit, PC běží jako
  dnes; potom „Přepnout sběr na PC“ v bloku Stroj. Změny schématu a stavu jsou aditivní
  (ADR-0047 bod 4), výchozí role PC bez stavu je aktivní (bod 3), `data/` na PC se nemaže.

### 11. Provoz (Rozhodnuto 19. 11., krok 2 v #1094)
- **Nečinnost Always Free: hlídat a připomenout.** F6 změří paměť serveru v prohlížení. Pod
  20 % → po 5 dnech aktivního PC Telegram a v bloku Stroj „server bude za 2 dny nečinný“
  (#1806, #1852). Zastavenou instanci vlastník spustí z konzole, nouzově nový server (bod 10).

  | Varianta | + | − |
  |---|---|---|
  | **A hlídat a připomenout (zvoleno)** | žádná umělá zátěž; vlastník ví předem | při ignorování připomínky Oracle instanci zastaví |
  | B server v prohlížení počítá (backtesty, přepočty) | instance nepůsobí nečinně | umělá zátěž proti smyslu Always Free; CPU mimo potřebu |
  | C neřešit | nic navíc | zastavení instance bez varování |

- **Týdenní IB Key na serveru:** plný login **neděle 22:30** (TZ Europe/Prague,
  `TWS_COLD_RESTART`); nepotvrzený push → ještě 2 pokusy po ~10 min (**nejvýš 3 pushe**), pak
  Gateway stojí a server jede přes tasty. Po otevření trhu Telegram a tlačítko „Přihlásit IBKR
  na serveru“ (#1850). Denní restart bez hesla zůstává **vždy v pauze Globexu**: dnešní
  `AUTO_RESTART_TIME` 23:05 Europe/Prague padne v týdnech posunu letního času do otevřeného
  trhu, proto ho #1850 převede do pásma America/Chicago (16:05 CT, Rozhodnuto 19. 11.). Nedělní
  plný login zůstává
  22:30 Europe/Prague.

  | Varianta | + | − |
  |---|---|---|
  | **A neděle 22:30 (zvoleno)** | přihlášení před otevřením seance 17:00 CT; nejvýš 3 pushe | push v neděli večer |
  | B pondělí 7:00 | push ráno | Globex běží od neděle 17:00 CT, server by do 7:00 jel bez IBKR |

- **Brána F6 na tasty** (#1803): v RTH sweep ≤ 90 s, 0 přetažených minut, CPU p95 a RAM
  ≤ 85 %. Zátěž s IBKR přeměří #1856 po zaplacení subskripce. Nesplnění = varianty pro
  vlastníka: plán B netcup x86 se stejnými skripty, placené OCPU, menší zátěž.

  | Varianta | + | − |
  |---|---|---|
  | **A brána na tasty teď, IBKR po zaplacení (zvoleno 19. 11.)** | server a migrace (#1809) nečekají na platbu | zátěž IBKR na 2 OCPU změří až #1856; do té doby server sbírá degradovaně (bod 8) |
  | B brána až s IBKR po zaplacení | měří plnou zátěž najednou | server i migrace stojí do platby |
  | C bez brány | nic se neměří | přetažené minuty by se ukázaly až v provozu (ztracená minuta ES i NQ) |
- **Místo:** boot volume 200 GB; server drží data i repo (~2× data), růst ~5,2 GB/měs. →
  měření komprese parquetů #1855 (dodatek ADR-0029) před první zálohou do repa (#1804),
  hlídka místa #1567.

## Důsledky
- PC zůstává do první migrace (#1809) beze změny chování: výchozí role aktivní, stejné joby,
  stejné úlohy hostitele. Úlohy hostitele se řídí rolí (#1857).
- Každá služba, která zapisuje nebo posílá upozornění, se ptá na roli stroje (#1847, #1848);
  nový zapisovatel bez dotazu na roli je chyba.
- Data se nemažou: výměna DB nechává pojistnou DB, `data/` se jen přepisuje změněnými
  soubory, data serveru z doby testů se před migrací odloží stranou (#1809).
- Pravidla AGENTS.md platí na obou strojích: 100 lines, obchodní den (`is_trading_session`),
  hlídače dat jen při otevřeném trhu, tajemství jen jménem. Nové jsou dvě věci: hlídače dat
  běží jen na aktivním stroji (bod 2) a server do zaplacení subskripce dočasně odchyluje od
  R5 a R1 (bod 8).
- Rozhodnutí, která přijdou s měřením: komprese (#1855), brána F6 (#1803), přeměření s IBKR
  (#1856). Každé jde jako dodatek tohoto ADR nebo ADR-0029.

## Zamítnuto a mimo rozsah (YAGNI)
- souběžný sběr na obou strojích a slučování dat (bod 2 C);
- automatické převzetí sběru PC a přepínání z mobilu (bod 5 B, C);
- umělá zátěž, veřejná IP s 80/443 a Let's Encrypt, Terraform, nový compose nebo jiná DB;
- placení subskripce druhého username (rozhoduje vlastník, pak #1856);
- MCP na hostingu (#1407), cloudové sessions (#1662), Cloudflare Access.

Souvisí: #1094, #542, #737, #1099, #1566, ADR-0001, ADR-0024, ADR-0025, ADR-0029, ADR-0047.
