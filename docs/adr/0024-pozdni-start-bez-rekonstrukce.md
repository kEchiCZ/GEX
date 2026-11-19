# ADR-0024: Pozdní start enginu — žádná rekonstrukce opční vrstvy

**Stav:** přijato (2026-08-06, rozhodl uživatel — issue #518)
**Kontext:** Při startu enginu uprostřed seance se bary podkladu backfillnou
(`UnderlyingBackfiller`), ale opční vrstva ne: minuty snapshotů (volume/Greeks
per strike), levels, gexprofile i CumΔ za dobu výpadku chybí. Issue #518
zvažovalo tři varianty: A) historický backfill opčních barů, B) dohnat
agregáty, C) díru jen poctivě označit.

## Rozhodnutí

1. **Varianta A se zamítá** — i do budoucna, bez nového ADR se dělat nebude.
   Greeks zpětně od IBKR neexistují, takže by se rekonstruovala jen volume
   vrstva; pro pásmo ~360 kontraktů to při pacing limitu (≤60 req/10 min,
   ADR-0001) znamená ~hodinu requestů (s BID_ASK pro klasifikaci dvojnásobek),
   které by dusily živý sběr kvůli poloviční rekonstrukci minulosti.
2. **Engine je nepřetržitý sběrač** (Docker, 24/7). Pozdní start je výjimečný
   stav a má se poctivě ukázat, ne draze a napůl zamaskovat — stejný princip
   jako šrafovaná chybějící OI (#465): žádná dokreslená data.
3. **Co se implementuje (#518, zúžený scope):**
   - IBKR posílá denní kumulativní volume per kontrakt, takže první sweep po
     startu srovná denní součty sám. **První minuta po startu se označí
     `catch_up` flagem** — přírůstkové odvozeniny (Opt Vol, Δ Flow, budoucí
     okenní analýza #483) ji nesmí číst jako minutový obchod.
   - **CumΔ nese poznámku „od HH:MM"** (start měření), protože tok za výpadek
     rekonstruovat nelze.
   - Viditelnost díry v ose řeší obecně #516 (bar-only minuty už osa po #459
     a #503 nese; sloupce bez snapshotu se nekreslí jako měřené).

## Důsledky

- #518 se zužuje z backfill podsystému na malé zadání (flag + popisek).
- Okenní analýza (M6 #483) musí `catch_up` minutu při `vol_window` rozdílech
  přeskočit / přiznat v odpovědi (`stale_age`/flag), jinak by celý skok
  kumulativu přiřkla jedné minutě.

## Dodatek 2026-09-01 (#617): bary podkladu se rekonstruovat ZAČALY

Původní znění vzdávalo rekonstrukci jako celek. **Opční vrstvy se to týká
dál a beze změny** — bod 1 (zamítnutí varianty A) platí. Změnil se jen jeden
dílek: **1min bary podkladu**.

dxFeed `Candle` umí historii od `fromTime` a na rozdíl od IBKR historical na
ni nemá pacing limit, který by dusil živý sběr. Rekonstrukce díry po pozdním
startu tedy stojí jedno krátké spojení, ne hodinu requestů — a tím padá
důvod, proč se u barů vzdávala.

**Co se rekonstruuje:** výhradně OHLCV 1min bary podkladu, a to jen minuty,
které v particii chybí. Zdroj zůstává doplňkem: primární je IBKR historical
(matice vlastnictví ADR-0025).

**Co se nerekonstruuje a proč:**

* **Opční vrstva** — beze změny, viz bod 1 výše.
* **CumΔ a cokoli z tick-level toku.** Svíčka nese OHLCV, ne jednotlivé
  printy s agresorem. Doplněná minuta má cenu a objem, ale žádný tok —
  poznámka „CumΔ od HH:MM" (bod 3) proto platí dál a nesmí se tvářit, že
  díru zaplnil backfill barů.

**Jak se pozná rekonstruovaná minuta:** sloupec `source` v particii barů.
`NULL` = partice pořízená před #617, `"ibkr"` = živá cesta, `"tasty_candle"`
= doplněno backfillem. Doplněná minuta není totéž co změřená a nesmí v UI
splynout — je to tentýž princip jako šrafovaná chybějící OI (#465) a bod 2
tohohle ADR: díra se poctivě ukáže, nedokresluje se.

**Past (ADR-0027):** `/ESU6:XCME` s hlubokým `fromTime` vrací svíčky z roku
2016. Symbol musí nést plný rok a bere se výhradně z chain endpointu.

## Dodatek 2026-10-27 (#1631): catch-up minuta v okně Rozsah ⧉

Bod Důsledky „okenní analýza musí catch-up minutu přeskočit / přiznat“ hlídá,
aby se skok kumulativu nepřiřkl **jedné minutě**. Okno se tak čte dvojím
způsobem:

* **Minutový tok okna** (`/flow` `window.opt_vol`, panel Opt Vol) catch-up minutu
  vynechává. Je to součet minutového obchodu a skok výpadku minutovým obchodem
  není.
* **Profil okna** (`/profile?from&to`, `rangeselect.windowVolumes`) catch-up
  přírůstek započte, pokud celý výpadek leží uvnitř okna. Objem se v okně
  opravdu zobchodoval, jen nejde rozdělit po minutách, a profil okna po
  minutách nedělí. Leží-li hranice okna ve výpadku, posune se na první měřenou
  minutu uvnitř okna (rozhodnutí B 27. 10. v #1385). Catch-up přírůstek se pak
  stane bází a do okna nejde, takže objem z doby před oknem se do okna
  nepřipíše.

Součet profilu okna proto může převýšit `window.opt_vol` téhož okna, a to
o objem výpadku uvnitř okna. Uživatelský manuál to přiznává (okenní profil).

## Dodatek 2026-11-19 (#1094, ADR-0063): navázání denního stavu i uprostřed seance

Rozhodnuto 19. 11. v #1094: **navázání denního stavu ano**, i uprostřed seance. Přepnutí
aktivního stroje (ADR-0063 bod 5) je řízené zastavení zdroje a start cíle s dnešními daty
z dorovnání. Bez navázání by cíl začínal jako pozdní start a přepnutí by stálo denní
akumulátory.

**Co už platí (z dnešních partic):** po restartu uprostřed seance se navazují kumulativní net
objem z partice `netflow` (#232, #1460), CumΔ z partice `flow` (#638, `restore_cum`) a CVD
podkladu (#1603). Čte se jen poslední měřený řádek **téže seance** (okno `session_bounds`,
i předchozí UTC partice, #1002); u CumΔ a net objemu navíc jen řádky zdroje běžícího trackeru
(ADR-0057 bod 8), CVD podkladu bere poslední úroveň bez filtru zdroje.

**Co se doplní (#1849):**

1. **Inventura** stavu, který engine drží přes den jen v paměti a z partic ho neodvodí
   (např. stav detektorů setupů a tendence, živá upozornění plánu QQQ, postup OI archivu).
   Tabulka položek s odpovědí „odvodit z partic / checkpoint / nenavazuje se“ přijde do
   tohoto dodatku s implementací.
2. **Checkpoint při řádném zastavení** (přechod do prohlížení, `docker stop`, nasazení):
   engine dopíše minutu a uloží denní stav jako **součást dat dne** pod klíčem obchodního dne
   (`trading_session_date`, ne UTC partice). Proto se zálohuje i synchronizuje s ostatními
   daty a cíl přepnutí ho má po dorovnání. Vyžaduje obsluhu SIGTERM a `stop_grace_period`
   enginu.
3. **Navázání při startu:** checkpoint téže seance → akumulátory pokračují od uložených hodnot;
   bez checkpointu (pád) se odvodí z dnešních partic, co jde. Položky, které nejde odvodit,
   začnou od nuly **viditelně** s popiskem „od HH:MM“ (bod 3), ne potichu. Checkpoint jiné
   seance se nenačte nikdy (reset na hranici seance, #638, #1460, #1603).

**Co se nenavazuje a proč:**

* **Díra zůstává dírou.** Minuty mezi poslední zapsanou minutou zdroje a první minutou cíle
  se nedokreslují ani neinterpolují (bod 2). První sweep po startu nese `catch_up` (bod 3),
  protože denní kumulativní objem IBKR v sobě má i objem díry.
* **Tok za dobu díry** chybí: CumΔ pokračuje od uložené hodnoty, tisky z mezery v něm nejsou.
  Mezera se ukáže v ose jako u každé jiné díry (#516).
* **Opční vrstva** za dobu díry — bod 1 (zamítnutí varianty A) platí beze změny.
* Popisek „CumΔ od HH:MM“ (bod 3) zůstává pro den, kdy stroj nemá z téže seance žádná
  starší data (první start v seanci bez dorovnání).
