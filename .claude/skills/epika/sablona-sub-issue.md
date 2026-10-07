# Šablona: sub-issue (jedna session, jeden PR)

Navazuje na zavedený styl (#1352). Titulek jako Conventional commit česky:
`feat(scope): …`, `fix(scope): …`, `docs(adr): …`. Labely: `prio:*`, právě jeden `epic:*`,
případně `needs-decision`, `performance`, doménový. Navázat jako GitHub sub-issue epiky
(dřív se vazba psala jen textem — teď obojí).

```markdown
Fáze N z #<EPIKA>. Koncept, data a rozhodnutí jsou v #<EPIKA> (fáze N−1 = PR #… / —).
Blokováno: #… (nebo —)

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
- 1 session. Když se nedokončí: commit + push do větve `feat/<N>-slug` a komentář „Stav"
  (hotovo / zbývá / další krok / otevřené otázky / jak ověřit).

Souvisí: #…, ADR-00xx
```

# Šablona: PR

Titulek `typ(scope): popis česky — upřesnění (#N)`. Tělo:

```markdown
Closes #N            (u částečné fáze: Refs #N na konci)

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
