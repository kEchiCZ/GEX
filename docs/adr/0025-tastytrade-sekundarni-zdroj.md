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
