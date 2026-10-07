# Šablona: tělo epiky

Navazuje na zavedený styl (#1187, #1323). Titulek ve stylu commitu česky
(`feat(scope): Název — co konkrétně`), nebo `Epic: …` u epiky přes více oblastí.
Labely: `enhancement`, právě jeden `epic:*`, `prio:*`, případně doménový (`setup`) a `needs-decision`.

Revize zadání: nahoru blockquote `> Revize D. M.: co se změnilo a proč`, původní text sbalit do
`<details>`, nepřepisovat ho.

```markdown
## Zadání (uživatel D. M. RRRR)
> <citace nebo věrné shrnutí slov vlastníka; odkazy na screenshoty nechat v issue>

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
- <dotčená pravidla: R1–R6, zavřený trh, obchodní den, data se nemažou, 100 lines, point-in-time>

## Fázovaný plán
Každá fáze se vejde do jedné session (jinak ji rozděl); pořadí = závislosti a hodnota pro obchodování.

**Fáze 0 — rozhodnutí: ADR-00xx … (samostatné issue, prio:Px, needs-decision):** …
**Fáze 1 — … (samostatné issue #…, prio:Px, 1 session):** …
**Mimo plán:** …

## Akceptační kritéria
- [ ] <ověřitelné: identifikátory, čísla, meze, chování při zavřeném trhu>
- [ ] Testy (unit / golden s odůvodněním / vitest / Playwright), ruff, mypy, pytest, `npm run build`
- [ ] Dokumentace (ADR, manuál #628, SPEC kap. 11 u nového modulu, roadmapa #629) + nasazení na dev a ověření na živé seanci

## Otevřené otázky k rozhodnutí
1. **<téma>**
   - A) … Výhoda: … Nevýhoda: …
   - B) … Výhoda: … Nevýhoda: …
   - **Doporučení: B** — <zdůvodnění: rychlost · výkon · relevance dat pro obchodování; co otázka blokuje>

## Rozhodnuto
- D. M. — <téma>: <volba> (vlastník, komentář / ADR-00xx)

## Závislosti
- Blokováno: #… · Navazuje: #…

Souvisí: #…, ADR-00xx
```
