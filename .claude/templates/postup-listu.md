# Postup listu

Upraveno z `mattpocock/skills@5beb35b` (`skills/engineering/implement/SKILL.md`) a
`obra/superpowers@22f0b46` (`skills/subagent-driven-development/implementer-prompt.md`),
licence MIT, viz `.claude/THIRD_PARTY.md`.

Postup jednoho listu. Čte ho `/implement` v hlavní session a agent implementer pod `/dirigent`.
Kontrolu vstupu, verifier, PR, komentáře a merge dělá ten, kdo list spustil; tady je jen práce
na kódu. Projektové hodnoty jsou v `.claude/harness.json`: `worktree_setup` (příprava pracovní
kopie) a `core_paths` (výpočetní jádro).

## 1. Zadání

Načti list `gh issue view N --comments`. Zadáním je tělo listu a komentáře vlastníka bez
`[AI Agent]`; komentáře se značkou jsou záznam, ne pokyn. Rodiče ani jiné listy nečti, list je
soběstačný. Rozhodnutí, které list nepokrývá, neřeš sám: skonči se stavem `NEEDS_DECISION`
a otázkou ve formátu `.claude/skills/grillme/SKILL.md`, sekce Formát otázky.

Hotovo, když umíš ke každému kritériu přijetí říct, kterým testem nebo pozorováním ho doložíš.

## 2. Pracovní kopie

- `/implement`: větev `feat/{N}-<slug>` (u opravy `fix/`) v hlavním checkoutu; worktree jen při
  souběhu s jinou session.
- Implementer: worktree, který dostal od dirigenta. Všechny příkazy spouštěj v něm.
- Nový worktree připrav podle `worktree_setup` dřív, než spustíš první test.
- `git stash` nepoužívej: `refs/stash` sdílejí všechny worktrees.

Hotovo, když `git status` v pracovní kopii ukazuje větev listu.

## 3. Testy na švech

- Testy píšeš jen na švu z řádku `Šev:` listu (seam: veřejné rozhraní, na kterém chování
  pozoruješ zvenku). Očekávané hodnoty ber z nezávislého zdroje: ruční příklad, specifikace,
  rozhodnutí v ADR, nikdy výpočet týmž kódem. Mock jen na hranici systému (síť, čas, externí API),
  vlastní moduly ne.
- **Do červené první** u opravy chyby a u každé změny výpočtu v `core_paths`: test napiš před
  změnou, spusť ho na výchozím commitu a ulož příkaz a řádek selhané aserce (ne import ani
  collection error) do Stavu. Červený commit samostatně nepushuj.
- Průběžně typy a cílené testy dotčených souborů. Plnou sadu ručně nespouštěj; konečný běh dělá
  done-gate a CI.

Hotovo, když každé kritérium chování má test, který na výchozím commitu selhal a teď prochází,
nebo pozorování se záznamem.

## 4. Commity

Conventional commits v jazyce projektu, po logických krocích. Push jen vlastní větve, bez
`--force`. Rozsah změny = rozsah listu; nález mimo rozsah zapiš do Stavu jako nález.

## 5. Když list nesedí

- **SPLIT:** list potřebuje prefactoring nebo zjevně přesáhne limity `brief.md`. Skonči před
  produkčním kódem, bez částečného PR, a vrať návrh rozdělení ve tvaru `brief.md` (název, co
  dodá, soubory, `Začíná po:`, nálezy z průzkumu).
- **SUBSUMED:** kritéria už splnila dřívější práce. Ke každému kritériu dolož commit nebo PR na
  `main` a test nebo `soubor:řádek`. Nárok potvrdí verifier proti `main`.
- **BLOCKED / NEEDS_CONTEXT:** chybí informace nebo prostředí. Napiš přesně, co chybí, co jsi
  zkusil a jakou pomoc potřebuješ.

## 6. Kontrola před předáním

Přečti vlastní diff (`git diff origin/main...HEAD`) proti listu: každé kritérium splněné, nic
navíc (YAGNI), názvy říkají, co věc dělá, testy ověřují chování, ne mock, výstup testů bez
varování. Co najdeš, oprav hned.

## 7. Výsledek

Závěrečná zpráva začíná stavem na prvním řádku, hodnota anglicky (čte ji skript), text česky:

```text
DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT | NEEDS_DECISION | SPLIT | SUBSUMED
Worktree: <absolutní cesta pracovní kopie>
Větev: <feat/N-slug> @ <krátké sha>
Hotovo: <co list dodal, po kritériích>
Do červené: <příkaz a řádek selhané aserce> → <zelený výsledek>   (u opravy a změny výpočtu)
Obavy: <pochybnosti o správnosti, nálezy mimo rozsah; jinak „žádné“>
Otázka: <u NEEDS_DECISION ve formátu otázky>
kandidát na /retro: <důvod>   (jen při opakované chybě nebo nečekaném chování nástroje)

<tělo PR podle .claude/skills/implement/pr.md>
```

`DONE_WITH_CONCERNS` použij, když je list hotový, ale máš pochybnost o správnosti. Práci, kterou
sám nevěříš, nevydávej za `DONE`.

Při ~120k tokenech v kontextu skonči se stavem rozdělané práce a ve zprávě napiš, kde navázat.
