# ADR-0061: FA odhad OI z objemu seance — OI + ρ·objem místo α·net

- **Stav:** přijato 24. 10. 2026. Vlastník v chatu zvolil doporučené varianty: model A (bod 1),
  kdy 2a (bod 5) a FA do opravy 3a (bod 6). Zapsáno v Rozhodnuto 24. 10. v epice #1385. Body 2–4
  a věty označené „odvozeno“ rozvádějí variantu A, jak ji vlastník dostal k volbě. Prahy kalibrace
  jsou převzaté z kalibrace α a retro ρ v #1665 je přeměří.
- **Datum:** 2026-10-24
- **Mění:** ADR-0011 rozhodnutí fáze 1 bod 2 a fáze 2 body 2 a 5 (vzorec odhadu a kalibrace α) ·
  ADR-0057 bod 6 (vstup FA a kalibrace z tisků, FA vypnutá do 5 dnů po bumpu) · SPEC 4.5
  (net jako vstup FA)
- **Implementace:** #1665 (engine, retro ρ), #1666 (texty UI); nasazení s bumpem v6 E-1.14b #1403.
- **Podklad:** rozbor #1612 (issuecomment-6373807743), skript `scripts/measure_fa_ratio_1612.py`.

## Kontext

ADR-0011 odhaduje intradenní OI jako `max(0, OI_ráno + α·net)`. Net je čistý klasifikovaný
objem kontraktu (buy − sell) a α je medián ΔOI/net stran sekundárního řetězu. Model předpokládá,
že čistý nákup OI přidává a čistý prodej ho ubírá. Ověřený nebyl: zpětná validace ADR-0011 měřila
jen |ΔOI| proti objemu bez znaménka.

Od 1. 10. 2026 vycházel medián ΔOI/net u NQ v 7 z 10 bodů ≤ 0. Bod ≤ 0 α nemění, takže α NQ stála
na zářijových bodech. Pod mechanikou v6 by FA NQ zůstala vypnutá, dokud nesebere 5 platných dnů,
a při tomto poměru by je nesebrala (#1605, #1612).

Rozbor #1612 vzal body D 25. 9.–22. 10. 2026, 20 na symbol. Do výběru šly strany řetězu D+1
s |net| ≥ 25, jejichž ΔOI bylo změřené po publikaci v D i D+1:

| | NQ | ES |
|---|---|---|
| stran | 832 | 2 001 |
| ΔOI > 0 na stranách s převahou nákupu | 97 % | 92 % |
| ΔOI > 0 na stranách s převahou prodeje | 94 % | 88 % |
| medián ΔOI/net, nákup / prodej | +0,37 / −0,26 | +0,86 / −0,81 |
| Spearman ΔOI × net / ΔOI × \|net\| | 0,14 / 0,35 | 0,21 / 0,55 |
| korelace podílu prodejních stran a mediánu dne | −0,88 | −0,51 |
| průměr denních mediánů ΔOI/objem bez 9. 10. (variační koeficient) | 0,339 (0,17) | 0,255 (0,21) |
| dnů s mediánem ΔOI/objem > 0 | 19 z 19 | 19 z 19 |

Z toho vyplývá:
- **OI roste skoro na každé obchodované straně bez ohledu na znaménko toku.** Strana agresora
  neříká, zda obchod pozici otevírá, nebo zavírá. Medián ΔOI/net proto měří hlavně podíl stran
  s převahou prodeje. NQ jich má víc (medián dne 48 %, ES 38 %), a proto vychází ≤ 0. ES má
  tutéž vadu, jen ji větší podíl nákupních stran skryje.
- **Tisky ze strany agresora (ADR-0057) model nezachrání.** Znaménko netu z midpointu se
  s agresorem shoduje jen na 46 % (NQ) a 50 % (ES) stran. I s netem z tisků ale OI roste na
  stranách prodeje. Fit ΔOI = b·net⁺ + s·|net⁻| dává oba koeficienty kladné: NQ 0,59 a 1,34,
  ES 1,10 a 1,28.
- Hypotézy #1612 H1 (výběr stran u ATM), H4 (řez a roll) a H6 (zlom kolem 1. 10.) data
  vyvrátila. H2 platí: znaménko midpointu je šum, ani net ze strany agresora ale model
  nezachrání (viz výše). H3 (šum objemu) zkresluje velikost poměru, ne jeho znaménko.
  H5 (pátek) vysvětluje menší podíl prodejních stran.
- **ΔOI/objem je stabilní.** Sedí s open-ratiem FA validace (0,26–0,41) i s hodnotou 0,39 ze
  zpětné validace ADR-0011, ze které vznikla výchozí α 0,4. Ta se ale násobila netem, ne objemem.

Příklad NQ, bod 14. 10. 2026, expirace 15. 10.:

| strana | OI D → D+1 | net | objem seance | FA dnes (α·net) | model A (ρ 0,34) |
|---|---|---|---|---|---|
| 30 300 C | 0 → 134 | −77 | 231 | 0 | 79 |
| 30 150 P | 5 → 234 | +257 | 555 | ~84 | ~194 |

## Rozhodnutí

1. **Odhad (A):** `OI_est(K, s) = OI_ráno(K, s) + ρ·V(K, s)`.
   - V je objem strany od začátku seance, tedy maximum denního kumulativu viděné v seanci
     (`SessionVolumes` v `CumDeltaTracker`, #1602). Pokles a návrat feedu ho nezvětšují.
   - Znaménko toku do odhadu nevstupuje.
   - Jediná definice zůstává `compute/flowoi.oi_estimate` (ADR-0011, fáze 2 bod 2). Z ní se
     počítají FA levels, FA Dyn GEX i řada `oiest`.
   - *Odvozeno:* podlaha 0 je zbytečná, protože ρ ≥ 0 a V ≥ 0.
2. **Kalibrace ρ per symbol** v ranním jobu místo α:
   - týž řetěz (byl celou seanci D sekundárem, #1580), týž řez 16:00 CT
     (`marketclock.daily_close_utc`) a ΔOI jen ze stran změřených po publikaci v D i D+1
     (`measured_oi_pair`, #1605);
   - V je maximum kumulativního objemu strany v seanci D do řezu ze snímků;
   - bod je medián ΔOI/V přes strany s V ≥ 25 kontraktů, aspoň 5 stran;
   - dál EMA λ 0,3, sevření do [0, 1] a medián ≤ 0 ρ nemění;
   - *odvozeno:* prahy jsou převzaté z kalibrace α (ADR-0011, fáze 2 bod 5). Retro ρ (#1665) je
     přeměří a odchylku předloží vlastníkovi.
3. **Platný den** pozná kalibrace ze snímků. Bod nedá den, jehož snímky řetězu mají za otevřeného
   trhu mezeru nebo končí před řezem. Příklad je 9. 10. 2026: data končí 14:18 UTC, a přesto jeho
   bod NQ 0,563 dnes v α je. Značka `netgap` (mezera trade větve) se pro ρ nečte.
4. **Stav a historie** se ukládají do `fa_alpha` a `fa_alpha_history` se `source = 'volume'`.
   - *Odvozeno:* nová tabulka netřeba.
   - Reset při bumpu zajistí pravidlo z ADR-0057 (doplněk 20. 10.): stav jiného zdroje je
     nekalibrovaný.
   - `ratio_buy` a `ratio_sell` zůstávají NULL a `FA_MIN_DAYS` (5) platí dál.
5. **Kdy (2a):** pod přepínačem mechaniky v6 (vzor ADR-0057 bod 11, #1602, #1600), pod v5 beze
   změny.
   - ρ se před bumpem dopočítá zpětně z archivu OI a snímků skriptem v `scripts/`. `--apply`
     běží jen v pauze s bumpem, se zálohou PG a po dotazu vlastníka.
   - FA tak jede od bumpu bez čekání 5 dnů.
   - Retro α z recorderu tisků (#1627) odpadá.
6. **Do bumpu (3a)** běží FA pod v5 beze změny (`α·net`). Je to jen zobrazení: setupy, tendence,
   verdikt ani news-engine FA vrstvu nečtou.

## Zvažované varianty

- **A — OI + ρ·objem seance (zvoleno).**
  - Výhody: sedí na data u obou symbolů a nezávisí na midpointu ani na tiscích, takže FA NQ
    po bumpu nečeká 5 dnů. Započte i spready a bloky bez tisku, které OI mění taky (ES ~21 %,
    NQ ~10 % objemu, ADR-0057 bod 6).
  - Nevýhody: mění vzorec ADR-0011 a vstup FA z ADR-0057 bodu 6. Odhad nereaguje na směr toku
    a hladiny FA se posunou.
- **B — dvě α podle strany agresora (pod v6).**
  - Výhody: zachová informaci o směru.
  - Nevýhody: tisky kryjí jen 49–80 % objemu a NQ má za den 3–22 použitelných stran. Oba
    koeficienty vycházejí kladné, takže směr nic nepřidá.
- **C — model ponechat a FA NQ vypnout.** ES má tutéž vadu a jeho α je jen směs stran.
- **D — vypnout FA celé.** Nic nezobrazuje špatná čísla, ale vrstva zmizí, i když je opravitelná.
- **Kalibrovat jen z podmnožin (strany, expirace), kde model platí.** Taková podmnožina v datech
  není: medián ΔOI/net prodejních stran je záporný ve všech pásmech vzdálenosti od ceny
  a ve 20 z 20 dnů u obou symbolů.
- **Kdy b — hned pod v5.** Byl by to druhý řez FA za týden.
- **Kdy c — až po bumpu.** FA NQ by pod v6 zůstala vypnutá a podle dat by se nemusela zapnout.
- **FA do opravy b — vypnout hned.** Znamenalo by to zápis α 0 do produkční PG mimo nasazení.

## Důsledky

- FA přestane reagovat na směr toku, který podle dat OI neurčuje. Odhad je na každé obchodované
  straně ≥ měřené OI. Ukazuje, kam objem dne pravděpodobně přidal OI, ne kdo pozici drží.
  Strany, kde OI klesl nebo se nezměnil (ΔOI ≤ 0, 3–12 % stran v rozboru), model nevidí.
- Hladiny FA se pod v6 posunou. Testy odhadu a kalibrace se mění v #1665.
- Kalibrace z řetězu D+1 zůstává proxy pro 0DTE aktivní řetěz, vědomý limit ADR-0011 trvá.
- Per-kontrakt net, řada `netflow` a značka `netgap` pod v6 ztrácejí jediného spotřebitele v enginu.
  Zda je dál psát, rozhodne triáž (#1667).
- Do bumpu ukazuje FA pod v5 model s vadou: na stranách s převahou prodeje odhad podhodnocuje
  (vědomě, 3a).
- Texty UI (badge „FA α“, Legenda FA, uživatelský manuál) řeší #1666, ADMIN-MANUAL kap. FA #1665.
