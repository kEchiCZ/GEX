# Šablona: sub-issue (jedna session, jeden PR)

Navazuje na zavedený styl (#1352). Titulek jako Conventional commit česky:
`feat(scope): …`, `fix(scope): …`, `docs(adr): …`. Labely: právě jeden `prio:*` a jeden `epic:*`,
případně `needs-decision`, `performance`, doménový. Navázat jako GitHub sub-issue epiky
(příkaz v SKILL.md, krok 3) — dřív se vazba psala jen textem, teď obojí.

```markdown
Fáze N z #<EPIKA>. Koncept, data a rozhodnutí jsou v #<EPIKA> (fáze N−1 = PR #… / —).
Postup: skill epika (`.claude/skills/epika/SKILL.md`, kroky 4–5). Blokováno: #… (nebo —)

## Rozsah
- **<co>** — <konkrétně: modul, funkce, endpoint, obrazovka; path:line, kde je známé>

### Mimo rozsah
- <co sem nepatří → vlastní issue>

## Akceptační kritéria
- [ ] <ověřitelné testem nebo na živých datech: identifikátory, čísla, meze>
- [ ] <u hlídačů a dnů: test na víkend, denní pauzu, svátek, nedělní otevření; obchodní, ne kalendářní den>
- [ ] Testy (unit / golden s odůvodněním / vitest / Playwright), ruff, mypy, pytest, `npm run build`
- [ ] Dokumentace (ADR / manuál #628 / SPEC kap. 11 / roadmapa #629) + u engine/API nasazení na dev a ověření naživo

## Velikost a navázání
- Odhad: vrstva <engine | API | frontend> · soubory ke čtení <…> · živé ověření <ano/ne> → 1 session.
- Na začátku session komentář „Plán session", po každém kroku commit + push, na konci „Stav".

Souvisí: #…, ADR-00xx
```

# Šablona: PR

Titulek `typ(scope): popis česky — upřesnění (#N)`, kde N je sub-issue. Tělo:

```markdown
Closes #N

## Problém / Shrnutí
## Řešení / Co se mění
**Engine** … **API** … **Frontend** … **Dokumentace** …

## Testy a kontroly
- nové testy po souborech; pytest (… ✓), vitest (… ✓), ruff/mypy/eslint, `npm run build`

## Kontrola zadání
- nálezy nezávislého ověřovatele (skill epika, krok 5) a jak jsou vyřešené

## Po nasazení
- co ověřit naživo

## K rozhodnutí (odchylky od zadání, prosím o potvrzení)   ← jen když nějaké jsou
```
