"""Měření obsazených market data lines (#630).

Původní `lines_utilization` byl statický podíl batch_size/market_data_lines —
konstanta 0,8 vydávaná za živý údaj. Tohle měřidlo čte skutečný stav: zdrojem
je registr subskripcí v ib_async (co u TWS opravdu běží), počítadlo dodává
volající jako callable, takže třída sama na ib_async nezávisí a testuje se
čistě.

Sweep drží dávku jen mezi subskripcí a cancel — okamžitý stav v čase status
pushe (jednou za minutu, po doběhu sweepu) by dávku minul a ukázal jen trvalé
streamy. Gauge proto sbírá špičku mezi čteními: `sample()` volá streamer po
každé subskripci, `take_peak()` špičku vydá a začne měřit znovu. Čtenář je
jediný — orchestrátor agregovaného statusu v `__main__`.
"""

from collections.abc import Callable


class LineGauge:
    """Špička souběžně držených market data lines mezi čteními."""

    def __init__(self, count_active: Callable[[], int]) -> None:
        self._count_active = count_active
        self._peak = 0

    def sample(self) -> int:
        """Zaznamená aktuální stav; volat po každé nové subskripci."""
        current = self._count_active()
        if current > self._peak:
            self._peak = current
        return current

    def take_peak(self) -> int:
        """Špička od posledního čtení; resetuje měření na aktuální stav."""
        current = self._count_active()
        peak = max(self._peak, current)
        self._peak = current
        return peak

    def utilization(self, market_data_lines: int) -> float:
        """Podíl špičky vůči stropu účtu (ADR-0001: tvrdých 100 linek)."""
        if market_data_lines <= 0:
            return 0.0
        return min(1.0, self.take_peak() / market_data_lines)
