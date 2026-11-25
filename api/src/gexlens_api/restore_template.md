# Obnova GEXLens ze zálohy

Balíček (režim `{{mode}}`) vytvořený {{created_utc}} na stroji `{{source_host}}`.

| | |
|---|---|
| Revize image | `{{image_sha}}` (tag při záloze `{{image_tag}}`) |
| Pin pro obnovu | `GEXLENS_IMAGE_TAG={{image_pin}}` |
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
Ruční postup; obnova jedním příkazem přijde s #1800.
1. `git clone` repozitáře a `git checkout {{image_sha}}` (u `dev` aktuální `main`).
2. `.env` z `config/env.template`; doplň tajemství výše; `GEXLENS_IMAGE_TAG={{image_pin}}`
   vrátí stejnou revizi image.
3. Rozbal data do kořene repozitáře: `tar -xf <balíček>.tar data` (Windows `tar.exe` i Linux `tar`).
4. Spusť jen PostgreSQL: `docker compose up -d postgres`.
5. Obnov dump:
   `docker compose cp <rozbalený>/postgres/gexlens.dump postgres:/tmp/gexlens.dump`, pak
   `docker compose exec postgres pg_restore --clean --if-exists --no-owner -U gexlens -d gexlens /tmp/gexlens.dump`.
6. `docker compose up -d` a ověř `curl http://127.0.0.1:8010/status`.
7. Kontrola: sha256 souborů proti `manifest.json` (`sha256sum`, ve Windows `Get-FileHash`);
   soubory s `live: true` se po záloze mohly dál měnit.
