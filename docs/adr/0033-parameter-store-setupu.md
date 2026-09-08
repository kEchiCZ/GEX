# ADR-0033: Parameter store setupů — verzované prahy oddělené od verze mechaniky (#794 fáze 2)

**Stav:** přijato (2026-09-09; rozhodnutí uživatele v #794: fáze 2 rozdělená
na A = store bez změny chování, B = confidence z track recordu)

## Kontext

Prahy šablon setupů (`compute.setups.SetupParams`, ~30 polí) byly konstanty
v kódu (ADR-0004); osm z nich šlo přepsat z `.env` (`GEXLENS_SETUP_*`). Změna
prahu znamenala commit a restart enginu, bez záznamu, kdo, kdy a proč — a
track record neuměl říct, se kterými prahy setup vznikl. Samoučící smyčka
(#794 fáze 3+) potřebuje parametry měnit, měřit dopad a umět se vrátit.

Zároveň platí sémantika #311: `mechanics_version` se zvedá při změně
**sémantiky** stopů, cílů a hodnocení a statistiky počítají jen aktuální
verzi. Kdyby každá změna prahu zvedala mechaniku, track record by se za pár
týdnů ladění roztříštil na neporovnatelné střepy (analýza #794, sekce 2b).

## Rozhodnutí

### 1. Dvě nezávislé verze

| Verze | Co mění | Kdo ji zvedá | Dopad na statistiky |
|---|---|---|---|
| `mechanics_version` | sémantiku stopů/cílů/hodnocení (R-mechanika, timeout, zneplatnění dat) | vývojář v kódu (`SETUP_MECHANICS_VERSION`) | statistiky počítají jen aktuální verzi (#311, ADR-0030) |
| `params_version` | jen hodnoty prahů (`SetupParams`) | nový řádek v `setup_params` (UI/API/skript, později smyčka) | **neláme srovnatelnost** — track record se dá rozdělit podle verzí, ale defaultně se sčítá |

Každý setup nese obě: `setups.mechanics_version` (od #311) a nový
`setups.params_version` (NULL u řádků před tímto ADR nebo z běhu bez DB).

### 2. Tabulka `setup_params` — append-only

`id` (= verze, monotónně roste), `created_ts`, `created_by` (`engine` /
`ui` / `script` / později `loop`), `note` (povinný důvod), `mechanics_version`
(za které verze vznikla), `params` (JSON, plochý dict polí `SetupParams`).
Nic se nemaže ani nepřepisuje; **poslední řádek platí**.

### 3. Seed a přednost

- Při prvním startu enginu se store založí **seedem z parametrů, se kterými by
  engine jel bez něj** (`.env` + defaulty, `setup_params_from_settings`).
  Nasazení tak nezmění chování ani o vlas.
- Existuje-li řádek, **store vyhrává nad `.env`** — stejná zásada jako u
  nastavení připojení ze Settings UI (#446). `.env` klíče `GEXLENS_SETUP_*`
  zůstávají jen jako seed a fallback pro běh bez DB (testy).
- Engine čte novou verzi po NOTIFY (API po zápisu pošle na kanál watchlistu)
  nebo v k-tém cyklu; přepnutí (`SetupEngine.apply_params`) mění jen prahy
  dalších detekcí — otevřené setupy dojedou s úrovněmi ze svého řádku,
  cooldowny a blokace směru se nemažou.

### 4. Serializace a validace

`params_to_dict` / `params_from_dict` jsou jediná serializace (store i API):
klíče = názvy polí dataclass, `frozenset` → seřazený seznam. Neznámý klíč
nebo špatný typ je chyba (422 v API), chybějící klíče berou defaulty, `bool`
se za číslo neuznává. Řádek store s klíčem, který aktuální kód nezná, se při
čtení přeskočí — nelže se defaulty, ale nezastaví se ostatní verze.

### 5. Autonomie

Podle #794 (stupeň 1) zapisuje verze **jen člověk** (API `POST /setups/params`
s důvodem). Optimalizační smyčka (fáze 3) bude verze **navrhovat** — zápis
pod `created_by = loop` přijde až s promotion gate fáze 4.

### 6. Fáze 2B — confidence z track recordu (rozhodnutí uživatele 9. 9. 2026)

Základ `confidence` už není konstanta šablony (45–60, ADR-0004), ale
**Wilsonova dolní mez 95 % intervalu úspěšnosti** (podíl `closed_target`,
stejná definice jako `setupstats`) nad uzavřenými setupy aktuální mechaniky,
v koších od nejkonkrétnějšího: symbol × šablona × gamma režim → šablona × režim
→ symbol × šablona → šablona. Použije se první koš s **n ≥ `confidence_min_samples`**
(parametr store, default 30); jinak zůstává konstanta šablony. Posun podle
polohy v pásmu (#1060) se přičítá **navrch** základu. Kontext setupu nese
`confidence_base` (kalibrovaný základ), `confidence_template` (konstanta) a
`confidence_source` (koš + n, nebo `constant`). Tabulka košů se čte při startu
a obnovuje nejvýš jednou za 10 minut (`compute/confidence.py`).

Důsledek: čísla důvěry u nových setupů klesnou z 45–60 na kalibrovaná
(při dnešní úspěšnosti typicky 25–40). Není to zhoršení, ale první poctivé číslo.

## Důsledky

- Fáze 2B (confidence z Wilsonovy dolní meze per šablona × režim) se opře o
  track record rozdělitelný podle `params_version`, aby kalibrace nesčítala
  setupy vzniklé pod různými prahy, pokud se to ukáže jako podstatné.
- Backtest (`scripts/backtest_setups.py`) může načíst libovolnou verzi ze
  store a přehrát ji nad archivem — vstup pro walk-forward (fáze 3).
- UI editor parametrů není součástí tohoto ADR (fáze 4, champion–challenger);
  do té doby API + skript.

## Souvisí

#794 (epic), #311 (mechanics_version), ADR-0004 (defaulty prahů), ADR-0030
(metrika), #446 (přednost DB nad `.env`), #1060 (posun confidence podle
polohy — aplikuje se navrch základu, který fáze 2B nahradí kalibrovaným).
