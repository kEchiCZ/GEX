# ADR-0031: `news_reactions` v širokém tvaru — jeden řádek per event × symbol

- Stav: přijato (#998, 3. 9. 2026)
- Souvisí: ADR-0026 (view `news_reaction_spread`), ADR-0029 (věčný archiv
  učicích dat), sentiment-SPEC 2.3 a 5.1, #564 (denní okna), #402 (GEX režim)

## Kontext

`news_reactions` byla řádek per (event, symbol, okno): 8 oken × 2 symboly
= 16 řádků na zprávu, PK `(event_id, symbol, window_min)`. Naměřeno 2. 9. 2026
na produkci (PostgreSQL 16, komentář v #998):

| Metrika | Hodnota |
|---|---|
| Řádků | 1 851 570 (271 118 dvojic event × symbol) |
| Heap + index | 164,5 MB + 103,4 MB = **268 MB** (145 B na okno) |
| Bloat indexu | ~45 MB (44 %) — denní okna se vkládají ~14 dní po minutových **doprostřed** klíče → 50/50 page splity; REINDEX to opraví jen dočasně |
| Růst | ~26 k řádků/den → **~1,4 GB/rok** |
| Čtenáři | každý (`window_min = 5 AND symbol = 'ES' AND NOT contaminated`) skenuje 155 MB, což se nevejde do `shared_buffers` (128 MB) |

Tabulka je učicí data (ADR-0029): žádná retence, nic se nesmí ztratit. Jiné
varianty z analýzy: `float8 → real` (−14 %, formálně mění bity), REINDEX (−17 %,
bloat se vrací), komprese (0 — nic se netoastuje).

Klíčové zjištění, které široký tvar umožňuje: `deferred`, `gex_regime`
i `computed_at` jsou **konstantní per (event, symbol, fáze)** — minutová fáze
(1–60 min) se počítá naráz, denní (1d–10d, #564) naráz o ~14 dní později.
Ověřeno nad celou produkční tabulkou (0 dvojic s více hodnotami v obou
fázích). `contaminated` se liší per okno. `vol_z` je podle SPEC 5.1 u denních
oken vždy NULL (objemová baseline per minuta dne nemá pro denní horizont
smysl) — v produkci 0 vyplněných z 882 968 denních řádků.

## Rozhodnutí

Varianta **B1**: jeden řádek per `(event_id, symbol)`, `float8` zachován
(přísně bezeztrátové, žádná debata o bitech):

| Sloupce | Význam |
|---|---|
| `ret_<w>`, `range_<w>`, `cont_<w>` pro w ∈ {1, 5, 15, 60, 1440, 2880, 7200, 14400} | okno; nezměřené okno (chybí bar) = NULL v celé čtveřici |
| `vol_z_<w>` jen pro w ∈ {1, 5, 15, 60} | denní okna sloupec nemají — SPEC 5.1 je definuje jako NULL, migrace to ověřuje (100 % NULL, jinak končí chybou) |
| `deferred_min`, `deferred_daily`, `regime_min`, `regime_daily`, `computed_at_min`, `computed_at_daily` | metadata per fáze; `computed_at_<fáze> IS NULL` = fáze zatím nespočítaná |

- Zápis: minutová fáze `INSERT` řádku, denní fáze `UPDATE` téhož řádku
  (HOT update — žádný indexovaný sloupec se nemění); řádek jen s denní fází
  vzniká u historických eventů před pokrytím minutových barů (~27 k dvojic),
  proto obě fáze jdou jednou upsert cestou (`_write_phase`).
- Pending dotazy: minutová fáze = event bez jakéhokoli řádku (stejně jako
  dřív — jinak by se historické jen-denní dvojice vybíraly donekonečna, past
  #655); denní fáze = žádný řádek eventu s `computed_at_daily IS NOT NULL`.
- Čtenáři sahají na sloupce přes helpery v `storage/sentiment.py`
  (`reaction_ret(w)`, `reaction_contaminated(w)`, …); konzumenti, kteří
  potřebují všechna okna, rozkládají řádek přes `unpivot_reaction` na
  `ReactionWindow` (tvar bývalého řádku). API `GET /news/{id}` vrací reakce
  dál jako řádky per (symbol, okno) — frontend se nemění.
- View `news_reaction_spread` (ADR-0026) je `UNION ALL` per okno nad širokým
  tvarem, sloupce a typy zůstávají.
- Žádný trvalý kompatibilní view pod jménem `news_reactions` — cílový stav je
  čistý široký tvar.

Očekávaný efekt (naměřeno CTAS kopií): heap 49,8 MB + index ~6 MB
= **~56 MB (−79 %)**, růst **~0,25–0,3 GB/rok** místo ~1,4 GB; čtenáři
skenují 3× méně a tabulka se vejde do cache; bloat indexu zmizí (obě fáze
zapisují na konec indexu, denní fáze index nemění).

## Migrace

Skript `scripts/migrate_news_reactions_wide.py` (ručně, idempotentní,
`--dry-run`), jedna transakce:

1. Předpoklady nad starou tabulkou: jen známá okna, `ret_bp`/`range_bp` bez
   NULL, `vol_z` denních oken 100 % NULL, `deferred`/`gex_regime`/`computed_at`
   konstantní per (event, symbol, fáze).
2. Nová tabulka pod dočasným jménem, naplnění pivotem v SQL (`max(CASE …)`
   per okno — hodnota se vybírá, nepočítá).
3. Ověření: počet řádků = počet dvojic; počet non-NULL buněk per okno a per
   sloupec = počet starých řádků okna; součty `ret`/`range`/`vol_z` per okno
   s relativní tolerancí 1e-6 (pořadí sčítání float8), počty kontaminovaných
   přesně; metadata fází i jednotlivé buňky množinově přesně (EXCEPT oběma
   směry).
4. Teprve po úspěchu: stará → `news_reactions_legacy_<datum>`, nová →
   `news_reactions`, PK/FK na kanonická jména, view znovu; při jakékoli
   neshodě rollback a nic se nepřejmenuje.

Startup: `ensure_sentiment_schema` starý tvar rozpozná a news-engine skončí
s chybou odkazující na skript — nesmí do starého tvaru tiše psát ani vedle
něj založit prázdnou širokou tabulku. Engine (píše jen `news_events`) chybu
loguje a běží dál. Čerstvá DB se zakládá rovnou široká.

Stará tabulka se **nikdy nemaže automaticky** — po týdnu provozu ji smaže
uživatel ručně (`DROP TABLE news_reactions_legacy_<datum>`).

Rollback: nasadit předchozí verzi news-engine + API a tabulky přejmenovat
zpět (`ALTER TABLE news_reactions RENAME TO news_reactions_wide_YYYYMMDD;
ALTER TABLE news_reactions_legacy_<datum> RENAME TO news_reactions;` + rename
PK/FK zpět na `news_reactions_pkey` / `news_reactions_event_id_fkey`,
`CREATE OR REPLACE VIEW news_reaction_spread` ze starého kódu). Okna
naměřená širokým tvarem mezitím by se musela rozpivotovat ručně — proto se
legacy tabulka drží, dokud provoz není ověřený.

## Důsledky

- Konfigurace oken `ReactionJob` musí být podmnožinou sloupců; jiné okno je
  chyba při startu, ne tichý zápis mimo.
- Přidání nového okna = nový sloupec (`ADD COLUMN`, aditivní migrace) místo
  nové hodnoty `window_min`.
- Testy vkládají široké řádky přes `reaction_row_values` — stejná cesta jako
  produkční zápis, takže invariant fází hlídá i testovací data.
