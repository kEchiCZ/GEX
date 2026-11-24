# Šablona listu (brief)

Upraveno z `mattpocock/skills@5beb35b` (`skills/engineering/triage/AGENT-BRIEF.md`), licence MIT,
viz `.claude/THIRD_PARTY.md`.

List je smlouva (contract) pro agenta, který ho implementuje v čerstvém kontextu: vidí jen tento
text, repo a soubory, na které list ukazuje. Jeden list = jedno chování, které jde samostatně
předvést, přes všechny vrstvy, kterých se týká (svislý řez, vertical slice). Názvy řádků čte
`check-brief.py`, piš je přesně.

```markdown
[AI Agent] spec · <model> · <D. M.>

Rodič: #<R>. Rozhodnutí a data jsou v rodiči.

Současné → požadované: <co systém dělá dnes> → <co bude dělat po listu, včetně okrajových případů
  a chybových stavů>
Kritéria přijetí:
- [ ] <chování, které na výchozím commitu selže a po listu projde: vstup → očekávaný výstup>
- [ ] (regrese) <co se nesmí změnit; jediný typ kritéria, které na výchozím commitu projde>
- [ ] <řádek Definition of done projektu, např. dokumentace>
Kontext (platí k <sha origin/main>):
- Rozhodnutí: <odkaz na Rozhodnuto v rodiči, Q-čísla>
- Rozhraní: <typy, signatury, tvary konfigurace, `soubor:řádek` k uvedenému commitu>
- Číst: <cesty od kořene repa, oddělené čárkou>
- Změnit: <cesty od kořene repa (krátká poznámka v závorce)>
- Nové: <cesty souborů, které vzniknou>
Šev: <šev testu (seam) ze seznamu Testovací švy v `.claude/verifier-project.md`>
Ověření: <pozorování, které na výchozím commitu selže a po dokončení projde: příkaz a výsledek>
Odhad: ~<N> ř.
Mimo rozsah: <co sem nepatří a proč>
Začíná po: <—, nebo #N, #M; v návrhu před založením L1, L2; podmínky mimo issue slovy na témže
  řádku, např. „po nasazení #N“, „při otevřeném trhu“>
```

## Pravidla

- **Kritérium umí selhat.** Každé kritérium chování musí na výchozím commitu selhat kvůli věrohodné
  chybě. Vadné je kritérium, které závisí na jiném listu, opakuje zadání nebo tvrdí, co vlastník
  neřekl (to označ „(nové, potvrď)“). Kritéria neměnnosti nesou „(regrese)“, řádky Definition of
  done stojí zvlášť.
- **Cesty platí k commitu.** List čeká dny až týdny. `Kontext (platí k <sha>)` říká, ke kterému
  stavu cesty a řádky patří. `check-brief.py` ověří Číst a Změnit na `origin/main` před startem.
- **Šev ze seznamu projektu.** Šev mimo seznam je otázka pro vlastníka, ne rozhodnutí listu.
- **Limity:** ≤ 8 zdrojových souborů ve Změnit a Nové (testy, golden data a dokumentace se
  nepočítají) a odhad ≤ 600 řádků. Větší práci rozděl podél chování, ne podle vrstvy. Prefactoring
  jde jako první list. Nová datová cesta přes víc vrstev začíná tenkým průstřelem (tracer bullet)
  přes všechny vrstvy, další listy ji rozšiřují.
- **Agent ho dokončí sám.** Krok člověka v listu není: volba jde do otázky `needs-decision`,
  kontrola po nasazení do checklistu „Ověření naživo“, ruční krok s termínem do připomínky.
- **Chování, ne postup.** Popisuj, co má systém dělat, a rozhraní, která se mění. Jak to napsat,
  rozhodne implementer po vlastním průzkumu.
