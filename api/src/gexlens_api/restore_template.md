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
Ruční postup; obnova jedním příkazem přijde s #1800. `<rozbalený>` je pracovní adresář
s obsahem balíčku (režim `full`: `mkdir rozbaleny` a `tar -xf <balíček>.tar -C rozbaleny`,
Windows `tar.exe` i Linux `tar`; režim `meta`: adresář s tímto souborem).
1. `git clone` repozitáře, pak {{checkout}}.
2. `.env` z `<rozbalený>/config/env.template`; doplň tajemství výše; {{image_pin}}.
3. {{data_step}}.
4. Spusť jen PostgreSQL: `docker compose up -d postgres`.
5. Obnov dump:
   `docker compose cp <rozbalený>/postgres/gexlens.dump postgres:/tmp/gexlens.dump`, pak
   `docker compose exec postgres pg_restore --clean --if-exists --no-owner -U gexlens -d gexlens /tmp/gexlens.dump`.
6. `docker compose up -d` a ověř `curl http://127.0.0.1:8010/status`.
7. Kontrola: sha256 souborů proti `manifest.json` (`sha256sum`, ve Windows `Get-FileHash`);
   soubory s `live: true` se po záloze mohly dál měnit.
