"""Provozní alerty (SPEC 7.5, issue #21; rozsah zúžen v #949): výpadek spojení a disk limit.

Publikuje do WS kanálu `alerts`. Obě hlášky jsou **globální** — nestojí na
žádném pravidle v tabulce `alerts`, takže fungují bez konfigurace.

Rozhodnutí #949 (varianta B): cenové druhy `price_cross`, `cum_delta_jump`
a `dominant_strike_change` byly odstraněny. Nikdy se nevolaly a cenové prahy
mezitím pokrývá `LevelProximityWatcher` v enginu (#675) bez konfigurace —
držet druhou, mrtvou cestu k témuž jen mátlo čtenáře i statistiky.

Obě hlášky jsou HRANOVÉ: vystřelí při přechodu do vadného stavu a znovu se
natáhnou, až se stav vrátí. Bez toho by se opakovaly s každým statusem
z enginu (~1×/min) a zvoneček by se stal nepoužitelným.
"""

import datetime as dt
import enum
import time

from gexlens_api.live import LiveHub
from gexlens_engine.compute.marketclock import is_market_closed


class AlertKind(enum.Enum):
    DISCONNECT = "disconnect"
    DISK_LIMIT = "disk_limit"


class AlertEngine:
    """Provozní alerty; vystřelení publikuje do kanálu `alerts`."""

    def __init__(self, hub: LiveHub) -> None:
        self._hub = hub
        # Hranové natažení: True = ve vadném stavu, další vystřelení až po návratu
        self._disconnected = False
        self._disk_over = False

    def observe_connection(self, connection: str | None, *, now: dt.datetime | None = None) -> bool:
        """Sleduje stav spojení; vystřelí JEN při přechodu do odpojeno.

        `None` (engine stav neposlal) se ignoruje — chybějící údaj není výpadek,
        jinak by každý neúplný status zvonil.

        Při zavřeném trhu podle rozvrhu CME (#1307; víkend, denní pauza) se
        nehlásí: sběr dat nestojí, protože žádná data nejsou (údržba IBKR
        o víkendu, denní odpojení kolem 00:00 UTC). Hrana se přitom
        nenatahuje, takže trvá-li výpadek i po otevření, ohlásí ho první
        status po otevření. Délku výpadku loguje watchdog enginu.
        """
        if connection is None:
            return False
        if connection == "connected":
            self._disconnected = False
            return False
        if self._disconnected:
            return False
        if is_market_closed(now if now is not None else dt.datetime.now(dt.UTC)):
            return False
        self._disconnected = True
        self._fire(0, AlertKind.DISCONNECT, "*", f"Výpadek spojení s IBKR: {connection}")
        return True

    def observe_disk(self, usage_bytes: object, limit_bytes: object) -> bool:
        """Sleduje obsazení disku; vystřelí JEN při překročení limitu."""
        if not isinstance(usage_bytes, int | float) or not isinstance(limit_bytes, int | float):
            return False
        if limit_bytes <= 0:
            return False
        over = usage_bytes >= limit_bytes
        if over and not self._disk_over:
            self._disk_over = True
            self._fire(
                0,
                AlertKind.DISK_LIMIT,
                "*",
                f"Obsazení disku {usage_bytes / 1e9:.1f} GB překročilo limit "
                f"{limit_bytes / 1e9:.1f} GB",
            )
            return True
        if not over:
            self._disk_over = False
        return False

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
