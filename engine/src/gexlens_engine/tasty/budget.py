"""Rozpočet DXLink subskripce (#982): 25 000 položek na spojení.

Server dxFeed počítá velikost subskripce v POLOŽKÁCH `symbol × typ eventu`,
ne v symbolech — změřeno 2. 9. 2026 sondou `tasty_probe.py sizecap`: strop
25 000 vyšel shodně pro samotné Quote i pro čtyři eventy produkce (tedy
6 250 symbolů při čtyřech eventech). Nad stropem server dávku odmítne
(`Your subscription size is too big`) a odmítnuté symboly tiše mlčí — přesně
tak ad-hoc pohled (#521 C) nikdy nedostal data: produkce jela na 24 944
položkách a 307 symbolů navíc přeteklo.

Dvě věci, které z toho plynou:

1. **Každý účel odebírá jen eventy, které čte.** Široký OI (#828) potřebuje
   Summary (a Quote, aby symbol „nemlčel" pro heal #936); extended expirace
   (#616) Quote + Greeks + Summary, printy z nich nikdo nečte (recorder #795 i
   stínové CumΔ #615 mapují jen aktivní řetěz). Tím se produkce vejde
   s rezervou, aniž by se zúžilo jediné pokrytí, které trader vidí.
2. **Ořez je deterministický a od nejméně důležitého.** Když se přes to vše
   plán nevejde, ubírá se zezadu seřazených seznamů: nejdřív extended od
   nejvzdálenější expirace a nejvzdálenějšího striku, pak wide od okraje
   pásma. Aktivní řetěz, podklad a ad-hoc se neořezávají — bez nich pohled
   nedává smysl. Dřív se řezalo abecedně (poslední dávky přetekly), což
   trefilo náhodné symboly.

Rezerva pro ad-hoc: běžný plán smí zabrat jen `max_entries − adhoc_reserve`,
aby vyhledaný symbol vždy měl kam přijít; nevyužitá rezerva se nevrací
extended — jinak by první ad-hoc pohled vyřadil kus extended pokrytí a heal
by je pak střídal. Z téže rezervy berou i krátké snímky řetězu QQQ pro plán opce
(`plan_chain`, #1740, #1768): ~520 položek na ~2 min v 9:15 ET a na pár sekund
v 9:31 ET se vejde vedle jednoho ad-hoc pohledu a extended kvůli nim nepřijde o nic.
Rezervu čerpá i odběr kontraktů plánu a podkladu QQQ od vzniku plánu do close
(`plan_quotes`, F1b #1747): 2 kontrakty a podklad × Quote + Greeks = 6 položek.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from gexlens_engine.tasty.symbols import ChainSymbols

#: Strop položek (symbol × event) na jedno DXLink spojení — měřeno 2. 9. 2026
MAX_ENTRIES = 25_000
#: Rezerva pro ad-hoc pohledy (~2 pohledy à 300 symbolů × 3 eventy) a snímek plánu (#1740)
ADHOC_RESERVE_ENTRIES = 2_000

ALL_EVENTS: tuple[str, ...] = ("Quote", "Greeks", "Summary", "TimeAndSale")

#: Eventy, které daný účel opravdu čte (viz docstring modulu)
EVENTS_BY_PURPOSE: dict[str, tuple[str, ...]] = {
    "underlying": ALL_EVENTS,  # cena + CVD podkladu (#829)
    "chain": ALL_EVENTS,  # validátor, OI fill, recorder printů, stínové CumΔ
    "adhoc": ("Quote", "Greeks", "Summary"),  # pohled bez flows (#521 C)
    "plan_chain": ("Quote", "Greeks", "Summary"),  # snímek řetězu QQQ pro plán (#1740)
    "plan_quotes": ("Quote", "Greeks"),  # kontrakty plánu a podklad QQQ do close (#1747)
    "extended": ("Quote", "Greeks", "Summary"),  # #616: snapshoty z mid + greeks
    "wide": ("Quote", "Summary"),  # #828: jen OI; Quote drží „nemlčí"
}

#: Pořadí přidělování; ořezává se jen `TRIMMABLE`, a to zezadu seznamu
KEEP_ORDER: tuple[str, ...] = (
    "underlying",
    "chain",
    "adhoc",
    "plan_chain",
    "plan_quotes",
    "wide",
    "extended",
)
TRIMMABLE: frozenset[str] = frozenset({"wide", "extended"})
#: Účely, které čerpají rezervu ad-hoc (snímek plánu #1740 a odběr plánu #1747 sdílí rezervu
#: s pohledy)
RESERVE_PURPOSES: frozenset[str] = frozenset({"adhoc", "plan_chain", "plan_quotes"})


@dataclass(frozen=True)
class BudgetPlan:
    """Výsledek rozpočtu: co subskribovat a co se nevešlo."""

    subscriptions: dict[str, frozenset[str]]
    entries: int
    #: Počet oříznutých symbolů per účel (jen ty, které měly přibýt a nevešly se)
    trimmed: dict[str, int] = field(default_factory=dict)
    #: Položky nad tvrdým stropem u neořezatelných účelů — chyba konfigurace
    over_hard_cap: int = 0


def plan_subscriptions(
    purposes: Mapping[str, Sequence[str]],
    *,
    max_entries: int = MAX_ENTRIES,
    adhoc_reserve: int = ADHOC_RESERVE_ENTRIES,
    events_by_purpose: Mapping[str, tuple[str, ...]] = EVENTS_BY_PURPOSE,
) -> BudgetPlan:
    """Rozdělí strop položek mezi účely; seznamy jsou seřazené od nejdůležitějšího.

    Symbol ve více účelech dostane sjednocení eventů a platí se jen za eventy,
    které ještě nemá. Neznámý účel dostává všechny eventy a neořezává se —
    lepší přeplatit než tiše ztratit data nového účelu.
    """
    subscriptions: dict[str, set[str]] = {}
    entries = 0
    trimmed: dict[str, int] = {}
    over_hard_cap = 0
    known = [name for name in KEEP_ORDER if name in purposes]
    extra = sorted(name for name in purposes if name not in KEEP_ORDER)
    # Rezerva chrání běžný plán před ad-hoc a naopak: běžné účely končí na
    # `max_entries − nevyužitá rezerva`, ad-hoc smí až na tvrdý strop
    regular_limit = max_entries - max(0, adhoc_reserve)
    reserve_used = 0
    for name in [*known, *extra]:
        events = events_by_purpose.get(name, ALL_EVENTS)
        limit = regular_limit if name in TRIMMABLE else max_entries
        purpose_cost = 0
        for symbol in purposes[name]:
            have = subscriptions.get(symbol, set())
            cost = len(set(events) - have)
            if cost == 0:
                continue
            if entries + cost > limit:
                if name in TRIMMABLE:
                    trimmed[name] = trimmed.get(name, 0) + 1
                    continue
                over_hard_cap += cost
            subscriptions.setdefault(symbol, set()).update(events)
            entries += cost
            purpose_cost += cost
        if name in RESERVE_PURPOSES:
            # Co ad-hoc a snímek plánu z rezervy skutečně vzaly, je už započtené
            # v `entries`; nevyužitý zbytek rezervy zůstává volný pro další pohled
            reserve_used += purpose_cost
            regular_limit = max_entries - max(0, adhoc_reserve - reserve_used)
    return BudgetPlan(
        subscriptions={symbol: frozenset(events) for symbol, events in subscriptions.items()},
        entries=entries,
        trimmed=trimmed,
        over_hard_cap=over_hard_cap,
    )


#: Skupina k seřazení: streamery jednoho produktu, jeho chain a spot (centrum)
DistanceGroup = tuple[Iterable[str], ChainSymbols | None, float | None]


def order_by_distance(groups: Sequence[DistanceGroup]) -> list[str]:
    """Seřadí streamery od nejdůležitějšího: nejbližší expirace, nejbližší strike.

    Ořez rozpočtu bere zezadu, takže vzadu musí být to, čeho je traderovi
    nejmíň líto — expirace za tři týdny a strike daleko od ceny. Vzdálenost
    je v % ceny, aby se ES a NQ řadily spravedlivě (lekce ADR-0004: NQ má
    cenu ~4× ES). Bez chainu nebo centra jde skupina za ty seřazené, abecedně
    (deterministické, ale hloupé — radši než náhodný ořez).
    """
    ranked: list[tuple[int, str, float, str]] = []
    for symbols, chain, center in groups:
        by_streamer = (
            {streamer: key for key, streamer in chain.by_contract.items()} if chain else {}
        )
        for streamer in symbols:
            key = by_streamer.get(streamer)
            if key is None or center is None or center <= 0:
                ranked.append((3, "", 0.0, streamer))  # bez informace — dozadu
                continue
            expiry, strike, _right = key
            ranked.append((2, expiry, abs(strike - center) / center, streamer))
    return [streamer for _rank, _expiry, _distance, streamer in sorted(ranked)]
