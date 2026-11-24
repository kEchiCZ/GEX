# Psaní pro agenty — měřítko

Upraveno z `mattpocock/skills@5beb35b`, licence MIT, viz `.claude/THIRD_PARTY.md`.

Měřítko pro každý soubor, který čte agent: skill, agent, šablonu, hook se zprávou, AGENTS.md a CLAUDE.md.
Spojuje tři zdroje: [`writing-for-agents`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/productivity/writing-for-agents/SKILL.md)
a [`SKILL-MECHANICS.md`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/productivity/writing-for-agents/SKILL-MECHANICS.md)
(celý originál), [Anthropic best practices](https://code.claude.com/docs/en/best-practices) (stav 24. 11. 2026)
a studii [arXiv 2602.11988](https://arxiv.org/abs/2602.11988). Soubor se do kontextu nenačítá, čte ho autor
souborů harnessu a `/retro`.

## 1. Kontrola má přednost před větou
- Pravidlo, které nesmí mít výjimku, patří do hooku, `permissions.deny` nebo testu. Věta v CLAUDE.md
  je pokyn, který model smí vyhodnotit jako nepoužitelný: „If something has to happen every time, make it
  a hook, a deny rule, or a managed setting. `CLAUDE.md` is for guidelines“ (Anthropic).
- Opakovanou chybu oprav kontrolou. Věta zůstává jen tam, kde je potřeba úsudek.
- Hook, který hlídá jen preferenci, smaž a nech rozhodnout model.

## 2. Každá věta mění chování (no-op test)
- Po celých větách: změní věta chování proti tomu, co model dělá sám? Vadilo by její smazání? Když ne,
  smaž celou větu, nezkracuj v ní slova.
- Test závisí na modelu, ne na čtenáři. Spor o no-op rozhodne běh souboru před změnou a po ní, ne debata.
- Slabé vedoucí slovo (_důkladně_ u agenta, který důkladný je) je také no-op. Oprava je silnější slovo.

## 3. Vždy načtený text stojí v každém tahu (context load)
- CLAUDE.md, AGENTS.md a popis model-invoked skillu platí tokeny a pozornost v každém tahu, i když se
  nepoužijí. Kontextové soubory podle arXiv 2602.11988 úspěšnost obecně nezvyšují a náklady zvyšují
  v průměru o víc než 20 %. Pokyny agent plní, přehledy repozitáře mu nepomáhají.
- Prostředí je zdroj pravdy a dokument, který ho opakuje, je jen kopie (cache). Co najde jeden příkaz
  nebo soubor (`--help`, `pyproject.toml`, adresáře), nepiš. Zapiš to, co se vyhledat nedá: nepsanou
  konvenci, důvod rozhodnutí, past.
- Každý význam má jedno místo (single source of truth). Duplicita stojí údržbu a tokeny a nafukuje váhu
  pravidla. Bez prořezávání se usazují zastaralé vrstvy (sediment).

## 4. Ukazatel rozhoduje, kdy agent materiál otevře (context pointer)
- Ukazatel je řádek v kontextu, který jmenuje materiál mimo kontext a podmínku, kdy ho číst: popis
  skillu, řádek v AGENTS.md. Spolehlivost určuje jeho znění, ne cíl. Slabě napsaný ukazatel nejdřív zostři,
  obsah vkládej do souboru, až když to nestačí.
- Vedoucí slovo na začátku, jeden spouštěč na každou větev (branch), nic z toho, co nese tělo.
- Do souboru patří, co potřebuje každá větev. Co potřebují jen některé, jde za ukazatel (progressive
  disclosure). Definice, pravidla a výjimky jednoho pojmu stojí pod jedním nadpisem (co-location).
- User-invoked skill (`disable-model-invocation: true`) nestojí kontext, indexem je člověk. Popis má
  jednu větu pro člověka, bez seznamu spouštěčů. Referenci, kterou sdílí dva user-invoked skilly, dej
  do obyčejného souboru mimo oba.

## 5. Každý krok končí kritériem dokončení (completion criterion)
- Jasnost: pozná agent hotovo od nehotova? Vágní mez („pochopení dosaženo“) zve k předčasnému dokončení
  (premature completion). Nejdřív mez zostři. Postup rozděl až při pozorovaném spěchu, a jen přes
  skutečnou hranici kontextu (subagent, předávka).
- Náročnost (demand): „každý upravený soubor má test“ vynutí víc práce než „vypiš změny“. Nejlepší
  kritérium jde ověřit a pokrývá všechno.

## 6. Piš cíl, zákaz jen jako pojistku (negation)
- Zákaz vtáhne zakázané chování do kontextu a zpřístupní ho. Napiš chování, které chceš („komentář na
  jeden řádek“).
- Zákaz patří jen tam, kde tvrdou pojistku nejde napsat pozitivně, a vždy spárovaný s cílem.

## 7. Vedoucí slova (leading words)
- Pojem z předtrénování (_tracer bullet_, _frontier_, _seam_, test, který jde _do červené_) opakovaný
  jako slovo ukotví celou oblast chování za pár tokenů. Nejdřív existující slovo, vlastní jen s definicí.
- Anglické vedoucí slovo zůstává, při prvním výskytu má český výklad. Trojice a opisy jednoho nápadu
  nahraď jedním slovem.

## 8. Délka (sprawl)
- Dlouhý soubor ředí pozornost, i když je každý řádek živý. Lék je reference za ukazatelem a rozdělení
  podle větve nebo pořadí kroků.

## Před PR
1. Každá věta prošla no-op testem.
2. Pravidlo bez výjimky má kontrolu, nebo PR říká, proč zatím ne.
3. Každý krok má kritérium dokončení.
4. Každý zákaz má vedle sebe cíl.
5. Nic z toho, co najde jeden příkaz.
6. Přírůstek vždy načteného textu (popis skillu, řádek CLAUDE.md) je v PR uvedený.
