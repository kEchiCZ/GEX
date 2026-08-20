"""Recorder surových TimeAndSale eventů z dxFeed (#795): učicí data pro #794/#615.

TimeAndSale s `aggressorSide` do enginu teče od zavedení shadow subskripce
(#613), ale jen se počítal (`TastyChainCache.trades*`). Trade-level data jsou
přitom jediná učicí data, která každý den nenávratně mizí — zpětně je žádné
API nedá. Recorder je proto ukládá SUROVÁ: žádná klasifikace ani interpretace,
tu dělá až #615 (plná klasifikace agresora).

Vědomé vynechání: prints PODKLADU (front future) se nezaznamenávají. Futures
mají řádově miliony printů/den — denní partice držená celá v paměti
(`_PartitionBuffer`) by na nich bobtnala do stovek MB, a CumΔ podkladu už
nese IBKR větev (bary + ticks hot zóny). Učicí hodnota #795 je v OPČNÍCH
tradech; mapping recorderu proto plní jen chain symboly (viz `__main__`).
"""

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Callable

from gexlens_engine.storage.parquet_store import TastyTradeRow
from gexlens_engine.tasty.provider import _number

logger = logging.getLogger(__name__)


def _flag(value: object) -> bool | None:
    """dxFeed COMPACT posílá bool, občas string „true"/„false" — jinak None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


class TradesRecorder:
    """Buffer TimeAndSale eventů per kořenový symbol; drain volá flush smyčka.

    `on_event` běží synchronně v callbacku DxLinkStream (jediný event loop),
    `drain` v téže smyčce — žádné zámky nejsou potřeba. Blokující Parquet
    zápis dělá volající přes `asyncio.to_thread` nad výsledkem drain.
    """

    def __init__(self, clock: Callable[[], dt.datetime] | None = None) -> None:
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        #: streamer symbol → kořenový symbol instrumentu ("ES"/"NQ")
        self._mapping: dict[str, str] = {}
        self._pending: dict[str, list[TastyTradeRow]] = defaultdict(list)
        #: Diagnostika pro /status a log: co se zapsalo a co spadlo mimo mapu
        self.recorded = 0
        self.dropped_unmapped = 0

    def set_mapping(self, mapping: dict[str, str]) -> None:
        """Nahradí mapu streamer → root; plní ji denní obnova chain map."""
        self._mapping = dict(mapping)

    def on_event(self, event_type: str, values: list[object]) -> None:
        """EventCallback větev pro TimeAndSale — pořadí polí dle EVENT_FIELDS."""
        if event_type != "TimeAndSale" or not values:
            return
        symbol = str(values[0])
        root = self._mapping.get(symbol)
        if root is None:
            # Podklad (záměr, viz docstring) nebo event před první chain mapou
            self.dropped_unmapped += 1
            return
        price = _number(values[2]) if len(values) > 2 else None
        if price is None:
            return  # print bez ceny nemá učicí hodnotu
        time_ms = _number(values[1]) if len(values) > 1 else None
        ts = (
            dt.datetime.fromtimestamp(time_ms / 1000.0, tz=dt.UTC)
            if time_ms is not None and time_ms > 0
            else self._clock()
        )
        aggressor = values[4] if len(values) > 4 else None
        self._pending[root].append(
            TastyTradeRow(
                ts=ts,
                streamer_symbol=symbol,
                price=price,
                size=_number(values[3]) if len(values) > 3 else None,
                aggressor=str(aggressor) if isinstance(aggressor, str) else None,
                spread_leg=_flag(values[5]) if len(values) > 5 else None,
                eth=_flag(values[6]) if len(values) > 6 else None,
            )
        )
        self.recorded += 1

    def drain(self) -> dict[tuple[str, dt.date], list[TastyTradeRow]]:
        """Odebere nasbírané řádky seskupené per (root, den UTC z ts eventu).

        Den se bere z časové značky eventu, ne z hodin flushe — trady těsně
        před půlnocí UTC by jinak spadly do partice následujícího dne.
        """
        pending, self._pending = self._pending, defaultdict(list)
        batches: dict[tuple[str, dt.date], list[TastyTradeRow]] = defaultdict(list)
        for root, rows in pending.items():
            for row in rows:
                batches[(root, row.ts.date())].append(row)
        return dict(batches)
