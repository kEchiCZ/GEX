# ADR-0025: tastytrade jako sekundární datový zdroj (revize R5)

**Stav:** přijato (2026-08-11, rozhodl uživatel po analýze v issue #610/#611)
**Odchylka:** R5 ve SPEC kap. 0 stanoví *„Datový zdroj výhradně IBKR (účet existuje);
žádné placené externí feedy"*. Tímto rozhodnutím se první klauzule přeformulovává tak,
aby připustila druhý **brokerský** účet, jehož market data jsou zdarma. Druhá klauzule
(žádné placené externí feedy) platí beze změny.

## Kontext

Aplikace naráží na tvrdé limity IBKR účtu, které nejsou důsledkem návrhu, ale stropu
subskripce:

| Limit | Hodnota | Dopad |
|---|---|---|
| Market data lines | **100** (ADR-0001 bod 4 uvádí „≥ 150", reálný strop účtu je 100) | `batch_size=80`, nelze držet víc expirací současně |
| Souběžné tick-by-tick streamy | **5** (error 10190, ADR-0001 bod 3) | Hot zóna degradována z cílových ATM±15 na ~ATM±1 C/P |
| OI tick 588 na FOP | nechodí vůbec (ADR-0001 bod 2) | Workaround přes tick 101 à 30 min + alert `oi_missing` |
| Souběh se sessions | feed je per-uživatel | Přihlášení mobilní aplikace na live přetáhne feed → error 10197, graf zamrzne |
| Rekonstrukce po pozdním startu | není | ADR-0024 ji explicitně vzdává |

R2 („Cum Δ s plnou klasifikací agresora, tick-by-tick pro hot zónu ATM ±15") je proto
dnes splněné jen formálně — SPEC 3.4 a 4.5 degradaci připouštějí, ale reálná šířka plně
klasifikované zóny je zhruba patnáctina cílové.

tastytrade distribuuje **dxFeed** přes protokol DXLink. Relevantní eventy nesou přesně
ta pole, která nám u IBKR chybějí nebo jsou limitovaná: `Summary.open_interest`,
`TimeAndSale.aggressor_side` (+ `bid_price`/`ask_price` v okamžiku tradu), `Greeks`,
`Candle` s `from_time`.

## Analýza R5

R5 obsahuje dvě klauzule s různým osudem:

- **„žádné placené externí feedy"** — **neporušeno.** tastytrade market data jsou pro
  osobní fundované non-professional účty zdarma. Pro srovnání: u IBKR platíme
  *CME Real-Time – North America* USD 1.55/měs. Nový zdroj je tedy levnější než stávající.
- **„výhradně IBKR"** — **porušeno doslovně.**

Závorka „(účet existuje)" v původním znění prozrazuje záměr rozhodnutí: nezavádět nové
náklady a novou závislost tam, kde už je funkční účet. tastytrade tomuto záměru vyhovuje —
účet existuje (margin, individual, funded, s povolenými futures), náklady jsou nulové.

Entitlement je podmíněn třemi věcmi: účet musí být **funded** (stačí libovolná částka),
klasifikovaný jako **non-professional** a mít **futures permissions („The Works")** pro
CME data. Pracuje se s předpokladem, že tyto podmínky jsou splněné; pokud by nebyly, nové
a nefundované účty mají 14 kalendářních dnů live dat, což na ověřovací spike (#612) stačí.

## Rozhodnutí

### R5 (rev. 2)

> Datové zdroje výhradně brokerské účty, které uživatel již vlastní a jejichž market data
> jsou k účtu zdarma. Žádné placené externí feedy. **Primárním zdrojem zůstává IBKR**;
> sekundární zdroj smí data pouze rozšiřovat, doplňovat nebo validovat — nikdy je tiše
> nahrazovat.

Motivací **není** „potřebujeme lepší feed". Motivací je, že několik dnes nesplnitelných
požadavků SPEC (R2 v plné šíři, Forward GEX přes víc expirací) je nesplnitelných výhradně
kvůli stropu jednoho účtu, a druhý účet ten strop odstraňuje za nulovou cenu.

### Řídící princip proti dvojí pravdě

Dva feedy téže burzy s odlišnou agregací a timestampy jsou zdroj nedebugovatelných rozporů.
Platí proto:

> **Vlastnictví se přiděluje per (datový typ × symbol), ne per hodnota. Každý datový bod má
> v každém okamžiku právě jednoho vlastníka. Hodnoty se nikdy neprůměrují ani nemergují.**

### Matice vlastnictví

| Datový typ | Primární | Role tastytrade | Režim |
|---|---|---|---|
| Spot / front future | IBKR | záloha | **fallback** (10197, stale, výpadek datové farmy) |
| Kotace řetězce | IBKR (do limitu lines) | strikes a expirace mimo dosah IBKR; při výpadku IBKR celý řetěz | **rozšíření + fallback** (rev. 2026-08-12, viz dodatek) |
| TimeAndSale / agresor | IBKR tick-by-tick (5 streamů, ATM) | zbytek ATM±15 místo Lee–Ready midpoint testu | **rozšíření** — disjunktní |
| OI | IBKR tick 101 | `Summary.open_interest` | **fallback** + logování rozporů |
| Greeks | vlastní výpočet | dxFeed `Greeks` | **validátor** → případně primární podle naměřených dat |
| Candles / backfill | IBKR historical | `Candle` s `from_time` | **doplněk** na chybějící intervaly |
| Market metrics, risk-free rate | — | jediný zdroj | **čistě nové**, nekonfliktní |

Většina řádků je „rozšíření" nebo „čistě nové", tedy bezkonfliktní z konstrukce. Skutečný
překryv nastává jen u spotu a OI.

### Pět závazných pravidel

1. **Sloupec `source` u každého záznamu** — PostgreSQL i Parquet. Bez něj nelze zpětně
   zjistit, odkud hodnota přišla, a diagnostika odchylky je nemožná. U věčného OI archivu
   (R4) to platí dvojnásob: nenahraditelná data nesmí mít neznámý původ. Záznamy bez
   `source` (historické) se interpretují jako `ibkr`.
2. **Žádné mergování hodnot.** Nikdy `(bid_ibkr + bid_tasty) / 2`. Vlastník dodá hodnotu
   celou, nebo nedodá nic.
3. **Přepnutí vlastnictví jen na hranici snapshotu, s hysterezí.** Přepnutí uprostřed
   výpočtu způsobí skok GEX uprostřed baru. Hystereze (N po sobě neúspěšných cyklů) brání
   kmitání při krátkých výpadcích; cena kmitání je vyšší než cena o pár vteřin zpožděného
   přepnutí. Hodnota N musí vyjít z měření, ne z odhadu.
4. **Shadow fáze je povinná před jakýmkoli přepnutím.** Sekundární zdroj nejdřív běží jen
   ke čtení a zapisuje odchylky do porovnávací tabulky. Prahy z pravidla 3 se odvodí z těchto
   dat. Precedens, proč se neladí od boku: šablona T5 `divergence_spring` vznikla z jediného
   živého případu, po změření měla 8,7 % úspěšnost a je vypnutá.
5. **Degradace je viditelná v UI, ne tichá.** Každý aktivní fallback a každý úsek dat
   z jiného než primárního zdroje musí být v rozhraní čitelný — stejný princip, jaký už
   platí pro pokrytí hot zóny.

### Přístupová práva a tajemství

*Doplněno 2026-08-11 na podnět uživatele (issue #620).*

Sekundární zdroj se připojuje k **brokerskému účtu s reálnými penězi**. Aplikace z něj čte
market data a nemá žádný důvod umět odeslat příkaz. Proto platí:

1. **Výhradně OAuth2 se scope `read`.** tastytrade nabízí scopy `read`, `trade` a `openid`;
   grant se vydává v Manage → Create Grant s potvrzením druhým faktorem. Scope **`trade` se
   nezaškrtne nikdy** — nejde o důvěru v kód, ale o to, že právo, které aplikace nemá, nelze
   zneužít. `openid` jen pokud ho autorizační tok vyžaduje.
2. **Přihlášení jménem a heslem přes `/sessions` je zakázané**, a to i pro jednorázový
   ověřovací spike. Session token z hesla nese plná práva účtu včetně obchodování.
3. **Tajemství jen v `.env`** (`GEXLENS_TASTY_CLIENT_SECRET`, `GEXLENS_TASTY_REFRESH_TOKEN`),
   nikdy v repu ani natvrdo v compose — stejný režim jako `GEXLENS_PG_PASSWORD`
   a `GEXLENS_API_TOKEN` podle #542. `.env.example` nese jen prázdné klíče s komentářem.
   **Dev a produkce mají každé svůj grant** na témže účtu (rozhodnutí uživatele
   2026-08-11) — viz níže.
4. **Refresh token nikdy neexpiruje** — je to trvalé tajemství, ne dočasná relace, a tak
   se s ním musí zacházet. Access token (platnost 15 min) se obnovuje automaticky.
5. **Redakce tokenů v logu doložená testem, ne docstringem.** Precedens #553: čištění tokenů
   z raw payloadů v news-engine nebylo implementované, přestože docstring tvrdil opak.
   Token nesmí projít do logu ani při výjimce a při retry.

**Poctivé omezení:** scopy jsou hrubé. `read` stále umožňuje číst zůstatky, pozice
a transakce — grant jen na market data vydat nelze. Blast radius kompromitace je tedy únik
informací o účtu, nikoli cizí obchody. To je řádový rozdíl oproti plnému session tokenu,
ale není to nula.

**Oddělené granty pro dev a produkci** (rozhodnutí uživatele 2026-08-11). Účet je jeden,
granty dva — na jednom účtu lze vydat víc grantů a odvolat je nezávisle (potvrzeno
uživatelem). Přínos:

- **nezávislé odvolání** — zabití dev přístupu (uniklý token, experiment mimo mísu) nechá
  produkci běžet,
- **atribuce** — při throttlingu je vidět, která strana ho způsobila,
- menší blast radius při úniku `.env` z jednoho prostředí.

Oddělené granty ale **neřeší kapacitu**: rate limit a kapacita subskripcí jsou
pravděpodobně vázané na účet, ne na grant, takže dev experiment může ujídat z rozpočtu
produkce a v krajním případě jí způsobit 429 uprostřed seance. Dev proto musí mít
konzervativnější limity než produkce, odvozené z hodnot změřených ve spiku (#612), ne
odhadnuté. Zda jsou limity per účet, nebo per grant, je explicitní bod měření v #612;
provozně to řeší #623.

### Konzervativní limity jsou dočasné, ne cílový stav

Opatrné hodnoty platí **jen dokud měření neproběhne**. Jakmile spike (#612) zjistí skutečné
stropy, produkční limity se **zvednou na maximum, které feed unese** — smyslem druhého zdroje
je odstranit strop, ne přinést nový. Nechat po měření zbytečně nízké hodnoty by znamenalo
zaplatit cenu integrace a nevybrat si její hlavní přínos.

Konkrétně se po měření přenastaví: počet symbolů na subskripci, počet souběžných spojení,
šířka strike bandu a počet současně držených expirací (#616), kadence REST dotazů.

**Pozor na záměnu s IBKR.** U IBKR platí opačné pravidlo — `batch_size` se zvyšovat nesmí,
protože strop účtu je tvrdých 100 market data lines a jeho překročení shodí subskripce.
Toto rozhodnutí se týká **výhradně tastytrade větve**; limity IBKR zůstávají tam, kde jsou.

Rezerva se nechává jen tam, kde ji vyžaduje sdílení účtu s dev prostředím a stabilita při
reconnectu — ne „pro jistotu". Pokud měření žádné omezení neodhalí, je správná hodnota
ta nejvyšší, která projde zátěžovým testem přes celou seanci.

**Předpoklad k ověření (#612):** že scope `read` stačí na `/api-quote-tokens` a na DXLink
streaming. Očekává se ano, ale celý tento návrh na tom stojí — pokud by market data
vyžadovala `trade`, je to důvod k přehodnocení celé integrace, ne k rozšíření scope.

## Důsledky

**Co se nemění:** IBKR zůstává primární pro všechny dnešní datové cesty. Do dokončení fáze 1
(#613) se nemění chování aplikace ani jediný řádek IBKR kódu — sekundární zdroj běží
výhradně v shadow módu za vypnutým feature flagem. Milestones M1–M6 nejsou dotčené,
integrace má vlastní milestone M7.

**Co se mění:** engine dostává vrstvu `MarketDataProvider` (rozšíření existujícího
`adapters.py`), datová schémata dostávají `source`, UI dostává indikaci zdroje a fallbacku.

**Rizika:**

- *Zpřesnění vstupu změní výstupy.* Plná klasifikace agresora (#615) změní hodnoty CumΔ,
  na které jsou naladěné detektory. Před nasazením se musí rozdíl vyčíslit na historii
  a track record (ADR-0021) musí umět rozlišit období před a po změně vstupu — jinak se
  smíchají statistiky úspěšnosti a znehodnotí měsíce sbíraného vzorku.
- *Druhá závislost.* Výpadek nebo změna licenčních podmínek tastytrade nesmí shodit
  aplikaci. Proto je primární zdroj IBKR a všechny tastytrade cesty musí mít definované
  chování při nedostupnosti (návrat k dnešnímu stavu, ne chyba).
- *Entitlement se může změnit.* Historicky (2023–24) tastytrade API token dodával jen
  akciová data a přístup k plnému streameru byl omezovaný. Od konce 2024 je streamer
  otevřený v plné šíři, ale je to obchodní rozhodnutí brokera, ne smluvní garance.
- *Redistribuce dat je zakázaná* podmínkami API. Aplikace běží lokálně pro jednoho
  uživatele a data neredistribuuje; při jakékoli úvaze o sdílení instance se tohle musí
  znovu posoudit.

**Revize:** pokud ověřovací spike (#612) prokáže, že ES FOP data přes tastytrade nejsou
dostupná nebo že klíčové eventy (`Summary.open_interest`, `TimeAndSale.aggressor_side`)
nechodí, ADR se překlopí na „zamítnuto" a R5 se vrátí k původnímu znění. Do té doby platí
rev. 2.

## Dodatek 2026-08-12 — plný fallback opčního řetězu (#614/#616)

**Rozhodnutí uživatele:** tastytrade je primárně rozšíření/doplnění; sekundárně
ale při výpadku IBKR (výpadek farmy, pád TWS, **přetažení feedu mobilní
aplikací** — error 10197) přebírá **celý opční řetěz**, a po zotavení IBKR ho
automaticky vrací. Řádek „Kotace řetězce" v matici se mění z „rozšíření" na
„rozšíření + fallback".

Pravidla zůstávají v platnosti beze změny a vztahují se i na řetěz:

1. Přepnutí vlastnictví **jen na hranici snapshotu s hysterezí** (pravidlo 3) —
   mobilní přetahovaná je intermitentní, bez hystereze by řetěz kmital.
2. **Viditelná degradace** (pravidlo 5): badge „řetěz: tastytrade" po celou dobu
   fallbacku; návrat k IBKR opět s hysterezí (N úspěšných cyklů).
3. Sloupec `source` per záznam (pravidlo 1) — fallbackové minuty jsou v datech
   rozlišitelné navždy.
4. **CumΔ během fallbacku** mění zdroj klasifikace (TimeAndSale místo IBKR
   tick-by-tick + midpoint) — track record musí období oddělit, stejný
   mechanismus jako u #615.
5. Podmínkou je parita kotací doložená shadow fází (#613) — bez ní by přepnutí
   skokově změnilo GEX pole.

**Dopad na spike #612:** schopnost utáhnout ŠÍŘKU CELÉHO řetězu (aktivní +
sekundární expirace, plné pásmo strikes) souběžně je od teď **tvrdý
požadavek**, ne nice-to-have — bez něj fallback řetězu nemůže existovat
a měří se explicitně.

Poznámka k souběhu s mobilem: plnohodnotné trvalé řešení zůstává druhý IBKR
username (#539 fáze 0) — fallback řetězu je pojistka a most, ne náhrada.

## Dodatek 15. 8. 2026 (#696)

Nález 17 z triáže („menší blast radius při úniku `.env` neplatí, dokud dev
i prod čtou tentýž soubor") vyřešen: dev stack čte `.env` + volitelný
`.env.dev`, dev grant se ukládá do `.env.dev` pod standardními názvy
`GEXLENS_TASTY_*`. Oddělené soubory = oddělený blast radius; na VPS (#539)
pojede jen prod `.env`.

## Dodatek 14. 9. 2026 — degradovaný start a plný přechod na tastytrade (#1153, #614 doplňky)

**Rozhodnutí uživatele (14. 9., #1153 varianta A):** po ztrátě IBKR má aplikace
**plynule přejít na tastytrade včetně spotu, zdí, OI a všeho, co tasty umí dodat**
— i tehdy, když IBKR vypadne dřív, než se pipeline založí. Dodatek z 12. 8.
řešil jen běžící pipeline; 14. 9. při souběhu s mobilem TWS ztratila spojení
s IBKR celé (Error 1100, sec-def farma „broken", později i Gateway vyhozená —
`connection refused`) a NQ, jehož pipeline padla s API socketem, stál celé
odpoledne bez grafu, zatímco ES založený minutu před výpadkem jel z tasty.

### Co se změnilo (PR #1154–#1164, engine `260efa2`)

| Vrstva | Do 14. 9. | Od 14. 9. |
| --- | --- | --- |
| Založení pipeline | výhradně IBKR sec-def (discovery front futures, `reqSecDefOptParams`, spot z IBKR tickeru); bez API socketu se nezakládá | **degradovaný start**: kontrakt a řetěz z `derived/discovery_cache.json` (poslední úspěšné discovery, ≤ 14 dní, neexpirovaný front kontrakt), úvodní spot z tasty; bez socketu prázdný ticker a IBKR subskripce až v `resubscribe` po reconnectu; alert `degraded_start` |
| Spot v cyklu pipeline | IBKR ticker (za fallbacku zamrzlý → GEX a hlídač barů nad cenou z doby výpadku) | `spot_override` z tasty, dokud fallback spotu běží |
| 1min bary podkladu | jen IBKR real-time bary; za výpadku svíčky stály | při stall každou minutu doplnění chybějících minut z dxFeed Candle (`source = tasty_candle`, UI úsek odliší); jedno pomocné DXLink spojení naráz |
| Plán tasty chain subskripcí | z IBKR quote cache (prázdná u pipeline založené za výpadku → 0 tasty symbolů → 0 snapshotů) | z **kontraktů, které pipeline drží** + symboly z konfigurace i watchlistu bez pipeline |
| Křížová kontrola | držené kontrakty bez IBKR kotace = „sledováno 0" → `insufficient`, fallback řetězu se po restartu nikdy nezapnul | takový kontrakt = mrtvá IBKR strana (`_DEAD_IBKR_QUOTE`) → při čerstvé tasty `ibkr_suspect` → fallback |
| Stáří tasty hodnot | „kotace i greeks změněné do 120 s" → deep OTM striky (bez změny minuty) vypadávaly, ES 116/160 | **živost streamu** (poslední event streamu ≤ `GEXLENS_TASTY_CHAIN_MAX_AGE_S`): dxFeed je event-on-change, nezměněná kotace na živém streamu je aktuální |
| Greeks | kontrakt bez dxFeed Greeks vynechán celý | BS greeks z mid (`fallback_greeks`, `source = computed`) jako v IBKR cestě (#547) a extended expiracích (#616); bid 0 s ask > 0 → BS z ask/2, při nekonvergenci limitní nula |
| IBKR sec-def bez odpovědi | `qualifyContractsAsync` bez stropu → OI archiv i orchestrátor viseli navěky | strop 20 s, po timeoutu 60 s fail-fast; discovery řetězu 30 s |

### Ověření na prod 14. 9.

Degradovaný start ES/NQ při Error 1100 (17:15 UTC): 160/160 a 96/96 snapshotů
s greeks a OI z tasty; návrat IBKR (1102, 17:18) → spot i řetěz zpět za minutu.
Restart bez Gateway (18:34) → ES z cache, po reconnectu subskripce obnoveny.
Večer na tasty (20:03): ES 15. 9. **159/160**, NQ **96/96**, spot i svíčky z tasty.

### Co z tasty vědomě NENÍ (beze změny)

Kumulativní denní opční objem, Prémie $ (objem × mid), CumΔ podkladu z IBKR
tick-by-tick (stín z dxFeed TimeAndSale řeší #615) a broker news. Během fallbacku
tedy stojí panely Vol, OptVol, CumΔ a Delta flow a sloupce objemu ve strike
profilu; nic z toho se nedosazuje (pravidlo 2, #465).

### Meze

- Čistá instalace bez jediného úspěšného discovery: cache je prázdná, pipeline čeká
  na IBKR. Cache se naplní prvním normálním startem.
- Roll front kontraktu během výpadku: záznam s expirovaným front kontraktem se
  nepoužije — pipeline čeká na IBKR.
- Degradovaný start trvá ~2 min (3× timeout discovery podkladu) — vědomě,
  transientní výpadky sec-def farmy mají retry přednost.

