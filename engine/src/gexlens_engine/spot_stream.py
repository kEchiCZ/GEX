"""Throttlovaný živý spot podkladu (#128) — publish `spot.{symbol}` z ticker.updateEvent.

Callback ib_async je synchronní; napojení na event a plánování async publishe zůstává
v `__main__` (živé IBKR se v CI netestuje, pravidlo 4). Zde je jen čistá throttle logika,
kterou lze deterministicky otestovat bez ib_async i bez event loopu.
"""

from __future__ import annotations

from .runtime import PublisherLike


class SpotStreamer:
    """Rozhoduje, zda z příchozího ticku publikovat spot (throttle na min. interval).

    `sample(price, now)` vrací cenu k publikaci, nebo None (throttle / NaN / zastaveno).
    Stav (poslední čas publishe) drží instance; `now` je monotónní čas v sekundách.
    """

    def __init__(self, publisher: PublisherLike, symbol: str, *, min_interval_s: float = 0.2):
        self.publisher = publisher
        self.symbol = symbol
        self.min_interval_s = min_interval_s
        self._last_ts: float | None = None
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def sample(self, price: float, now: float) -> float | None:
        """Cena k publikaci, nebo None. `price != price` je NaN test (bez importu math)."""
        if self._stopped or price != price:
            return None
        if self._last_ts is not None and now - self._last_ts < self.min_interval_s:
            return None
        self._last_ts = now
        return float(price)
