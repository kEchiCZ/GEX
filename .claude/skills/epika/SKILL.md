---
name: epika
description: Lehký spec-driven postup GEXLens pro větší funkce a epiky (místo GitHub spec-kit) — zadání epiky v issue, vyjasnění otázkami s variantami a doporučením, schválení vlastníkem, rozpad na sub-issues velikosti jedné session, práce po sessions s průběžnými commity a bodem navázání, kontrola shody kód ↔ zadání nezávislým ověřovatelem před PR. Použij, když vlastník zadává novou větší funkci („chci, aby aplikace uměla…", „převezmi funkci…", screenshot nebo odkaz jako zadání), když se má zadání rozepsat do issues, když se pokračuje nebo navazuje na fázi či sub-issue epiky („pokračuj na #N", „navaž"), a před otevřením PR epiky. Nepoužívej pro malé opravy (jeden PR, jasná příčina) — ty jdou běžným tokem z AGENTS.md.
argument-hint: "[číslo issue epiky nebo sub-issue]"
---

# Epika — lehký spec-driven postup

Doplňuje `AGENTS.md` (zdroj pravdy) a nic z něj nemění — zásady pro rozhodování (ptát se, varianty,
kritéria rychlost · výkon · relevance dat) jsou v AGENTS.md „Rozhodování" a platí i tady.
Zadání žije v **těle issue** — vlastník ho schvaluje a komentuje z telefonu; soubory se zadáním
se do repa nezakládají (jediný zdroj, žádný další dokument, který by se rozjel s kódem).

## Kdy ano, kdy ne
- **Ano:** funkce na víc PR, nové architektonické rozhodnutí, nejasné nebo „syrové" zadání
  (screenshot, odkaz, pár vět), převzetí funkce odjinud.
- **Ne:** oprava chyby nebo malá změna na jeden PR s jasnou příčinou → běžný tok z AGENTS.md.

## Clean-room a veřejné repo
- Jména referenčních aplikací, jejich screenshoty a srovnání nepatří do kódu, docs, commitů ani
  issues (konvence „referenční aplikace", #482). Funkce se stavějí z veřejných popisů a vlastního
  kódu — nikdy z dekompilace, rozbalování instalátoru, odposlechu cizí aplikace ani obcházení
  licence či předplatného. Žádá-li to zadání, řekni nahlas, že tuto část neuděláš, a nabídni
  clean-room postup.
- Obsahuje-li syrové zadání jméno nebo screenshoty referenční aplikace, epika vznikne jako
  **nové očištěné issue** (cizí UI popsat slovy); syrové issue se odkáže na epiku a vlastníkovi
  se navrhne jeho úprava nebo uzavření. Commity a PR odkazují na očištěné issue.

## Krok 1 — Zadání epiky
- Syrové zadání přepiš do šablony [sablona-epiky.md](sablona-epiky.md) jako tělo epiky.
- **Výchozí stav** dohledej v kódu a issues (tvrdá data, `path:line`), ne z paměti.
- Před založením hledej existující issue (`gh issue list --search …`); co už existuje, použij
  a doplň, nezakládej paralelní.
- Tělo issue upravuj vždy z čerstvé kopie (vlastník mohl mezitím editovat z telefonu):
  `gh issue view <E> --json body -q .body > epika.md` → úprava → `gh issue edit <E> --body-file epika.md`.
  Tělo drží jen aktuální stav (≤ ~25 000 znaků, čte se z telefonu); předchozí znění revize jde do
  komentáře, nahoru krátká poznámka `> Revize D. M.: …`.

## Krok 2 — Vyjasnění a schválení
- Projdi zadání proti kódu, AGENTS.md, SPEC a ADR. Hledej: nejasnosti, rozpory se stávající
  funkcí, kolize s R1–R6 a kritickými pravidly, dopad na výkon a na relevanci dat.
- Otázky: v chatu nejvýš 4 najednou (limit AskUserQuestion, doporučená varianta první), v issue
  nejvýš 5; u každé kontext, varianty (+/−), doporučení a co otázka blokuje.
- **Každou odpověď hned zapiš do sekce Rozhodnuto** v těle epiky (datum, volba) — odpověď v chatu
  s koncem session zmizí. Architektonické rozhodnutí → ADR (číslo navazuje) s `needs-decision`.
- Čekající otázka = label `needs-decision` na epice (vlastník je vidí ve filtru
  `is:open label:needs-decision`; na komentáře pod vlastním účtem GitHub neupozorní). Label se
  odebere, až je sekce Otevřené otázky prázdná.
- **Schválení:** Krok 3 začíná až po komentáři vlastníka v epice („Schvaluji", případně s výhradami);
  datum zapiš do Rozhodnuto. Bez schválení session končí bodem navázání.

## Krok 3 — Rozpad na sub-issues
- **Velikost (odhad předem):** jedna vrstva (engine | API | frontend) + testy; ≤ ~8 měněných
  souborů a ≤ ~600 řádků diffu bez golden dat; nejvýš 2 soubory nad 1 500 řádků ke čtení
  (`__main__.py`, `App.tsx`, `Heatmap.tsx`, `setups.py`…); nasazení a ověření na devu = půl session.
  ADR je vlastní sub-issue **před** implementací, kterou rozhoduje.
- Šablona [sablona-sub-issue.md](sablona-sub-issue.md); každý sub-issue má právě jeden `prio:*`
  a jeden `epic:*`. Navázání jako GitHub sub-issue (`id` není číslo issue):
  `gh api -X POST repos/kEchiCZ/GEX/issues/<E>/sub_issues -F sub_issue_id=$(gh api repos/kEchiCZ/GEX/issues/<N> --jq .id)`
- Pořadí podle hodnoty pro obchodování a rizika; co dává hodnotu samo, jde dřív.
- V těle epiky aktualizuj sekci **Fázovaný plán** (čísla sub-issues, pořadí, závislosti, sessions).

## Krok 4 — Session nad sub-issue
**Start:**
1. přečti sub-issue, epiku, poslední „Stav" **a všechny komentáře po něm** (odpovědi vlastníka
   přenes do Rozhodnuto); ověř skutečný stav (`git fetch`, `git log origin/main..HEAD`,
   `git status`, `git stash list`) — ne ze souhrnu;
2. větev `feat/{N}-slug` nebo `fix/{N}-slug` (nebo pokračuj v existující);
3. do sub-issue napiš komentář `### Plán session (D. M.)` s očíslovanými kroky.

**Během:** po každém dokončeném kroku commit + push (`wip(scope): <krok> — další: <krok>`; zprávy
WIP commitů skončí v těle squash commitu, piš je srozumitelně). Rozsah = rozsah sub-issue; nález
mimo rozsah → nové issue s `prio:*`. Testy podle AGENTS.md (Build & validace, Kritická pravidla).

**Konec session** (hotovo, nebo ~60 % kontextu — co nastane dřív; při ~70 % nabídni novou session):
commit + push a komentář přes `--body-file` (nikdy `--edit-last` — vlastník píše pod stejným účtem):
```
### Stav (D. M.)
- Hotovo: …
- Zbývá: …
- Další krok: … (soubor, funkce, příkaz)
- Rozhodnutí / otevřené otázky: …
- Jak ověřit: …
```
Když session skončí náhle bez „Stavu", další session ho zrekonstruuje z posledního „Plánu session"
a git logu.

Cíl je 1 sub-issue = 1 PR. Přesah = další session na stejné větvi; částečný PR nedělej — zbytek,
který se nevejde, přesuň do nového sub-issue.

## Krok 5 — Kontrola před PR (nezávislý ověřovatel)
Pořadí: implementace + testy + dokumentace (DoD 1–3 z AGENTS.md) → commit + push → **ověřovatel**
→ opravy → PR → DoD 4 (nasazení na dev a ověření naživo u engine/API) → merge-when-green.
- Ověřovatel = subagent (nástroj Agent) **bez kontextu implementace**, prompt z [kontrola.md](kontrola.md)
  s čísly sub-issue a epiky. Nezná tvoje úvahy, jen zadání, diff a repo.
- Každý nález oprav, nebo v PR věcně zdůvodni, proč ne; PR podle šablony v
  [sablona-sub-issue.md](sablona-sub-issue.md) má sekci **Kontrola zadání**. `Closes #<sub-issue>`,
  nikdy `Closes #<epika>` — epiku zavírá jen krok 6.
- PR s labelem `needs-decision` (ADR) se merguje až po rozhodnutí vlastníka v PR nebo issue.

## Krok 6 — Uzavření epiky
- Po posledním sub-issue spusť ověřovatele nad **celou epikou** (zadání × zavřené sub-issues ×
  stav kódu × manuál/ADR/SPEC kap. 11).
- **Ověř** (nedoplňuj), že roadmapa #629 a manuál #628 jsou aktuální — chybějící = drift z PR
  sub-issue, oprav ho samostatným PR. Odškrtni akceptační kritéria v těle epiky, pak ji zavři.
- Netriviální poučení → záznam do `docs/lessons-learned.md`.

## Stará epika ve starém formátu
Tělo nepřepisuj. Až se na ní pokračuje, doplň s revizní poznámkou chybějící sekce (Fázovaný plán
s čísly, Rozhodnuto) a existující fáze navaž jako sub-issues. Je-li epika zavřená a fáze otevřené,
znovu ji otevři.
