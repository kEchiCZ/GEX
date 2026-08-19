"""Aktivní IBKR sonda (#517 fáze B) — rozliší výpadek farmy od mrtvých subskripcí.

Fáze A (pasivní křížová kontrola, `tasty/crosscheck.py`) umí říct, že problém
je na straně IBKR — ale ne KDE: mrtvá datová farma a potichu umřelé subskripce
vypadají zvenku stejně, jenže akce se liší. Mrtvé subskripce spraví cílená
resubskripce hned; při výpadku farmy by resubskripční bouře nepomohla a jediná
smysluplná akce je čekat a vědět o tom.

Sonda se proto spouští **až na signál fáze A** (verdikt `ibkr_suspect`), ne
periodicky naslepo: jednorázový snapshot referenčního likvidního kontraktu
(front future podkladu) MIMO běžné subskripce.

* snapshot dodá data → farma žije → mrtvé jsou subskripce → jedna cílená
  obnova přes `ConnectionManager.resubscribe_now()` (týž řetěz callbacků jako
  po reconnectu — žádná bouře per kontrakt, žádný zbytečný reconnect),
* snapshot nedodá nic (timeout, chyba spojení) → výpadek farmy → jen hlášení;
  resubskripce se vědomě NESPOUŠTÍ.

Rozpočet: jeden snapshot = jedna market data line na pár sekund. Strop účtu je
tvrdých 100 (ADR-0001) a sweep špičkově bere ~84 — sonda se přesto pouští jen
s volnou rezervou (`lines_headroom`), aby nikdy nebyla tou poslední kapkou.
PacingGuard se netýká: hlídá historical requesty, snapshot mezi ně nepatří.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

#: Jak dlouho se čeká na data snapshotu, než se farma prohlásí za mrtvou.
#: Sweep dávky dostávají odpovědi do jednotek sekund; 10 s je násobná rezerva.
DEFAULT_TIMEOUT_S = 10.0

#: Nejkratší rozestup dvou běhů sondy. Trigger je alert fáze A, který má
#: vlastní cooldown 15 min — tohle je pojistka, kdyby se spouštěčů přidalo.
DEFAULT_MIN_INTERVAL_S = 600.0

#: Kolik market data lines musí zbývat volných, aby se sonda vůbec pustila.
DEFAULT_LINES_HEADROOM = 2

ProbeOutcome = Literal["subscriptions_dead", "farm_dead", "skipped"]


@dataclass(frozen=True)
class ProbeReport:
    """Výsledek jednoho běhu sondy; `message` je určená do alertu a logu."""

    outcome: ProbeOutcome
    message: str


class FarmProbe:
    """Jednorázová sonda se single-flight ochranou a vlastním cooldownem.

    Závislosti se vstřikují, aby šla logika testovat nad mockem (pravidlo
    práce 4 — na live API se v testech nesahá):

    * `snapshot_probe` — vrátí True, když referenční kontrakt dodal data;
      výjimka se počítá jako mrtvá farma (spojení k datům nevede).
    * `resubscribe` — cílená obnova subskripcí (produkčně
      `ConnectionManager.resubscribe_now`).
    * `lines_free` — kolik market data lines zbývá; None = neměří se.
    """

    def __init__(
        self,
        snapshot_probe: Callable[[], Awaitable[bool]],
        resubscribe: Callable[[], Awaitable[bool]],
        *,
        lines_free: Callable[[], int | None] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        lines_headroom: int = DEFAULT_LINES_HEADROOM,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._snapshot_probe = snapshot_probe
        self._resubscribe = resubscribe
        self._lines_free = lines_free
        self._timeout_s = timeout_s
        self._min_interval_s = min_interval_s
        self._lines_headroom = lines_headroom
        self._clock = clock
        self._running = False
        self._last_run: float | None = None
        #: Poslední dokončený běh — diagnostika pro log a testy
        self.last: ProbeReport | None = None

    async def trigger(self, reason: str) -> ProbeReport | None:
        """Spustí sondu; None = neběžela (souběh, cooldown, bez rezervy linek).

        Volá se z alert cesty fáze A — nesmí vyhodit nic, co by shodilo
        monitor feedů, proto všechny cesty končí návratem, ne výjimkou.
        """
        if self._running:
            logger.info("Aktivní sonda už běží — druhý trigger se zahazuje (%s)", reason)
            return None
        now = self._clock()
        if self._last_run is not None and now - self._last_run < self._min_interval_s:
            logger.info("Aktivní sonda v cooldownu — trigger se zahazuje (%s)", reason)
            return None
        free = self._lines_free() if self._lines_free is not None else None
        if free is not None and free < self._lines_headroom:
            # Bez rezervy by sonda mohla být poslední kapkou přes strop účtu
            report = ProbeReport(
                outcome="skipped",
                message=f"Aktivní sonda přeskočena: zbývá jen {free} market data lines",
            )
            logger.warning("%s", report.message)
            self.last = report
            return report
        self._running = True
        self._last_run = now
        try:
            report = await self._run(reason)
        finally:
            self._running = False
        self.last = report
        return report

    async def _run(self, reason: str) -> ProbeReport:
        try:
            delivered = await asyncio.wait_for(self._snapshot_probe(), timeout=self._timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Chyba cestou k datům (ConnectionError, timeout uvnitř klienta…)
            # nese stejnou informaci jako mlčící snapshot: k farmě se nedá dostat
            logger.warning("Aktivní sonda selhala (%s) — počítá se jako mrtvá farma", exc)
            delivered = False
        if not delivered:
            return ProbeReport(
                outcome="farm_dead",
                message=(
                    "Aktivní sonda: referenční kontrakt nedostal data — výpadek datové "
                    "farmy IBKR. Resubskripce se nespouští (bouře by nepomohla); "
                    f"čeká se na návrat farmy. Spouštěč: {reason}"
                ),
            )
        # Farma žije → mrtvé jsou subskripce → jedna cílená obnova. Selhání
        # obnovy řeší ConnectionManager sám (disconnect → supervisor přepojí).
        recovered = await self._resubscribe()
        return ProbeReport(
            outcome="subscriptions_dead",
            message=(
                "Aktivní sonda: farma IBKR data dodává, mrtvé byly subskripce — "
                + (
                    "cílená obnova subskripcí proběhla."
                    if recovered
                    else "obnova subskripcí selhala, spojení se přepojuje."
                )
                + f" Spouštěč: {reason}"
            ),
        )
