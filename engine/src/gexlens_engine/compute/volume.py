"""Přírůstek denního kumulativního volume kontraktu proti maximu seance (#1602).

IBKR posílá denní kumulativ, který v seanci klesat nemá, a přece kmitá
(7915 C 862 ↔ 863 každé 1–5 minut, 15. 10. 10,5 % kontrakt-minut RTH) a propadá
(pauza Globexu, noc 15. 10. Σ 29 744 → 5 792 → 29 912). Přírůstek proti POSLEDNÍ
hodnotě započetl návrat po každém poklesu znovu: Opt Vol RTH +12–70 %, Δ Flow
C/P +5–22 %. Reference přírůstku je proto maximum viděné od začátku seance:

- pokles i návrat k maximu dají 0, započte se jen objem nad maximem;
- NULL (tasty fallback objem nenese) referenci nemění;
- první výskyt kontraktu v seanci (start, restart) a minuta po mezeře jen
  založí stav: přírůstek se neměří, reference = max(reference, hodnota).
  Mezera = kontrakt v minulém cyklu objem nenesl (NULL, chybějící kotace,
  stará kotace); jinak by celý objem výpadku spadl do jedné minuty — ES
  2. 10. 19:18 UTC C 44 243 / P 49 343 (Rozhodnuto 21. 10. v #1385: B).
- časová díra (přeskočená minuta cyklu: zamrzlý proces, ztracená minuta) dá
  mezeru všem kontraktům a kontrakt v ní zůstane, dokud jeho objem poprvé
  nepřeroste první hodnotu po díře; ta změna jen založí stav (#1774,
  Rozhodnuto 12. 11.: A+). První sweep po probuzení nese objem z doby před
  spánkem (cache), čerstvý přijde až dalším sweepem ATM a křídel — ES 6. 11.
  15:12 Σ 47 981 a 15:14 Σ 14 467 místo stovek. Nová seance díru nepřenáší.

Totéž počítá API `volume_increments` a frontend `ensureDerived` (#1630, #1636;
Daily minutu po mezeře započte, časovou díru nezná). V enginu platí pod
mechanikou v6 (ADR-0057 bod 11, doplněk 22. 10.):
volá ho `CumDeltaTracker.add_volume` pod tisky (bez časové díry — rozklad
po spánku zneplatní verdikt minuty, ztracená minuta živého procesu nese tisky
i přírůstek přes týž interval), `SetupEngine._flows` a `TendencyEngine._flows`;
replay `scripts/backtest_setups.py option_flows` je jeho vektorové zrcadlo.
"""

import datetime as dt
from collections.abc import Hashable
from typing import NamedTuple

#: Krok štítků minutového cyklu; větší skok = časová díra (#1774)
CYCLE_STEP = dt.timedelta(minutes=1)


class VolumeStep(NamedTuple):
    """Krok `volume_increment`.

    `increment` None = přírůstek se neměřil (první výskyt, minuta po mezeře),
    v součtu toku 0; `declined` = hodnota pod referencí (pokles feedu).
    """

    increment: float | None
    reference: float
    declined: bool


def volume_increment(
    reference: float | None, cumulative: float, *, after_gap: bool = False
) -> VolumeStep:
    """Přírůstek kumulativního volume proti maximu seance (`reference`, None = bez stavu).

    Nová reference je vždy max(reference, cumulative); přírůstek nikdy záporný.
    """
    if reference is None:
        return VolumeStep(None, cumulative, False)
    declined = cumulative < reference
    if after_gap:
        return VolumeStep(None, max(reference, cumulative), declined)
    if declined:
        return VolumeStep(0.0, reference, True)
    return VolumeStep(cumulative - reference, cumulative, False)


class SessionVolumes:
    """Reference přírůstků per kontrakt v jedné seanci — stav kolem `volume_increment`.

    Konzument s cyklem (tracker, setupy, tendence) volá `add` pro kontrakty
    s objemem a `end_cycle` na konci cyklu: kontrakt se stavem, který v cyklu
    objem nenesl, má v dalším `add` mezeru. Restart začíná prázdný (první bar
    jen založí stav), nová seance stav maže — kumulativ IBKR začíná od ~0
    (Σ prvních hodnot u startu seance 0–558, ES a NQ 1.–20. 10.).

    Konzument s minutovými štítky (setupy, tendence) volá navíc `cycle` na
    začátku každé minuty, i bez barů a za zavřeného trhu: přeskočený štítek je
    časová díra (#1774, A+). Kontrakty, které nesou objem v prvním cyklu po
    ní, si zapamatují tu hodnotu jako kotvu a zůstanou v mezeře, dokud objem
    kotvu nepřeroste — první sweep po probuzení nese objem z cache, čerstvý
    přijde až dalším sweepem ATM a křídel. Kontrakt bez objemu v tom cyklu
    kotvu nemá a jde jako po mezeře. Restart štítky nezná (začíná prázdný).
    """

    def __init__(self) -> None:
        self._reference: dict[Hashable, float] = {}
        self._fed: set[Hashable] = set()
        self._after_gap: set[Hashable] = set()
        self._session: dt.date | None = None
        # Časová díra (#1774): kotva = první objem kontraktu po díře; `_armed`
        # = cyklus po díře ještě neskončil (kotvy se zakládají)
        self._anchors: dict[Hashable, float] = {}
        self._armed = False
        self._last_cycle: dt.datetime | None = None

    def cycle(self, ts_min: dt.datetime) -> bool:
        """Štítek minuty konzumenta; vrací True, když předchozí štítek chybí (časová díra).

        Skok zpět (srovnání hodin) díru nedává. Nová seance díru nepřenáší:
        `roll` stav i rozpracovanou díru smaže.
        """
        gap = self._last_cycle is not None and ts_min - self._last_cycle > CYCLE_STEP
        self._last_cycle = ts_min
        if gap:
            self._after_gap |= self._reference.keys()
            self._armed = True
        return gap

    def add(self, key: Hashable, cumulative: float) -> VolumeStep:
        """Kontrakt s objemem v tomto cyklu."""
        after_gap = key in self._after_gap
        if self._armed:
            self._anchors[key] = cumulative
            after_gap = True
        elif key in self._anchors:
            # Objem z cache před dírou kotvu nepřeroste; první čerstvý jen založí stav
            after_gap = True
            if cumulative > self._anchors[key]:
                del self._anchors[key]
        step = volume_increment(self._reference.get(key), cumulative, after_gap=after_gap)
        self._reference[key] = step.reference
        self._fed.add(key)
        self._after_gap.discard(key)
        return step

    def maximum(self, key: Hashable) -> float | None:
        """Maximum kumulativu kontraktu viděné v seanci (reference); None = bez objemu.

        Objem strany za seanci pro FA odhad pod v6 (ADR-0061 bod 1): pokles
        a návrat feedu ho nezvětší, restart ho převezme z prvního kumulativu.
        """
        return self._reference.get(key)

    def end_cycle(self) -> None:
        """Konec cyklu: kontrakty se stavem bez objemu v cyklu mají v dalším mezeru."""
        self._after_gap |= self._reference.keys() - self._fed
        self._fed.clear()
        self._armed = False

    def gap_all(self) -> None:
        """Hranice uvnitř seance (předání v rollu): maximum zůstává, první bar jen založí stav."""
        self._after_gap |= self._reference.keys()

    def roll(self, session: dt.date) -> None:
        """Obchodní den cyklu; nová seance stav smaže (kumulativ začíná znovu)."""
        if session != self._session:
            self.clear()
            self._session = session

    def clear(self) -> None:
        self._reference.clear()
        self._fed.clear()
        self._after_gap.clear()
        self._anchors.clear()
        self._armed = False
