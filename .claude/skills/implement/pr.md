# Šablona těla PR

Upraveno z `mattpocock/skills@5beb35b` (`skills/engineering/pr/SKILL.md`), licence MIT,
viz `.claude/THIRD_PARTY.md`.

Titulek PR je Conventional commit v jazyce projektu, bez značky `[AI Agent]`: při squash merge se
stane první řádkou commitu. Tělo čte vlastník místo diffu, proto ukazuje důkaz, ne souhrn.

```markdown
[AI Agent] implement · <model> · <D. M.>

Closes #<N>

## Problém
<1–3 věty: co nefungovalo nebo chybělo, z pohledu uživatele>

## Řešení
<nejmenší pohled, který vystihne změnu: pseudokód, strom volání nebo `diff` tvaru; krátký text
vedle něj>

## Testy a důkaz
- **Před:** <příkaz a řádek selhané aserce na výchozím commitu, nebo výstup či data před změnou>
- **Po:** <týž příkaz a výsledek po změně>
- Kritéria: <každé kritérium listu → test (`soubor::test`) nebo pozorování>

## UI
<jedno z: „Bez změny UI“ · „UI změněno, ověřeno: <co agent viděl>“ · „UI změněno, neověřeno,
ověří vlastník po nasazení“>

## Kontrola zadání
<výsledek verifieru po osách Zadání a Standardy a jak je každý nález vyřešený>

## Po nasazení
- Ověření naživo: <co ověří agent, co posoudí vlastník>
- Vratnost: <obousměrné | jednosměrné> — <co je nevratné a kdy: merge, nasazení, skript>;
  dosah: <jedno slovo>

## K rozhodnutí
<jen když list odbočil od zadání: otázky ve formátu `skills/grillme/SKILL.md`, sekce Formát otázky>
```

## Pravidla

- „pytest ✓“ důkazem není. Důkaz je test, který padal a teď prochází, nebo změněný výstup
  příkazu či dat. U opravy chyby a změny výpočtu je Před povinné.
- Jednosměrné dveře (one-way door) jsou změny, které se nedají vrátit revertem: smazaná nebo
  přepsaná data, migrace schématu, změněný formát uložených dat, odeslaná zpráva.
- Sekce bez obsahu vynech, kromě Testy a důkaz, UI a Vratnost.
