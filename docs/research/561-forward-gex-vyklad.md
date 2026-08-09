# Forward GEX na denním grafu — referenční výklad (podklad k #561)

> **Provenience.** Veřejně publikovaný výklad referenčního tradera k tomu, jak čte
> modelovanou gamma plochu na denním grafu své opční aplikace, doplněný snímkem
> intradenního zobrazení (akcie MU). Přepsáno 9. 8. 2026 jako podklad pro srovnání
> se SentimentLens a s naší Dyn GEX vrstvou.
>
> Jména osob ani názvy referenční aplikace se sem záměrně nepíší (viz úklid #482).
> **Vzhled nepřebíráme, jen principy** — naše UI má vlastní jazyk.
>
> Odvozená issues: #569, #570, #571, #572, #573, #574, #575, #576, #577, #580.
> Analýza tří videí téhož autora o sentimentu je v #561 a vyústila v #563–#567.

---

## 0. Dva různé pohledy

Text popisuje **denní (Daily) graf se zapnutým Forward GEX a konturami**. Přiložený
snímek je naproti tomu **intradenní 1min zobrazení**. Autor to výslovně odděluje:
„neplést s intradenním zobrazením, to je jiný příběh“. Při vytěžování je nutné
oba pohledy držet oddělené.

## 1. Orientace: graf má dvě poloviny

Uprostřed vede svislá bílá čára s popiskem **„Today“**. Rozděluje obrázek na dvě
zcela odlišné věci:

- **Vlevo je minulost** — obyčejné denní svíčky, co se s cenou skutečně stalo.
- **Vpravo je budoucnost** — žádné svíčky, jen barevná mapa toho, jak je momentálně
  poskládaný opční trh na nadcházející dny.

> Barevná mapa vpravo **není předpověď ceny**.

## 2. O co jde: kdo je na druhé straně opcí

Na druhé straně skoro vždy stojí market maker, který nechce spekulovat na směr a
proto se zajišťuje — podle pohybu podkladu dokupuje nebo doprodává. **To zajišťování
hýbe trhem.**

**Kladná gamma (tlumení):** cena roste → prodávají → brzdí; cena klesá → nakupují →
brzdí pád. Cena se chová jako v hustém sirupu, drží se v pásmu („pinning“).

**Záporná gamma (zesilování):** cena roste → dokupují → tlačí výš; cena klesá →
prodávají → pád zrychlují. Malý impulz udělá velkou svíčku.

## 3. Barvy

| Barva | Význam |
|---|---|
| zelená | kladná gamma — market makeři tlumí pohyb, cena se v pásmu drží |
| červená | záporná gamma — market makeři zesilují pohyb |
| tmavá / bez barvy | nic podstatného, jejich pozice tu nikoho netlačí |

Sytost = síla. **Zelená pásma jsou magnety a bariéry, červená pásma jsou skluzavky.**
Cally se počítají kladně, puty záporně — kde převažuje OI v callech, vyjde zelená.

## 4. Jak se budoucnost počítá

```
hodnota(den D, cena P) = Σ (±) OI × Γ(P, τ_D) × multiplikátor × P² / 100
```

- **OI** — kolik kontraktů na daném striku visí
- **Γ** — spočítaná pro cenu `P` a pro zbývající čas do expirace v den `D`
- **±** — cally plus, puty mínus
- **P² / 100** — převádí to na „kolik dolarů podkladu musí market makeři přeobchodovat,
  když se cena pohne o 1 %“. **Proto mají vyšší cenové hladiny přirozeně větší váhu.**

Příspěvek každého kontraktu se rozprostře i do okolních cenových hladin — gamma
nefunguje jako bodový zásah, ale jako **zvon kolem striku**. Čím dál je expirace,
tím je zvon širší a plošší.

**Co se v čase mění:** jediné — kolik dní zbývá do expirace. **Otevřený zájem i
volatilita se drží zmrazené na dnešních hodnotách.**

## 5. Svislé tečkované čáry = expirace

Čárkované svislice s popiskem tickeru jsou dny, kdy expirují opce. Sytější a
silnější čára je **OPEX** — třetí pátek v měsíci.

> Když opce vyexpirují, jejich gamma z trhu **zmizí ze dne na den**. Proto v mapě
> uvidíte ostré svislé předěly — nalevo od expirace je pásmo třeba silně zelené,
> hned napravo je bledé nebo pryč. Říkáme tomu **gamma útesy**.
>
> Struktura, která dnes cenu drží, nemusí v pondělí po expiraci existovat. **Trh,
> který se týden nehnul, se najednou rozjede — a nemuselo se stát nic jiného, než
> že vypršely opce, které ho držely.**

Útesy jsou v mapě záměrně ponechané **ostré**. Časová osa se nevyhlazuje, aby se
předěl nerozmazal — je to signál, ne šum.

## 6. Bílé čárkované čáry = kontury

Fungují jako vrstevnice na turistické mapě: spojují místa se stejnou silou gammy.

- Hustě u sebe = síla se mění prudce, strmý sráz.
- Daleko od sebe = plochá krajina.
- Uzavřený kroužek = jádro, nejsilnější místo v okolí.

Kreslí se **ve dvou úrovních naráz** → soustředné obrysy: vnější označí celou
oblast, vnitřní jen nejsilnější jádro.

| Volba | Prahy |
|---|---|
| Off | žádné čáry, jen barvy |
| Major | jen výrazné útvary — **65 % a 95 %** síly |
| All | i slabší struktury — **40 % a 70 %** síly |

**Kontury se kreslí pro kladnou i zápornou stranu**, takže obkreslují jak zelené
bariéry, tak červené skluzavky.

### Čtení dvou čar na spodním okraji zelené

- **spodní čára** — tady tlumení **začíná** (práh 40 %)
- **horní čára** — tady už je tlumení **silné** (práh 70 %)

Mezi nimi tlumení náběhne. Čím jsou čáry blíž u sebe, tím ostřejší přechod.

| Kde je cena | Co to znamená |
|---|---|
| nad oběma čarami | uvnitř tlumící zóny — výkyvy se zaplácnou, spíš postranní pohyb |
| mezi čarami | přechodové pásmo, tlumení slábne |
| pod oběma | mimo tlumící zónu — brzdy jsou pryč, pohyb se rozjede snáz |

### Past 1: není to „cesta nejmenšího odporu“

> Zelené pásmo je **zóna největšího odporu** vůči pohybu, ne nejmenšího. Uvnitř něj
> market makeři pohyb aktivně brzdí. Cena tam bývá **uvězněná**, ne že by tudy klouzala.
> Cesta nejmenšího odporu je naopak tam, kde je mapa **tmavá nebo červená**.

Na intuici „cena se pohybuje podél pásma“ ale něco je: silné zelené pásmo opravdu
funguje jako koridor. **Jen je to koridor z tření, ne z hladkosti.**

- uvnitř pásma — postranní pohyb a návraty ke středu, ne trend
- pod pásmem — rychlejší, trendovější pohyb

### Past 2: stoupající pásmo neznamená rostoucí cenu

> V projekci pásmo často stoupá směrem doprava. Neznamená to, že se čeká růst.
> Znamená to, že jak postupně vypadávají nejbližší expirace, zbývá struktura tvořená
> delšími kontrakty, jejichž otevřený zájem leží výš. Je to **důsledek složení trhu,
> ne směrová předpověď.**
>
> Ty čáry ber jako **hranice režimu, ne jako trajektorii.**

## 7. Ostatní ovladače

- **Scale** — jak se síla převádí na barvu. `Linear` doslovná; `√`, `Log`, `Pow⅓`
  postupně víc zvýrazňují slabší struktury. Když je v mapě jedna obrovská dominanta
  a zbytek splývá do tmy, přepnout na `√` nebo `Log`. **S daty to nehýbe, jen s
  jejich zobrazením.**
- **Style** — `Gradient` plynulá plocha, `Blobs` zvýrazňuje jádra jako ostrůvky.
- **Walls** — čára sledující nejsilnější pásmo: `Off`, `Peak` (nejsilnější hladina
  v každém dni), `Center` (těžiště), `Smooth` (vyhlazená, bez poskakování),
  `Flip` (kde se kladná gamma překlápí do záporné), `Ridge` (hřebeny pásem).
  **`Flip` je prakticky nejzajímavější — hranice mezi klidným a divokým režimem.**

## 8. Co je kolem grafu

- **Pravý panel (Vol + OI)** — vodorovné sloupečky podle cenových hladin; zelená
  vpravo = cally, červená vlevo = puty. Táž struktura z boku, sečtená přes žebřík.
- **Max Pain** (fialová vodorovná čára) — hladina, na které by při expiraci propadla
  nejvyšší souhrnná hodnota opcí. Orientační bod, ne předpověď.
- **Vol** — denní zobchodovaný objem podkladu.
- **Evo OI** — jak celkový otevřený zájem narůstal a klesal v čase; červená a zelená
  linka = puty a cally. **Ukazuje, jestli se pozice budují, nebo zavírají.**

## 9. Postup čtení

1. Najdi bílou čáru „Today“ — vpravo od ní nejsou data, ale projekce.
2. Kde je dnešní cena vůči barvám? Sedí v zeleném pásmu, nebo pod ním?
3. Najdi hranici zelená/červená (`Walls: Flip`). Pod ní se chování trhu mění.
4. Najdi nejbližší expirační čáru a porovnej mapu těsně před ní a za ní. Mizí za ní
   zelená? Pak po té expiraci ubude to, co trh drží.
5. Kontury řeknou, jak ostré ty hranice jsou. Husté čáry = ostrý přechod.

## 10. Čemu věřit a čemu ne

- **Mapa neříká, kam cena půjde.** Říká, jak se trh pravděpodobně zachová, až se
  někam pohne — jestli pohyb utlumí, nebo zesílí.
- **Budoucnost je spočítaná ze zmrazeného dneška.** OI i volatilita se předpokládají
  konstantní, mění se jen zbývající čas. Čím dál doprava, tím spekulativnější:
  pár dní dopředu solidní, za měsíc spíš náčrt.
- **Data se přepočítávají ráno** (u akcií — OI publikuje burza jednou denně).
- **Ostré útesy u expirací jsou spolehlivější než absolutní čísla.** Že po expiraci
  struktura zmizí, je jistota daná kalendářem. Přesná síla pásma je odhad.

## 11. Konkrétní čtení ukázky (MU)

Mapa je skoro celá zelená, a je to korektní:

- OI v callech je posazený **výš** než cena, v putech výrazně **níž**.
- Váha `P² / 100` dává vyšším hladinám větší číselnou váhu, takže zelená oblast
  nahoře vyjde silnější než červená dole.
- Dohromady poměr zhruba **12 : 1** ve prospěch zelené.

Když kvůli té dominanci spodní část splývá do tmy, pomůže přepnutí `Scale` na `√`
nebo `Log` — slabší červená struktura se vynoří, **aniž by se s daty cokoli udělalo**.

### Obě kontury nad cenou = strop nad hlavou

> Tlumící struktura leží **výš**, než kde se cena obchoduje. Cena je tedy **pod**
> zónou, která by ji brzdila — v pásmu, kde ji nikdo zvlášť nedrží.
> Kdyby cena vystoupala k té spodní čáře a přes ni, dostala by se do prostředí,
> které pohyb postupně utlumí. **To je hranice, kde by se rally nejspíš zpomalila.**
> Čti to jako **strop nad hlavou**, ne jako koridor, ve kterém cena je.

## 12. Doprovodná poznámka autora k intradennímu snímku

K expirující opci na MU: *„tato opce dnes expiruje (dnes po 22:00 a v pondělí již
zde nebude překážet). Krásně je vidět, jak hranice gammy call opcí cenu tlačí na
aktuální level.“* — tedy tatáž úvaha o gamma útesu, jen aplikovaná dopředu na
intradenním pohledu.

---

## Co z toho plyne pro nás

| Zjištění | Náš stav | Issue |
|---|---|---|
| Váha `P²/100` s cenou hladiny | počítáme jen `Γ·OI·M` = $/bod | #569 |
| Kontury na kladné i záporné straně | jen kladná (`filter(v > 0)`) | #570 |
| Prahy jako % ze síly (65/95, 40/70), dvě úrovně | kvantily p75/p90 resp. 5 úrovní p50–p95 | #571 |
| Denní graf s „Today“, budoucími dny, expiračními svislicemi, OPEX | Daily je jen naměřená minulost | #572, #519 |
| Pás Evo OI | nemáme (jen ΔOI vs. včera v tooltipu) | #573 |
| Interpretační pasti (odpor, stoupající pásmo, dvě kontury, zmrazený model) | kap. 18 je správná, ale neúplná | #574 |
| Pásmo místo bodového flipu jako režim | `gex_regime` je binární vůči flipu | #575 |
| Gamma útes jako měřitelná veličina | nepočítáme vůbec | #576 |
| „Strop nad hlavou“ jako obchodní situace | T1 ji nepokrývá (kotví na strike, ne na hranu pásma) | #577 |
| Ztlumení projekce podle vzdálenosti, nápověda na škálu | plošná konstanta `PROJECTION_ALPHA`, žádná nápověda | #580 |
