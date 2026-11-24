---
name: implement
description: "Implementuje jeden list (issue #N) v této session: kontrola vstupu, kód, verifier, draft PR, merge."
argument-hint: "#N"
disable-model-invocation: true
hooks:
  Stop:
    - hooks:
        - type: command
          command: python
          args: ["-I", "${CLAUDE_PROJECT_DIR}/.claude/hooks/done-gate.py"]
---

Upraveno z `mattpocock/skills@5beb35b` (`skills/engineering/implement/SKILL.md`) a
`obra/superpowers@22f0b46` (`skills/subagent-driven-development/implementer-prompt.md`),
licence MIT, viz `.claude/THIRD_PARTY.md`.

Jeden list v této session. Model a effort zvolil vlastník při startu (`claude --model sonnet
--effort medium`, list v `core_paths` z `.claude/harness.json` `--model opus --effort high`).
Listy rodiče po jednom vede `/dirigent`. Naráz běží jen jedna `/implement` session. Na konci
každé odpovědi běží done-gate (`scripts/check.py --changed`); jeho výstup oprav, neobcházej.

## 1. Kontrola vstupu

1. `git fetch origin`, pak `python -I .claude/skills/spec/check-brief.py --mode implement N`.
   - Exit 1: kód nepiš. Vypiš důvody a nabídni `/grillme #N` (zadání neprošlo grillingem nebo
     schválením) nebo návrat listu do `/spec` (vadný list).
   - Exit 2: chyba okolí, vypiš ji a skonči.
2. Label `in-progress` nebo otevřený PR listu (`gh pr list --search "N in:body" --state open`)
   znamená rozdělanou práci: nabídni navázání na její větev, worktree a poslední Stav. Zámek
   `in-progress` starší než 24 h nabídni vlastníkovi k uvolnění.
3. Slovní podmínky na řádku `Začíná po:` (stav trhu, nasazení) posuď; když si nejsi jistý,
   zeptej se. Ověření, které potřebuje živá data, při zavřeném trhu nespouštěj.
4. `gh issue edit N --add-label in-progress --remove-label ready` a vypiš název listu.

Hotovo, když kontrola prošla a list má `in-progress`.

## 2. Práce

Postupuj podle `.claude/templates/postup-listu.md`, kroky 1–6. Stavy `SPLIT`, `SUBSUMED`,
`BLOCKED`, `NEEDS_CONTEXT` a `NEEDS_DECISION` ukončí práci krokem 5 tohoto souboru.

## 3. Verifier

Až je práce commitnutá (necommitnuté změny verifier nevidí), spusť nástrojem Agent subagenta
typu `verifier`. Předej mu jen číslo listu, výchozí sha (`git merge-base origin/main HEAD`)
a příkaz `git diff origin/main...HEAD`, žádné shrnutí implementace. Nálezy oprav v jednom kole
jako další commity, pak spusť verifier znovu jen na cílenou kontrolu opravených nálezů.

Hotovo, když verifier nevrací blokující nález, nebo zbylý blokující nález míří do kroku 5 jako
`needs-decision`.

## 4. PR a Stav

1. `git push -u origin <větev>`.
2. Tělo PR podle `.claude/skills/implement/pr.md` do souboru, `test -s`, pak
   `gh pr create --draft --title "<typ>(<oblast>): <popis> (#N)" --body-file <soubor>`.
3. Komentář Stav pod list (`gh issue comment N --body-file <soubor>`, první řádek
   `[AI Agent] implement · <model> · <D. M.>`): stav, co je hotovo, výsledek verifieru po osách
   a tokeny z `python -I .claude/skills/retro/session-digest.py --tokens ${CLAUDE_SESSION_ID}`.
   Při červeném CI, eskalaci nebo opakovaném nálezu verifieru přidej řádek
   „kandidát na /retro: <důvod>“.

## 5. Merge, nebo zastavení

- **Zelené CI** aktuálního sha (`gh pr checks <PR>` vidí checky posledního commitu): `gh pr ready
  <PR>`, pak skript merge projektu (`merge_script` v `.claude/harness.json`) vždy ve verzi
  z `origin/main`: `git fetch origin main && git show origin/main:<merge_script> > "$TMP/mwg.sh"
  && bash "$TMP/mwg.sh" <PR>`. Guard zastaví PR se soubory harnessu; ten merguje vlastník.
- **Červené CI:** oprav příčinu dalším commitem. Po dvou neúspěších rozhoduje vlastník.
- **Zastavení:** `NEEDS_DECISION` a blokující nález (změna testů, golden dat nebo příkazů kontroly,
  o které kritéria nemluví; nesplněné kritérium) dostanou label `needs-decision` a otázku ve
  formátu `.claude/skills/grillme/SKILL.md`, sekce Formát otázky; PR zůstane draftem. `SPLIT`
  vrátí návrh rozdělení k `/spec`. Neblokující nález jde komentářem do PR.
- Po merge, zastavení nebo eskalaci sundej `in-progress`.

## 6. Konec

Shrň výsledek a zeptej se na nasazení s checklistem „Ověření naživo“ z kritérií mergnutých
a nenasazených listů: co ověří agent, co posoudí vlastník. U jednosměrné změny začíná checklist
zálohou a návratovým bodem podle AGENTS.md projektu. Sám nenasazuj.
