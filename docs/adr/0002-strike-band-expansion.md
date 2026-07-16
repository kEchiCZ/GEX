# ADR-0002: Politika auto-rozšíření pásma strikes

**Stav:** proposed (needs-decision) · **Datum:** 2026-07-16 · **Souvisí:** SPEC 3.2, issue #6

## Kontext

SPEC 3.2 požaduje pásmo strikes ±X bodů od spotu „s automatickým rozšířením, pokud se
spot přiblíží k okraji na < 25 % pásma", ale nedefinuje, JAK se pásmo rozšíří
(faktor růstu, recentrování). CLAUDE.md pravidlo 6: nerozhodovat mlčky.

## Rozhodnutí

Při splnění podmínky rozšíření (vzdálenost spotu k okraji < `strike_range_expand_threshold`
× šířka pásma) se pásmo:

1. **recentruje na aktuální spot** — trend pokračuje častěji, než se obrací, a centrované
   pásmo maximalizuje užitečnou šířku na obě strany;
2. **rozšíří o faktor 1.5** (`EXPANSION_GROWTH`) — jeden skok stačí, aby spot nebyl
   okamžitě znovu u okraje, a růst není tak agresivní, aby zbytečně žral market data lines.

Pásmo se nikdy samo nezužuje — zúžení je jen ruční změnou `GEXLENS_STRIKE_RANGE_POINTS`.

## Alternativy zvážené

- **Posun beze změny šířky** — levnější na subskripce, ale při trendovém dni vede
  k opakovaným posunům a ztrátě křídla, které trader sleduje.
- **Fixní přírůstek (+50 b)** — nezávislý na aktuální šířce, u širokých pásem prakticky
  bez efektu.

## Důsledky

- Po rozšíření vzroste počet kontraktů sweepu (~1.5×) → delší cyklus; lines limit
  (ADR-0001: ≥ 150) není ohrožen, sweep čas hlídá metrika `sweep_duration` (#7).
- Rozhodnutí lze revidovat bez dopadu na API — politika je izolovaná v
  `ChainDiscovery.maybe_expand` + konstantě `EXPANSION_GROWTH`.
