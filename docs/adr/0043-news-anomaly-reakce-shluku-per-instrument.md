# ADR-0043: Upozornění na zprávy = mimořádná reakce na shluk s významnou zprávou, per instrument

- Stav: přijato (25. 9. 2026, rozhodnutí uživatele v #1291: bod 5, Q1 varianta B, Q2 předobchodní
  upozornění — změna původního „souhrnu na otevření“ z téhož dne — a upřesnění výběru zpráv do
  předobchodního souhrnu)
- Souvisí: sentiment SPEC 9.4 (anomálie ve zvonku), #295 (původní pravidlo), #744 (backfill),
  #578 (kurátoři Bluesky), #1284 (přepínače Telegramu), #1287 (měření pravděpodobnosti),
  #1290 (proklik na graf), ADR-0016 (fuzzy dedup), ADR-0023 (seance, svátky podle barů)

## Kontext

Pravidlo #295 (SPEC 9.4 „reakce překročila historický p90 bucketu“) hodnotilo každou zprávu
zvlášť: `ret_5` z `news_reactions` nad p90 bucketu kategorie × importance × surprise, jen
nekontaminovaná okna. Za 14 dní (11.–25. 9. 2026) vyrobilo **598 upozornění** (ES 227 /
NQ 371), 94 % s importance 1, žádné na plánovaný makro release; 25. 9. po UoM 80 upozornění
za 50 minut. Příčiny (analýza #1291, replay seděl na log news-enginu):

- Pohyb ze stejné chvíle nejde přisoudit jednotlivé zprávě — v minutě vyjde několik titulků,
  většinou šum; každý dostal „svou“ anomálii.
- Kontaminace (jiná zpráva s importance ≥ 2 v okně) je při dnešním toku skoro všudypřítomná
  (82,5 % oken `cont_5`), takže strukturálně blokovala skutečné releasy a pouštěla poslední
  šum před klidem.
- `importance` v `news_events` je po pravidlovém klasifikátoru, ne od zdroje: regex přepisuje
  i FF impact (USD PPI High → 1, „FOMC Member Speaks“ Low → 3).
- Close-to-close míjí whipsaw (FOMC 16. 9.: ES −0,3 bp, výchylka −19 bp).
- Upozornění chodilo ≥ 60 min po zprávě (čekalo na nejdelší okno reakce).
- Víkendové zprávy dostaly „deferred“ reakci = celý gap na otevření (92 upozornění NQ 12. 9.
  z jediného gapu) — až ve chvíli, kdy už se na open nedalo připravit.

## Rozhodnutí

1. **Shluk** zpráv se kotví na první **významné** zprávě `t0` a bere zprávy do `t0 + 2 min`;
   šum shluk nezaloží ani neprodlouží, jen se počítá. Shluk jen ze šumu se neohlašuje (bod 5).
2. **Významná zpráva** (Q1 varianta B): FF kalendář s `raw.impact` High/Medium (ne
   `importance`); headline a broker s importance ≥ 2 mimo kategorii EARNINGS; sociální sítě
   jen kurátor (#578, příznak `raw.curated` zapisuje Bluesky collector) s importance ≥ 2.
3. **Mimořádný pohyb** = maximální výchylka (high/low) do 5 min od celé minuty `t0` proti close
   minuty před ní, **nad 97. percentilem** výchylek ve stejné denní době (minuta dne v New Yorku
   ± 30 min, 20 předchozích seancí, ≥ 200 vzorků) **a zároveň** nad 97. percentilem po normalizaci
   σ minutových log výnosů poslední hodiny (režim volatility). Text nese výchylku a hranici v bp,
   žádnou pravděpodobnost.
4. **Per instrument**: ES a NQ mají vlastní bary, baseline, prahy a cooldown; jedno upozornění =
   shluk × instrument. Cooldown 15 min per instrument oběma směry (sousední shluky měří týž
   pohyb, pozdní zpráva může kotvu posunout dřív).
5. **Zavřený trh** (Q2): zprávy za zavřený trh se jednotlivě neohlašují (výchylka potřebuje bar
   minuty před zprávou, gap na otevření se nikomu nepřisuzuje). Po víkendu přijde **předobchodní
   upozornění** (`news_preopen`) — ES a NQ zvlášť, jen když za zavřený trh vyšla aspoň jedna
   **zásadní** zpráva:
   - **zásadní zpráva** (upřesnění uživatele 25. 9., `preopen.is_key`) je podmnožina významných:
     kalendář FF High/Medium a kurátor na sociálních sítích (obojí jako v bodě 2) a headline
     nebo broker v kategorii `FED`, `MACRO_INFLATION`, `MACRO_LABOR`, `MACRO_GROWTH` nebo
     `GEOPOLITICS` s importance 3. Obchod a cla samostatnou kategorii nemají — pravidlový
     klasifikátor je řadí do `GEOPOLITICS` (`tariff|sanction`), LLM má týž výčet
     `NEWS_CATEGORIES`. Ostatní významné (varianta B) jsou jen počet „další významné: k“,
     šum „ostatní zprávy: n“. Intradenní shluky (body 1–4) se tím neřídí;
   - **hlavní souhrn 4 h před otevřením Globexu** (v běžném týdnu 20:00 Praha) a **aktualizace
     15 min před otevřením** (23:45) jen se zásadní zprávou, kterou hlavní souhrn neobsáhl;
   - **bez zásadní zprávy se nic neposílá**, ani když varianta B něco splní (uživatel nechal
     rozhodnout podle simulace). Za 8 víkendů 1. 8.–20. 9. 2026 nastalo v čase hlavního souhrnu
     2× (1.–2. 8.: 2 jen významné; 8.–9. 8.: 39), v čase aktualizace 0×; v obou víkendech přišla
     zásadní zpráva do 23:45 a upozornění odešlo jako první souhrn. Souhrn „bez zásadní zprávy“
     by nesl jen úrovně (ty ukazuje aplikace) a počty toho, co uživatel označil za šum — a byl by
     to rutinní nedělní text bez zprávy, proti pravidlu „nic, když nevyšla zpráva, která stojí
     za přípravu“;
   - obsah: zásadní zprávy od posledního obchodu (−5 min — zpráva těsně před zavřením už nejde
     změřit) s klasifikovaným směrem (`sentiment_dir`), souhrnný sklon 🟢/🔴/⚪ z počtu jejich
     směrů, klíčové úrovně poslední seance (call/put zeď, flip, těžiště z `levels` expirace
     příští seance — páteční 0DTE po settle zanikla) a close; výčet max 5 (kalendář, pak
     importance, v nich nejnovější; „další zásadní: j“), tatáž story z více zdrojů jednou
     (Jaccard ≥ 0,9 jako ADR-0016, kalendář se neslučuje; už ohlášená story má přednost, takže
     kopie s dřívějším `ts_event` zapsaná po souhrnu není nová); **bez pravděpodobnosti**,
     dokud ji nezměří #1287;
   - časy se odvozují od otevření Globexu (`settle.session_bounds` 17:00 CT +
     `marketclock.is_market_closed`), takže sedí přes DST v obou zemích (25. 10. 2026: 19:00
     a 22:45 Praha); denní pauza (1 h) se nehlásí;
   - **svátky** kalendář nezná (ADR-0023 bod 4). Pokrytý je jen svátek, který **zkrátí nebo
     zruší páteční seanci** (Velký pátek, Vánoce či Nový rok v pátek, 3. 7.): projeví se
     dřívějším posledním barem, okno zpráv začne od něj a otevření zůstává nedělní. Svátek
     s **celodenním zavřením v pondělí** (Vánoce či Nový rok v pondělí nebo s náhradním
     pondělím, poprvé 25. 12. 2028 a 1. 1. 2029) pokrytý **není**: CME v neděli 17:00 CT
     neotevře, rozvrh ale ano — job pošle v neděli 20:00 a 23:45 souhrn k otevření, které
     nenastane, a před skutečným otevřením v pondělí 17:00 CT nic (`follows_long_closure` ho
     bere jako konec denní pauzy). Patří k nepokrytým svátkům v Důsledcích;
   - **dedup i přes restart**: stav etap a ohlášených zpráv v PG `settings`
     (`news_preopen_state`, vzor `drift_state`), zapsaný PŘED vrácením payloadů. Restart
     news-enginu v neděli večer souhrn nezopakuje a aktualizace pozná, co už bylo ohlášeno;
     proces, který ve 20:00 neběžel, pošle souhrn při prvním běhu do otevření (jednou).
     Riziko je ztráta (pád mezi zápisem a publikací), nikdy duplicita.
   - Telegram: přepínač „Zprávy před otevřením po víkendu“ (Setupy a burza, výchozí zapnuto)
     s výjimkou z tichých hodin (`quiet_exempt`) — aktualizace ve 23:45 by jinak výchozí
     tiché hodiny 23:00–06:00 nikdy nepustily.
6. **Načasování a stav reakcí**: job čte bary přímo (`news_reactions` nepoužívá), shluk
   vyhodnotí po `t0 + 5 min + 2 min` na zápis barů, upozornění odejde za 7–12 min (smyčka
   à 300 s). Shluky se každý běh staví znovu ze zpráv za 30 min (pozdní zpráva shluk doplní;
   starší pohyby se nehlásí — nahrazuje ochranu #744). Dedup v paměti, watermark = start
   procesu (po restartu ztráta, ne duplicita).

Kód: čisté funkce `news-engine/src/gexlens_news/clusters.py` (shluky, významnost, rozhodnutí,
text), `reactions.py` (výchylka, baseline) a `preopen.py` (etapy, sklon, úrovně, text, stav);
`anomaly_job.py` a `preopen_job.py` jsou IO adaptéry. Měření: `scripts/measure_news_anomaly.py`
(replay skutečných jobů nad produkčními daty, včetně simulace víkendů).

## Zvažované varianty

- **p90 bucketu + kontaminace** (dosavadní) — viz kontext.
- **Single-linkage shluk přes všechny zprávy** — při ~2 800 zprávách denně se řetězí
  (max 3 950 zpráv, 707 min).
- **Close-to-close výnos** — míjí whipsaw u FOMC a CPI.
- **Jen percentil denní doby (bez σ)** — dvojnásobek upozornění, v rozjetém dni hlásí vše.
- **Režim z `vol_regime` předchozí seance** — během dne zná jen včerejšek.
- **Okna 15/60 min** — 60 min drží zpoždění; W5 | W15 zvedá počet převážně o souběhy.
- **Q1 A (doslova importance ≥ 2)** — polovina upozornění cituje earnings a přepisy hovorů.
- **Q1 C (jen makro kategorie s importance 3)** — ztratí makro headline s importance 2.
- **Q2 (a) neohlašovat zavřený trh** — víkendové zprávy by zmizely.
- **Q2 souhrn po otevření s gapem** (první volba uživatele 25. 9., implementovaná a zahozená
  týž den) — gap už nastal, na open se podle něj nedá připravit.
- **Dedup předobchodního souhrnu jen watermarkem „start procesu“** (jako u reakcí) — po
  restartu mezi 20:00 a 23:45 by aktualizace nevěděla, co hlavní souhrn vyjmenoval, a souhrn
  zmeškaný výpadkem by se ztratil.
- **Udržovaný seznam svátků CME** — rozpor s ADR-0023 bod 4 (tiše zastará).
- **Výčet a sklon předobchodního souhrnu z celé varianty B** (první implementace) — víkend
  nese ~15–50 významných zpráv a výčet i sklon ovládl šum, který regex povýšil („… Payroll
  Tax“ jako makro, články o inflaci na spořicích účtech); uživatel výběr zpřísnil.
- **Souhrn bez zásadní zprávy s ⚪ „bez zásadní zprávy“** — viz bod 5 (simulace 8 víkendů).

## Důsledky

- Za 14 dní 11.–25. 9. 2026 (`measure_news_anomaly.py`, kurátoři doplnění podle DID):
  **16 upozornění na reakci** (ES 9 / NQ 7) místo 598. Projdou Core PCE, NFP, PPI, CPI,
  FOMC statement a revidovaný UoM 25. 9. na obou instrumentech; tisková konference FOMC až
  shluky 18:33 (ES) a 18:37 (NQ). Neprojdou ISM, ADP, UoM prelim, Retail Sales, Flash PMI.
- Předobchodní upozornění za 8 víkendů 1. 8.–20. 9. 2026 (replay skutečného `PreopenJob`):
  24 zpráv — 5 víkendů hlavní souhrn i aktualizace pro ES i NQ, 2 víkendy (2. a 9. 8.) jen
  23:45 (zásadní zpráva přišla nebo byla zapsána až po 20:00), 1 víkend nic (4.–6. 9. výpadek
  ingestu, zprávy zapsány až v pondělí 05:07 UTC). Zásadních ~1–31 na víkend proti 3–51
  jen významným a 100–2 100 ostatním; výčet ukáže 5, zbytek počtem.
- Významnost headline i směr zprávy stojí dál na regexovém klasifikátoru (kategorie
  a importance se hledají i ve shrnutí) — i přísnější výběr pustí šum s importance 3
  („Why a Doctor … Payroll Tax“ = `MACRO_LABOR`, „Walmart Just Declared War on DoorDash“
  a „Extreme heat spurs earliest-ever Champagne harvest“ = `GEOPOLITICS`) a sklon je hrubý
  („HP Raises Outlook … Tariff Refunds“ = 🔴 kvůli slovu tariff). Přepis importance
  u FF/fed_rss řeší samostatné issue.
- Svátky, které předobchodní upozornění nepokryje (rozvrh je nezná a chybějící bary v tu
  chvíli nejdou odlišit od výpadku enginu): svátek uprostřed týdne (Den díkůvzdání, pondělní
  svátky s přerušením 12:00–17:00 CT, Vánoce/Nový rok v út–čt) a svátek s celodenním
  zavřením v pondělí (bod 5, poprvé 12/2028 — souhrn k nedělnímu otevření, které nenastane).
  O obojím se rozhodne společně.
- Druh `news_open_summary` zanikl dřív, než byl nasazen; nový druh `news_preopen`.
- Sociální sítě jsou významné až pro posty od nasazení (starší nemají `raw.curated`).
- Shluky v prvních ~45 min po otevření nejdou změřit (σ potřebuje 45 minutových výnosů);
  loguje se jejich počet.
