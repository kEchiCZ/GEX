---
name: epika
description: Lehký spec-driven postup GEXLens pro větší funkce a epiky (místo GitHub spec-kit) — zadání epiky v issue, vyjasnění (nejvýš 5 otázek s variantami a doporučením), rozpad na sub-issues velikosti jedné session, práce po sessions s bodem navázání a kontrola shody kód ↔ zadání nezávislým ověřovatelem před PR. Použij, když vlastník zadává novou větší funkci nebo epiku, když se má zadání rozpadnout na issues, když začíná nebo končí session nad sub-issue epiky a před otevřením PR epiky. Nepoužívej pro malé opravy (jeden PR, jasná příčina) — ty jdou běžným tokem z AGENTS.md.
---

# Epika — lehký spec-driven postup

Doplňuje `AGENTS.md` (zdroj pravdy), nic z něj nemění: R1–R6, kritická pravidla, DoD, větve
`feat/{N}-slug`, squash merge přes `scripts/merge-when-green.sh` platí beze změny.
Zadání žije v **těle issue** — vlastník ho schvaluje a komentuje z telefonu; soubory se zadáním
se do repa nezakládají (jediný zdroj, žádný další dokument, který by se rozjel s kódem).

## Kdy ano, kdy ne
- **Ano:** funkce na víc PR, nové architektonické rozhodnutí, nejasné nebo „syrové" zadání
  (screenshot, odkaz, pár vět), převzetí funkce odjinud.
- **Ne:** oprava chyby nebo malá změna na jeden PR s jasnou příčinou → běžný tok
  (issue → `fix/{N}-slug` → PR s kostrou Příčina / Oprava / Testy / Po nasazení / Dokumentace).

## Trvalé zásady vlastníka
1. **Nejasnost → zeptej se**, nehádej (AskUserQuestion, doporučená varianta první).
2. **Rozpor stávající funkce GEXLens × funkce převzatá odjinud** (referenční aplikace, článek,
   screenshot) → zeptej se; nikdy mlčky nepřepisuj chování, na které je vlastník zvyklý.
3. **Každé rozhodnutí = varianty s výhodami/nevýhodami + doporučení.** Doporučení se řídí:
   - **rychlost** — latence UI, čas od otevření aplikace k rozhodnutí, „kliknu a je hotovo";
   - **výkon** — CPU/RAM produkčního notebooku (16 GB, WSL 6 GB), strop 100 market data lines;
   - **relevance dat pro obchodní rozhodnutí** — point-in-time bez look-ahead, čerstvost a stáří
     viditelně, chybějící data = viditelná mezera (ne zmrzlá čísla), měřené > odhadnuté.
4. **Práce po sessions:** jeden sub-issue = jedna session = jeden PR. Session končí v bodě
   navázání, nikdy uprostřed rozpracované změny bez commitu a komentáře „Stav".
5. **Veřejné repo / clean-room:** jména referenčních aplikací, jejich screenshoty a srovnání
   nepatří do kódu, docs, commitů ani issues (konvence „referenční aplikace"); funkce se stavějí
   z veřejných popisů a vlastního kódu, nikdy z rozboru cizí binárky.

## Krok 1 — Zadání epiky
- Syrové zadání přepiš do šablony [sablona-epiky.md](sablona-epiky.md) jako tělo epiky (nové issue,
  nebo úprava těla po souhlasu vlastníka). Delší texty do `gh` vždy přes `--body-file`.
- **Výchozí stav** dohledej v kódu a issues (tvrdá data, `path:line`, `gh issue view`), ne z paměti.
- Před založením hledej existující issue (`gh issue list --search …`); co už existuje, použij
  a doplň, nezakládej paralelní.

## Krok 2 — Vyjasnění
- Projdi zadání proti kódu, AGENTS.md, SPEC a ADR. Hledej: nejasnosti, rozpory se stávající
  funkcí, kolize s R1–R6 a kritickými pravidly (zavřený trh, obchodní den, data se nemažou,
  100 lines), dopad na výkon a na relevanci dat.
- Nejvýš **5 otázek najednou**; u každé kontext, varianty (+/−), doporučení podle zásady 3
  a co otázka blokuje. Neblokující otázka → do epiky jako `needs-decision`, práce běží dál.
- Otázky patří do sekce **Otevřené otázky k rozhodnutí**, odpovědi do sekce **Rozhodnuto**
  v těle epiky (datum, kdo rozhodl); architektonické rozhodnutí → ADR (číslo navazuje) v PR
  s labelem `needs-decision`.

## Krok 3 — Rozpad na sub-issues
- **Velikost:** sub-issue se vejde do jedné session — orientačně jeden subsystém, ≤ ~8 souborů,
  ≤ ~600 řádků diffu bez golden dat, ≤ ~60 % kontextu. Větší → rozděl (např. výpočet + golden /
  API / UI zvlášť). ADR je vlastní sub-issue **před** implementací, kterou rozhoduje.
- Šablona [sablona-sub-issue.md](sablona-sub-issue.md). Labely: `prio:P0–P3`, právě jeden
  `epic:*`, `needs-decision` u rozhodnutí. Navázat jako GitHub sub-issue epiky, závislosti
  „Blokováno: #N".
- Pořadí podle hodnoty pro obchodování a rizika; co dává hodnotu samo, jde dřív.
- V těle epiky aktualizuj sekci **Fázovaný plán** (čísla sub-issues, pořadí, závislosti, sessions).

## Krok 4 — Session nad sub-issue
**Start:** přečti sub-issue, epiku a poslední komentář „Stav"; ověř skutečný stav (`gh issue view`,
`git log`, `git status`) — ne ze souhrnu. Větev `feat/{N}-slug` (nebo pokračuj v existující).

**Během:** rozsah = rozsah sub-issue; nález mimo rozsah → nové issue s `prio:*`. Testy podle
AGENTS.md (golden při změně výpočtu, test na víkend / denní pauzu / svátek u hlídačů a dnů).

**Konec session nebo ~60 % kontextu** (co nastane dřív):
1. commit + push do větve (i rozpracované — WIP commit v PR větvi je v pořádku, squash ho smaže);
2. komentář do sub-issue přes `--body-file`:
   ```
   ### Stav (DD. MM.)
   - Hotovo: …
   - Zbývá: …
   - Další krok: … (soubor, funkce, příkaz)
   - Rozhodnutí / otevřené otázky: …
   - Jak ověřit: …
   ```
3. nabídni novou session; další session začíná krokem 4 / Start.

## Krok 5 — Kontrola před PR (nezávislý ověřovatel)
- Spusť subagenta (nástroj Agent) **bez kontextu implementace** s promptem z
  [kontrola.md](kontrola.md) a s čísly sub-issue a epiky. Ověřovatel nezná tvoje úvahy, jen
  zadání, diff a repo — tím chytí, co sebekontrola přehlédne.
- Každý nález oprav, nebo v PR věcně zdůvodni, proč ne. PR podle šablony v
  [sablona-sub-issue.md](sablona-sub-issue.md) dostane sekci **Kontrola zadání** (nálezy a jejich
  řešení); `Closes #N`, u částečné fáze `Refs #N`.
- Až pak DoD z AGENTS.md (testy, lint, mypy, build, docs ve stejném PR) a merge-when-green.

## Krok 6 — Uzavření epiky
- Po posledním sub-issue spusť ověřovatele nad **celou epikou** (zadání × zavřené sub-issues ×
  stav kódu × manuál/ADR/SPEC kap. 11).
- Odškrtni akceptační kritéria v těle epiky, doplň roadmapu a manuál (#628), pak epiku zavři.
  Neodškrtnutá AC u zavřené epiky jsou drift.
- Netriviální poučení → záznam do `docs/lessons-learned.md`.
