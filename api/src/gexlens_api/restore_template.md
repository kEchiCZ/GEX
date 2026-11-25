# Obnova GEXLens ze zálohy

Balíček (režim `{{mode}}`) vytvořený {{created_utc}} na stroji `{{source_host}}`.

| | |
|---|---|
| Revize image | `{{image_sha}}` (tag při záloze `{{image_tag}}`) |
| Pin pro obnovu | {{image_pin}} |
| PostgreSQL | {{pg_version}} |

{{market_note}}

## Obsah
- `postgres/gexlens.dump`: `pg_dump -Fc` celé databáze;
- `data/`: datový adresář bez `ticks/`, `logs/`, `local/`, `backup/tmp/` a `*.tmp`
  (v režimu `meta` chybí, `data/` zálohuje restic přímo);
- `config/env.template`: klíče `.env` s hodnotami, tajemství prázdná;
- `config/secret-keys.txt`: jména tajemství k doplnění;
- `manifest.json`: velikost a sha256 každého souboru; v taru je **poslední člen**.

Rozsah dní v `data/`:
{{day_ranges}}

## Tajemství k doplnění ze správce hesel
{{secret_keys}}
- na serveru heslo IB Gateway do `secrets/ibkr_password` (ADMIN-MANUAL kap. 14).

## Postup obnovy
`<zdroj>` je tento balíček `.tar`, nebo adresář s tímto souborem (režim `meta`).
1. `git clone` repozitáře, pak {{checkout}}.
2. Z balíčku vyndej návod a konfiguraci: `tar -xf <balíček>.tar RESTORE.md config`
   (Windows `tar.exe` i Linux `tar`, čte celý balíček). U adresáře jsou soubory rovnou v něm.
3. `.env` z `config/env.template`; doplň tajemství výše; {{image_pin}}.
4. {{data_step}}
5. Obnova jedním příkazem z kořene repozitáře: na PC `.\scripts\restore.ps1 -Source <zdroj>`,
   na serveru `scripts/restore.sh <zdroj>`. Obal zastaví engine, news-engine a api; obnova pak
   ověří celý zdroj proti `manifest.json` (změněný bajt = konec, nic se nezapíše), obnoví PG
   i `data/` a obal nastartuje stack a ověří `/status`. Na stroji, který sbírá data, ji spouštěj
   jen při zavřeném trhu nebo v pauze Globexu: engine stojí po celou dobu obnovy.
   Cíl, který už má data, chce `-Overwrite` / `--overwrite`: předtím vznikne pojistný dump
   v `data/local/pred-obnovou/`, změněné soubory v `data/` se přepíšou bez kopie a soubory,
   které zdroj nemá, se přesunou do `data/quarantine/sync-<den>/`. `data/local/` se nepřepisuje.
6. Výpis na konci jmenuje tajemství, která cíli chybí, a připomene nastavení IBKR v Settings.

Podrobnosti a postup bez skriptu: ADMIN-MANUAL kap. 14 „Obnova ze zálohy“.
