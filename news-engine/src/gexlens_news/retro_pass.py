"""Ranní retro pass (#284, SPEC 7.4).

Trader má ráno otevřít aplikaci s **kompletně zpracovanou nocí**, ne s frontou
nedodělků. Job proto v konfigurovatelném čase před EU open dožene všechno, co
přes noc zůstalo viset:

1. doklasifikuje headlines z asijské seance (včetně front z vyčerpaného
   denního limitu klasifikátoru),
2. dopočítá jejich reakce z nočních ES/NQ barů,
3. přepočítá SentIndex a topic indexy.

Nejde o nový výpočet, ale o **vynucené doběhnutí** stejných jobů, které jedou
i přes den — jen se nečeká na jejich periodu. Díky tomu je retro pass
bezpečně opakovatelný: všechny fáze jsou idempotentní.

Revize řady **nemění už vzniklé predikce ani signály** (S11): predikce nesou
verzi klasifikace a nový průchod jim ji nepřepisuje.
"""

import datetime as dt
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Kolik hodin zpět se považuje za „noc" pro report (jen popisná hodnota)
OVERNIGHT_HOURS = 12


@dataclass(frozen=True)
class RetroResult:
    """Co retro pass doháněl — podklad pro `Overnight: processed X events`."""

    ran_at: dt.datetime
    classified: int
    reactions: int
    index_points: int

    @property
    def processed(self) -> int:
        """Souhrn pro UI; nula znamená, že noc byla průběžně zpracovaná."""
        return self.classified + self.reactions

    def describe(self) -> str:
        return (
            f"Overnight: zpracováno {self.processed} položek "
            f"({self.classified} klasifikací, {self.reactions} reakčních oken)"
        )


def should_run(now: dt.datetime, run_at: dt.time, last_run: dt.date | None) -> bool:
    """Spustit dnes? Jednou za den, po nastaveném čase.

    Po restartu enginu se pass **dožene i se zpožděním** — kdyby se čekalo na
    přesnou minutu, výpadek by celý den vynechal.
    """
    if last_run == now.date():
        return False
    return now.timetz().replace(tzinfo=None) >= run_at


class RetroPass:
    """Orchestrace doběhnutí nočních front (SPEC 7.4)."""

    def __init__(
        self,
        classification_job: object,
        reaction_job: object,
        sentindex_job: object,
        *,
        run_at: dt.time = dt.time(5, 30),
    ) -> None:
        self._classification = classification_job
        self._reactions = reaction_job
        self._sentindex = sentindex_job
        self._run_at = run_at
        self._last_run: dt.date | None = None

    @property
    def last_run(self) -> dt.date | None:
        return self._last_run

    def due(self, now: dt.datetime) -> bool:
        return should_run(now, self._run_at, self._last_run)

    def run(self, now: dt.datetime) -> RetroResult:
        """Doběhne fáze v pořadí klasifikace → reakce → index.

        Pořadí je podstatné: bez kategorie by event nevstoupil do modelu a bez
        reakcí by se nedaly vyhodnotit predikce. Selhání jedné fáze nezastaví
        ostatní — retro pass má dohnat, co jde, ne spadnout na první chybě.
        """
        classified = self._safe("klasifikace", lambda: self._classification.run(now))  # type: ignore[attr-defined]
        reactions = self._safe("reakce", lambda: self._reactions.run(now))  # type: ignore[attr-defined]
        points = self._safe("SentIndex", lambda: self._sentindex.run(now)[0])  # type: ignore[attr-defined]

        self._last_run = now.date()
        result = RetroResult(
            ran_at=now, classified=classified, reactions=reactions, index_points=points
        )
        logger.info("Ranní retro pass — %s", result.describe())
        return result

    @staticmethod
    def _safe(label: str, call: object) -> int:
        try:
            return int(call())  # type: ignore[operator]
        except Exception:
            logger.exception("Retro pass: fáze %s selhala — pokračuji dál", label)
            return 0
