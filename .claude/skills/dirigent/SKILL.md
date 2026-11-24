---
name: dirigent
description: "Vede listy rodiče #R po jednom: implementer, verifier, PR, CI, merge; otázky položí najednou na konci."
argument-hint: "#R"
disable-model-invocation: true
model: opus
effort: high
---

Upraveno z `obra/superpowers@22f0b46` (`skills/subagent-driven-development/SKILL.md`)
a `mattpocock/skills@5beb35b` (`docs/engineering/implement-spec.md`), licence MIT,
viz `.claude/THIRD_PARTY.md`.

Vedeš listy (sub-issues) rodiče #R, vždy jeden naráz: jeden agent s testy, žádný souběh ani
integrační větev. Kód nepíšeš, píše ho agent `implementer`; posuzuje agent `verifier`. Stav je
v issues a PR, ne v kontextu: nový `/dirigent #R` naváže podle labelů a komentářů Stav.
Nikdy nenasazuješ. Projektové hodnoty čteš z `.claude/harness.json`.

## 1. Výběr listu

1. `git fetch origin`. Listy rodiče: `gh api repos/{owner}/{repo}/issues/R/sub_issues --jq
   '.[] | "\(.number) \(.state) \(.title)"'`.
2. Další list je otevřený, s labelem `ready`, bez `in-progress` a bez otevřeného PR, v pořadí
   L1, L2… z titulku. Přednost má zastavený list, pod kterým je komentář vlastníka bez
   `[AI Agent]` novější než zápis zastavení (krok 6).
3. `python -I .claude/skills/spec/check-brief.py --mode implement N` (ověří i blokátory
   z `Začíná po:` zavřené mergnutým PR). Neprošlý list přeskoč a důvod zapiš komentářem pod list.
4. `gh issue edit N --add-label in-progress --remove-label ready`.

Hotovo, když máš jeden list, který prošel kontrolou, nebo víš, že žádný spustitelný nezbývá
(pak krok 7).

## 2. Implementer

1. Worktree vedle hlavního checkoutu: `git worktree add <cesta> -b feat/N-<slug> origin/main`.
   Zapiš si výchozí sha (`git rev-parse origin/main`).
2. Spusť nástrojem Agent subagenta typu `implementer`. Prompt obsahuje jen: číslo listu,
   absolutní cestu worktree, větev a jednu větu, kam list patří. Historii ostatních listů ani
   shrnutí rodiče nepřidávej. List, jehož Změnit nebo Nové leží v `core_paths`, spusť
   s parametrem `model: opus`.
3. Zprávu implementera neber jako důkaz: ověř `git log origin/main..HEAD --oneline` ve worktree.

Podle stavu na prvním řádku zprávy:
- `DONE`, `DONE_WITH_CONCERNS` → krok 3.
- `NEEDS_DECISION`, `BLOCKED`, `NEEDS_CONTEXT`, `SPLIT` → krok 6.
- `SUBSUMED` → verifier potvrdí nárok proti `main`; potvrzený list zavři komentářem s důkazy.

## 3. Verifier a kolo oprav

1. Spusť subagenta typu `verifier`: číslo listu, výchozí sha, cesta worktree a příkaz
   `git diff origin/main...HEAD`. Nálezy nepředjímej a nic mu nezakazuj hlásit.
2. Nálezy ulož do souboru ve scratchpadu a pusť **nového** implementera (čerstvý kontext) do
   téhož worktree se souborem nálezů. Jedno kolo.
3. Pak verifier jen na cílenou kontrolu opravených nálezů. Zbylý blokující nález → krok 6.

## 4. PR, CI a merge

1. Tělo PR = tělo ze zprávy implementera doplněné o výsledek verifieru; první řádek
   `[AI Agent] dirigent · <model> · <D. M.>`. `test -s`, pak `gh pr create --draft --head
   feat/N-<slug> --title "<typ>(<oblast>): <popis> (#N)" --body-file <soubor>`.
2. Komentář Stav pod list: stav, výsledek verifieru po osách, kola oprav a tokeny subagentů
   tohoto listu (řádky `subagent agent-<agentId>` z
   `python -I .claude/skills/retro/session-digest.py --tokens ${CLAUDE_SESSION_ID}`). Při
   červeném CI, eskalaci nebo opakovaném nálezu verifieru řádek „kandidát na /retro: <důvod>“.
3. Počkej na dokončení CI aktuálního sha (`gh pr checks <PR>`; `--watch` vrací 0 i při failu).
   - Červené: nový implementer s výpisem selhání do téhož worktree, pak znovu CI. Po dvou
     neúspěších téhož listu krok 6.
   - Zelené: `gh pr ready <PR>`, `git worktree remove <cesta>`, pak skript merge projektu ve
     verzi z `origin/main`: `git fetch origin main && git show origin/main:<merge_script> >
     "$TMP/mwg.sh" && bash "$TMP/mwg.sh" <PR>`. Skript smaže i větev listu. Když merge zastaví
     guard nebo skript (soubory harnessu, nevratná změna), list zapiš jako zastavený: merguje
     vlastník.
4. Sundej `in-progress` a pokračuj krokem 1.

## 5. Předávka

Při ~120k tokenech v kontextu zapiš komentář Stav pod rodiče #R: stav každého listu, rozdělaná
práce (větev, worktree, PR) a otevřené otázky. Pak skonči větou „Pokračuj `/dirigent #R`
v nové session.“

## 6. Zastavený list

Důvody: `NEEDS_DECISION`, blokující nález verifieru, nevratná změna nebo PR se soubory harnessu,
`SPLIT` nebo `NEEDS_CONTEXT` (návrat k `/spec`), `BLOCKED`, dva neúspěchy téhož listu.

1. Komentář Stav pod list s důvodem a otázkou ve formátu `.claude/skills/grillme/SKILL.md`,
   sekce Formát otázky. U otázky label `needs-decision`. Worktree a větev nech do navázání.
2. Pokračuj listy, které na zastaveném nezávisí. Otázku vlastníkovi teď nepokládej.

## 7. Konec

Když není co spustit:

1. Souhrn: každý list, stav, PR, merge ano/ne, kola oprav, červené CI.
2. Všechny otázky zastavených listů najednou, ve formátu otázky.
3. Jeden dotaz na nasazení s checklistem „Ověření naživo“ z kritérií mergnutých a nenasazených
   listů: co ověří agent, co posoudí vlastník.

Po odpovědích vlastníka naváž zastavenými listy: nový implementer v jejich worktree a větvi
s odpovědí vlastníka, pak krok 3.
