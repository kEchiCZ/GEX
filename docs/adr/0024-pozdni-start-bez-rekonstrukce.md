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
