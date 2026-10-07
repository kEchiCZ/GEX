# Šablona: tělo epiky

Navazuje na zavedený styl (#1187, #1323). Titulek ve stylu commitu česky
(`feat(scope): Název — co konkrétně`), nebo `Epic: …` u epiky přes více oblastí.
Labely: `enhancement`, ≥ 1 `epic:*` (oblast), právě jeden `prio:*`, případně doménový (`setup`)
a `needs-decision`, dokud jsou otevřené otázky.

Revize: nahoru krátká poznámka `> Revize D. M.: co se změnilo a proč`; předchozí znění do komentáře,
tělo drží jen aktuální stav (≤ ~25 000 znaků).

```markdown
## Zadání (uživatel D. M. RRRR)
> <věrné shrnutí slov vlastníka; jméno referenční aplikace nahradit „referenční aplikace" (#482),
> cizí screenshoty nepřenášet — popsat slovy, co ukazují>

## Kontext a tvrdá data k D. M.
- <co UŽ je v repu — modul path:line, endpoint, #issue, ADR-00xx>
- <co chybí nebo je špatně — měření, tabulka čísel z produkce>

## Návrh
<koncept: vstupy → výstupy → stavové přechody; tabulky stavů, tok dat; mockup jen u UI.
Jak to vlastník použije při obchodování: kdy, na jaké obrazovce, jaké rozhodnutí tím udělá.>

### Záměrně mimo rozsah (YAGNI)
- <co ne a proč>

## Rizika a tvrdé podmínky
- **<riziko>** → <protiopatření>
- **Rozpočet:** latence UI ≤ … ms · minutový cyklus +… ms · RAM +… MB · nové market data lines: 0
- <dotčená pravidla: R1–R6, zavřený trh, obchodní den, data se nemažou, 100 lines, point-in-time>

## Fázovaný plán
Každá fáze se vejde do jedné session (jinak ji rozděl); pořadí = závislosti a hodnota pro obchodování.

**Fáze 0 — rozhodnutí: ADR-00xx … (samostatné issue, prio:Px, needs-decision):** …
**Fáze 1 — … (sub-issue #…, prio:Px, 1 session):** …
**Mimo plán:** …

## Akceptační kritéria
- [ ] <ověřitelné: identifikátory, čísla, meze, chování při zavřeném trhu>
- [ ] Data: bez look-ahead (test bodu v minulosti), stáří viditelné, výpadek = viditelná mezera
- [ ] Testy (unit / golden s odůvodněním / vitest / Playwright), ruff, mypy, pytest, `npm run build`
- [ ] Dokumentace (ADR, manuál #628, SPEC kap. 11 u nového modulu, roadmapa #629) + u engine/API nasazení na dev a ověření naživo

## Otevřené otázky k rozhodnutí
1. **<téma>**
   - A) … Výhoda: … Nevýhoda: …
   - B) … Výhoda: … Nevýhoda: …
   - **Doporučení: B** — <zdůvodnění: rychlost · výkon · relevance dat pro obchodování; co otázka blokuje>

## Rozhodnuto
- D. M. — <téma>: <volba> (vlastník, komentář / ADR-00xx)
- D. M. — schváleno vlastníkem (komentář „Schvaluji")

## Závislosti
- Blokováno: #… · Navazuje: #…

Souvisí: #…, ADR-00xx
```
