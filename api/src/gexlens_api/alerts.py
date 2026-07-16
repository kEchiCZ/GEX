"""Alert engine (SPEC 7.5, issue #21): vyhodnocení podmínek a publikace do WS kanálu alerts.

Druhy alertů: cena × flip/wall cross, skok CumΔ o práh, změna dominantního
striku, výpadek spojení, disk limit. Stav (poslední hodnoty) drží engine per
alert id; každé vystřelení jde do LiveHub kanálu `alerts`.
"""

import enum
import time

from gexlens_api.live import LiveHub


class AlertKind(enum.Enum):
    PRICE_CROSS = "price_cross"
    CUM_DELTA_JUMP = "cum_delta_jump"
    DOMINANT_STRIKE_CHANGE = "dominant_strike_change"
    DISCONNECT = "disconnect"
    DISK_LIMIT = "disk_limit"


class AlertEngine:
    """Stavové vyhodnocování alertů; vystřelení publikuje do kanálu `alerts`."""

    def __init__(self, hub: LiveHub) -> None:
        self._hub = hub
        self._last_price: dict[int, float] = {}
        self._last_cum: dict[int, float] = {}
        self._last_dominant: dict[int, float] = {}

    def price_cross(
        self, alert_id: int, symbol: str, price: float, level: float, level_name: str
    ) -> bool:
        """Cross detekce: vystřelí, když cena protne úroveň mezi dvěma vzorky."""
        previous = self._last_price.get(alert_id)
        self._last_price[alert_id] = price
        if previous is None or previous == level:
            return False
        crossed = (previous < level <= price) or (previous > level >= price)
        if crossed:
            direction = "nahoru" if price >= level else "dolů"
            self._fire(
                alert_id,
                AlertKind.PRICE_CROSS,
                symbol,
                f"{symbol}: cena {price:g} protnula {level_name} {level:g} {direction}",
            )
        return crossed

    def cum_delta_jump(
        self, alert_id: int, symbol: str, cum_delta: float, threshold: float
    ) -> bool:
        """Skok CumΔ mezi vzorky o ≥ threshold (konfigurovatelný práh, SPEC 7.5)."""
        previous = self._last_cum.get(alert_id)
        self._last_cum[alert_id] = cum_delta
        if previous is None:
            return False
        jump = abs(cum_delta - previous)
        if jump >= threshold:
            self._fire(
                alert_id,
                AlertKind.CUM_DELTA_JUMP,
                symbol,
                f"{symbol}: CumΔ skok o {jump:g} (práh {threshold:g})",
            )
            return True
        return False

    def dominant_strike_change(self, alert_id: int, symbol: str, strike: float) -> bool:
        previous = self._last_dominant.get(alert_id)
        self._last_dominant[alert_id] = strike
        if previous is None or previous == strike:
            return False
        self._fire(
            alert_id,
            AlertKind.DOMINANT_STRIKE_CHANGE,
            symbol,
            f"{symbol}: dominantní strike {previous:g} → {strike:g}",
        )
        return True

    def connection_lost(self, detail: str) -> None:
        self._fire(0, AlertKind.DISCONNECT, "*", f"Výpadek spojení s IBKR: {detail}")

    def disk_limit_exceeded(self, usage_bytes: int, limit_bytes: int) -> None:
        self._fire(
            0,
            AlertKind.DISK_LIMIT,
            "*",
            f"Obsazení disku {usage_bytes} B překročilo limit {limit_bytes} B",
        )

    def _fire(self, alert_id: int, kind: AlertKind, symbol: str, message: str) -> None:
        self._hub.publish(
            "alerts",
            {
                "alert_id": alert_id,
                "kind": kind.value,
                "symbol": symbol,
                "message": message,
                "ts": time.time(),
            },
        )
