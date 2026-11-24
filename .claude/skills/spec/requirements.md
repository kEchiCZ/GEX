# Šablona vyjasněného zadání (requirements)

Výstup `/grillme` v těle issue. Z něj `/spec` řeže listy podle `brief.md`. Do této formy
`/grillme` přepíše i starší issue po kontrole připravenosti. Sekce, které by zůstaly prázdné,
vynech, kromě Zadání, Kritérií přijetí a Rozhodnuto.

```markdown
[AI Agent] grillme · <model> · <D. M.>

## Zadání (vlastník <D. M.>)
> <slova vlastníka, věrně; cizí obrázky a zdroje inspirace popsané slovy, bez jmen a odkazů>

## Kontext a tvrdá data k <D. M.>
- <co v repu už je: `soubor:řádek`, endpoint, issue, ADR>
- <co chybí nebo je špatně: měření, čísla, výpis; odkud pochází>

## Návrh
<vstupy → výstupy → stavové přechody. Jak to vlastník použije: kdy, kde a jaké rozhodnutí tím udělá.>

### Mimo rozsah
- <co ne a proč>

## Rizika
- **<riziko>** → <protiopatření>

## Kritéria přijetí
Chování (každé umí selhat na výchozím commitu):
- [ ] <vstup → očekávaný výstup, meze, okrajové případy>
Neměnnost:
- [ ] (regrese) <co zůstává>
Definition of done:
- [ ] <testy, dokumentace a ověření podle AGENTS.md projektu>

## Otevřené otázky
<jen otázky, které už jde přesně položit; formát `skills/grillme/SKILL.md`, sekce Formát otázky>

## Zatím neurčeno
- <téma> — <proč zatím nejde přesně položit; co ho odemkne>

## Fáze a brány
<jen u práce na víc fází, jen nejbližší fáze: co dodá, „Začíná po:“>

## Rozhodnuto
- <D. M.> — Q<n> <téma>: <volba> (vlastník | doporučení Clauda, přijato)
```

## Pravidla

- **Zatím neurčeno** (fog of war, mlha) je pro téma, u kterého zatím nejde položit přesnou otázku.
  Test: „umím otázku teď přesně položit?“ Ano = Otevřené otázky. Ne = zůstává tady a `/spec`
  na ni listy neřeže.
- **Rozhodnuto** nese každou odpověď vlastníka. Odpověď „ano“ i „nech na tobě“ se zapíše jako
  „doporučení Clauda, přijato“.
- **Tvrdá data** mají zdroj a datum. Odhad je označený jako odhad.
