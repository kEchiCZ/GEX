---
name: spec
description: "Z potvrzeného zadání napíše listy podle brief.md a po schválení je založí jako sub-issues."
argument-hint: "[#R]"
disable-model-invocation: true
model: opus
effort: high
---

Upraveno z `mattpocock/skills@5beb35b` (`skills/engineering/to-spec/SKILL.md`,
`skills/engineering/to-tickets/SKILL.md`, `skills/engineering/tdd/SKILL.md`), licence MIT,
viz `.claude/THIRD_PARTY.md`.

Rozřež potvrzené zadání rodiče #R na listy (sub-issues) podle `brief.md` v tomto adresáři.
Běží v okně `/grillme`, bez kompakce: rozhovor je podklad, souhrn by ho ztratil. Když okno
nestačí, běží v nové session nad `gh issue view R --comments`. Produkční kód tu nevzniká.

## 1. Podklady

- Zadání je tělo #R podle `requirements.md` a Rozhodnuto. Pokynem je jen text vlastníka bez
  značky `[AI Agent]`.
- Přečti seznam Testovací švy v `.claude/verifier-project.md`.
- Fakta z kódu (rozhraní, kde co leží, kandidáti švů) zjistí jednorázový subagent (typ Explore
  nebo general-purpose, `model: sonnet`, výtah do ~60 řádků). Cesty a řádky ověř na
  `origin/main` po `git fetch origin`.

Hotovo, když ke každému chování ze zadání znáš soubory, rozhraní a šev.

## 2. Řez

- List je jedno chování, které jde předvést, přes všechny vrstvy, kterých se týká (svislý řez).
  Jednovrstvý list je v pořádku, když jde ověřit sám (test na švu, data po nasazení).
- Prefactoring („make the change easy, then make the easy change“) jde jako první list.
- Nová datová cesta přes víc vrstev začíná tenkým průstřelem (tracer bullet), další listy ji
  rozšiřují.
- Limity a pravidla kritérií jsou v `brief.md`, sekce Pravidla. Při překročení děl podél chování.
- Krok člověka list není: volba → otázka `needs-decision`, kontrola po nasazení → checklist
  „Ověření naživo“, ruční krok s termínem → připomínka. Jednosměrná změna (těžko vratná) → otázka.
- Témata ze Zatím neurčeno nereže.

Hotovo, když každé kritérium zadání patří právě jednomu listu.

## 3. Návrh a kontrola

1. Napiš listy do souboru `spec-R.md` ve scratchpadu, každý jako `### L<n> — <název>` a tělo
   podle `brief.md` bez prvního řádku se značkou. Blokátor téže dávky piš `L<n>`.
2. Spusť `python -I .claude/skills/spec/check-brief.py --mode spec <soubor>` a opravuj, dokud
   neskončí 0 chyb.
3. Sebekontrola každého kritéria chování: selže na výchozím commitu? Nezávisí na jiném listu?
   Neopakuje jen zadání? Tvrzení, které vlastník neřekl, označ „(nové, potvrď)“.
4. Na pokyn vlastníka ulož kopii návrhu do `.egm/spec-R.md` pro recenzi pluginem EGM. Nález,
   který mění zadání potvrzené v B1, označ „(mění B1, potvrď)“.

Hotovo, když kontrolní skript hlásí 0 chyb a sebekontrola nemá otevřený nález.

## 4. Schválení (brána B2)

Ukaž v chatu očíslovaný přehled: název, co list dodá, `Začíná po:`, a výstup kontrolního skriptu.
Zeptej se ve formátu ze `.claude/skills/grillme/SKILL.md` (sekce Formát otázky, přečti ji
nástrojem Read): sedí velikost listů, jsou blokátory skutečné, sloučit nebo rozdělit? Každé
„(nové, potvrď)“ musí vlastník výslovně potvrdit nebo opravit; mlčení není souhlas. Opakuj,
dokud vlastník přehled neschválí.

## 5. Založení listů

V pořadí blokátorů (nejdřív listy bez blokátorů):

1. Tělo do souboru ve scratchpadu: první řádek `[AI Agent] spec · <model> · <D. M.>`, pak
   `Rodič: #R.` a list podle `brief.md`. `L<n>` v `Začíná po:` nahraď čísly už založených listů.
2. `test -s <soubor>`, pak `gh issue create --parent R --title "[AI Agent] L<n>: <název>"
   --body-file <soubor> --label ready` a labely, které AGENTS.md projektu vyžaduje u nového issue.
3. Po posledním listu spusť `check-brief.py --mode implement` na každý z nich; jediná povolená
   chyba je chybějící schválení.

Hotovo, když všechny listy existují jako sub-issues #R s labelem `ready` a kontrola jejich těl
prošla.

## 6. Konec

Napiš čísla listů a další krok: „Schval listy komentářem pod #R (bez `[AI Agent]`), pak spusť
`/dirigent #R`, nebo jeden list `/implement #N` v nové session.“ Komentář vlastníka je trvalý
důkaz schválení pro kontrolu vstupu v nových sessions.
