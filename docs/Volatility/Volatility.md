# Zadání pro Claude Code — porovnání aplikace GEX s videem o volatilitě

## O zdroji

Video: Tom Sosnoff (tastytrade), „What volatility actually is“ (~7:45).

**Důležité vymezení rozsahu.** Video je konceptuální, nikoli technické. NEZAZNÍVÁ v něm:
gamma, gamma exposure, GEX, dealer positioning, vanna, charm, VEX, dark pool flow,
open interest, IV Percentile, konkrétní DTE (0/7/30/45), konkrétní opční strategie
(covered call, cash-secured put, credit spread), ani kalkulace probability of profit.
Vše níže je pouze to, co ve videu skutečně zaznělo. Nic není doplněno zvenčí.

---

## 1. Co je volatilita (0:00–0:53)

- Volatilita je nejlepší nástroj pro nastavení rozumných očekávání — o čemkoli.
- **Volatilita je v podstatě očekávaný pohyb (expected move).** Je to přesná míra
  pravděpodobnosti a rizika.
- Na rozdíl od ceny je volatilita **statistická veličina s vlastnostmi návratu
  k průměru** (mean reversion).
- „Mean reverze **bez krátkodobého časového omezení** je snem oportunisty.“
  Podmínkou je tedy, že obchodník není tlačen časem.
- (Doplněno v 6:44) Volatilita je matematická rovnice; je mean-revertující
  a **stahuje se zhruba dvakrát častěji, než se rozšiřuje**.

---

## 2. Důvod 1 — Příležitost je zřídkakdy zjevná (1:07–2:42)

- Chceme-li využít mispricing — ať už emocionální, nebo strukturální, a způsobený
  čímkoli — **jistotu pro vstup dá jedině komparativní ocenění volatility**
  (porovnání, kde volatilita je, ne cena samotná).
- Když je volatilita vysoká a trh trochu panikaří, ceny aktiv mohou na krátkou dobu
  **klesnout pod svou vnitřní hodnotu**, nebo se naopak stát **výrazně předraženými**
  vůči skutečné hodnotě.
- Když se trhy odpojí od nějaké formy reality, tehdy se dají vydělat skutečné peníze.
- Tento typ situace nastává prakticky na každém trhu — akcie, nemovitosti,
  alternativní aktiva.
- **Měření volatility na listovaných trzích je relativně snadné.** Na nelistovaných
  je obtížnější, ale lze je provést stanovením odhadovaného cenového pásma.

**Osobní příklady mispricingu (1:55):**
- Žije v Chicagu a na nemovitostech tam nikdy nevydělal skutečné peníze, protože nikdy
  nekoupil nemovitost obchodovanou levně vůči svému obchodnímu pásmu — kupoval vždy
  „z touhy“, nikdy kvůli příležitosti.
- Když investoval do finančních aktiv, která považoval za levná kvůli vysoké volatilitě
  a tržní kapitulaci, nebo prodával, když je považoval za drahá kvůli hype a extrémní
  volatilitě, byly to vždy velké výhry.
- Protistrany dělají emocionální a impulzivní nákupy — a právě tam leží příležitost.

---

## 3. Důvod 2 — Volatilita odhaluje systémové zranitelnosti (2:42–4:17)

- Bez pochopení volatility je obtížné odhalit **strukturální slabinu** ve vlastních
  investicích.
- Bez pochopení toho, **odkud příležitost pochází**, je prakticky nemožné posoudit
  upside a downside portfolia.
- Příklady: z portfolia utilities nelze čekat asymetrický upside. Naopak u portfolia
  quantum a crypto akcií platí, že s neomezeným upside přichází významné downside riziko.

**Býčí trhy maskují špatné návyky (3:14):**
- Dlouhotrvající býčí trhy dokážou oklamat profesionály i samostatné investory.
  **Zamaskují špatné praktiky řízení portfolia a špatné pochopení expected move.**
- Jakmile se volatilita stane součástí rozhodování, začneš oceňovat listované trhy —
  a to, že **listované trhy volatilitu mispricují jen zřídka**, díky čemuž se
  spekulativní investice stanou dobře definovanými.

**Příběh 90/10 (3:55):**
- Investoval do řady netradičních alternativních investic; většina nevyšla.
- Dlouho nechápal, proč má tak špatný track record. Pak si uvědomil, že nikdy
  neinvestoval správně, protože ho **svedl upside a nikdy nepochopil skutečné
  downside riziko**.
- **Choval se k těmto investicím, jako by šlo o 50/50 sázky, přitom měly být oceněné
  9:1 proti němu.** Jeho alokace a struktura obchodů byly proto zásadně špatně,
  protože těmto obchodům nikdy nepřiřadil správnou volatilitu.
- (Toto je jeho vlastní zkušenost s alternativními investicemi — netvrdí, že obchody
  obecně jsou 90/10 proti retailu.)

---

## 4. Důvod 3 — Volatilita je prediktivní (4:17–5:44)

- **Volatilita je obchodovatelný index strachu.** Je proto velmi reálná pro dnešek,
  zítřek i pro horizont šesti měsíců.
- **Implied volatility (IV) měří, jak volatilní budou trhy v budoucnu.
  Historical volatility (HV) měří, co se stalo v minulosti.** Když se mluví
  o volatilitě, mluví se o implied volatility.
- Obchodovatelný index strachu dává **v reálném čase** vědět:
  - jaká je **nálada trhu a jeho apetit k riziku** — tedy zda je trh
    **complacent (klidný), nebo capitulating (kapitulující)**;
  - zda derivátový trh oceňuje prémii jako **rich (drahou), nebo cheap (levnou)**.
- Je zásadní **eliminovat hádání a odstranit subjektivitu z rozhodovacího procesu**.

**Historka z pitu CBOE (5:02):**
- Byl téměř 20 let market makerem na burze CBOE, ještě před vznikem VIX.
- Při obchodování v pitu musel **hádat**, jestli je volatilita drahá, nebo levná.
  Prostě to nevěděli — obchodovali potmě a jistě tím přišli o spoustu peněz.
- Dnešní retailový obchodník má:
  - **IVR (Implied Volatility Rank)**, který dává kontext k úrovním implied
    volatility. Je na téměř každé obchodní platformě na viditelném místě.
  - **Implied expected move**, odvozený z implied volatility konkrétního titulu,
    přímo na každé stránce obchodu.
- Hra se tím kompletně změnila a volatilita je dnes skutečně na prvním místě.

---

## 5. Důvod 4 — Volatilita vytváří efektivitu (5:44–7:16)

- Volatilita **zlepšuje basis**, **vytváří spekulativní příležitost** a **umožňuje
  strategickou kapitálovou efektivitu**.

**Zlepšování nákladové báze:**
- Jedno z nejdůležitějších využití zvýšené volatility je **vypisování callů nebo putů
  proti podkladu za účelem zlepšení nákladové báze**.
- **Čím vyšší implied volatility, tím vyšší ceny opcí a tím vyšší pravděpodobnost zisku.**
- Věří, že **omezit ziskovost výměnou za vyšší pravděpodobnost zisku je jedna
  z nejrozumnějších věcí, které lze při investování udělat**.
- Snaha zlepšovat basis je must-have strategie pro pasivní i aktivní investory.

**Binární události a earnings (6:33):**
- Volatilita může vytvářet **high alpha příležitosti**, protože otevírá dveře
  **outlier případům** — binárním událostem a earnings obchodům.
- **Je mu lhostejné, jaká strategie je zvolena.** Téměř každá opční strategie je
  efektivnější v obdobích vysoké implied volatility.

**Kapitálová efektivita:**
- Protože je volatilita obchodovatelná, lze použít **defined nebo undefined risk** —
  je tu obrovská optionalita.
- Pokud něco vypadá atraktivně kvůli zvýšené volatilitě, lze to obchodovat
  s **minimem rizikového kapitálu**.
- Jako aktivní obchodník: **„Musím si odškrtnout volatility box, jinak obchod
  neudělám.“** Volatilita hraje v jeho denním obchodování největší roli.

---

## 6. Rada pro začátek (7:16)

- Kdo s volatilitou začíná, má **začít u IV Ranku** — hodně to zjednodušuje a je na
  viditelném místě prakticky na každé platformě.
- **Jednotlivé hodnoty volatility jsou matoucí**, protože není jasné, jak jim dát kontext.
- **Je-li IV Rank vysoký, je obvykle celkem bezpečné jít do obchodu.**

---

## 7. Souhrn konceptů převeditelných na výpočet nebo UI

Pouze položky, které ve videu skutečně zazněly:

| Koncept | Co k tomu video říká |
|---|---|
| Expected move | Volatilita = očekávaný pohyb; míra pravděpodobnosti a rizika |
| Implied expected move | Odvozený z IV daného titulu, zobrazený u každého obchodu |
| IV Rank (IVR) | Dává kontext k úrovni IV; výchozí metrika pro začátečníky; vysoký IVR = obvykle bezpečné vstoupit |
| IV vs. HV | IV = budoucnost, HV = minulost |
| Mean reversion volatility | Kontrahuje ~2× častěji, než expanduje; funguje bez krátkodobého časového omezení |
| Komparativní ocenění volatility | Jediný zdroj jistoty pro vstup do obchodu |
| Premium rich / cheap | Zda derivátový trh oceňuje prémii draze, nebo levně |
| Nálada trhu | Complacent vs. capitulating, v reálném čase |
| Vztah IV → prémie → POP | Vyšší IV = vyšší ceny opcí = vyšší pravděpodobnost zisku |
| Trade-off zisk vs. pravděpodobnost | Omezit zisk výměnou za vyšší POP je rozumné |
| Zlepšování nákladové báze | Vypisování callů/putů proti podkladu při vysoké IV |
| Outlier / binární události | Vysoká IV otevírá dveře binárním událostem a earnings |
| Defined vs. undefined risk | Volatilita umožňuje vstup s minimem rizikového kapitálu |
| Volatility box | Povinná kontrola volatility před každým obchodem |
| Listed vs. non-listed | Listované trhy volatilitu mispricují jen zřídka → spekulace je dobře definovaná |
| Bull market bias | Dlouhé růstové trhy maskují špatné řízení portfolia a nepochopení expected move |
| Špatně přiřazená volatilita | Vede ke špatné alokaci a špatné struktuře obchodu (příběh 90/10) |

---

## 8. Úkoly pro Claude Code

Toto je repozitář mé GEX aplikace (ES + NQ, data z Interactive Brokers).
Zatím **nic neimplementuj** — jde o analýzu a návrh.

**Úkol 1 — audit codebase**
Projdi aplikaci a pro každý koncept z tabulky v sekci 7 urči:
- **A) JIŽ MÁM** — uveď soubor/modul a posuď, zda implementace odpovídá pojetí z videa
- **B) MÁM JINAK** — popiš rozdíl a řekni, která varianta je pro intradenní ES/NQ vhodnější
- **C) NEMÁM** — chybí úplně

Nepředjímej, které moduly existují — zjisti to z kódu.

**Úkol 2 — datová proveditelnost přes IBKR**
U každé chybějící položky urči:
- jsou potřebná data dostupná z IBKR API?
- kolik historie je potřeba a jak to koliduje s mou současnou retencí 14 dní?
- pokud je potřeba delší historie (typicky IVR), navrhni řešení mimo hlavní
  retenční politiku

**Úkol 3 — synergie s existující gamma vrstvou**
Video o gammě nemluví, ale aplikace na ní stojí. Navrhni, jak volatility vrstvu
propojit se stávající gamma vrstvou. Zajímá mě zejména:
- expected move jako pásmo přes GEX heatmapu a jeho vztah k walls a gamma flip
- zda se gamma režim chová jinak při vysokém a nízkém IVR
- statistika: jak často cena za sledované období respektovala expected move
- rozšíření modulu „Ranní briefing“ o sekci Volatilita
- položka „volatility box“ v akci „Založit ranní plán do deníku“

**Úkol 4 — výstup**
Zapiš do `docs/Volatility/analyza-video-sosnoff-volatilita.md`:
- tabulku konceptů s kategoriemi A/B/C
- sekci „Doporučení k implementaci“ seřazenou podle poměru
  (přínos pro obchodní rozhodování) / (náročnost), s ohledem na dostupnost dat
- u každého doporučení návrh GitHub issue: název, popis, akceptační kritéria

**Issues zatím NEZAKLÁDEJ.**

**Buď kritický.** Video je primárně o akciovém a portfoliovém investování.
Části o nemovitostech, alternativních investicích, earnings obchodech a zlepšování
nákladové báze vypisováním opcí proti podkladu nemusí na intradenní obchodování
futures ES/NQ vůbec sedět. Pokud je něco irelevantní, řekni to a nedoporučuj to.