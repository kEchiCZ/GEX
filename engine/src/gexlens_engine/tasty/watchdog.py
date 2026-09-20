"""Hlídač mlčícího DXLink streamu (#1228) — čistá rozhodovací logika.

`DxLinkStream.run` umí jen pád spojení: socket zavřený → backoff → reconnect.
Nepokryté je „socket žije, datový kanál ne" (KEEPALIVE chodí, eventy ne).
Tenhle hlídač měří stáří posledního eventu přes celou cache a při otevřeném
trhu vynutí přepojení; `run` pak projde standardní cestou včetně resubskripce.

Kdy se NESMÍ spustit (regresní scénář 19. 9. 2026 — sobota): zavřený trh
(`is_market_closed`) je ticho z definice, ne porucha. Prahy: US RTH 3 min
(trh nikdy nemlčí 3 min), Globex 10 min (tenká noc mívá minuty bez tisku,
ale ne desítky). Po zásahu backoff 5 min, ať smyčka nekonverguje k
nekonečnému přepojování, když je porucha na straně serveru.
"""

import datetime as dt
from dataclasses import dataclass

from gexlens_engine.compute.marketclock import is_market_closed, outside_us_rth

#: Ticho v US RTH, po kterém se přepojuje (s)
SILENT_RTH_S = 180.0
#: Ticho v Globexu mimo US RTH (s)
SILENT_GLOBEX_S = 600.0
#: Nejkratší rozestup dvou zásahů (s)
RECONNECT_BACKOFF_S = 300.0


@dataclass(frozen=True)
class WatchdogAction:
    reason: str
    #: Alert jen na začátku epizody; opakované zásahy v téže epizodě jdou do logu
    alert: bool
    silence_s: float


@dataclass
class SilentStreamWatchdog:
    rth_silence_s: float = SILENT_RTH_S
    globex_silence_s: float = SILENT_GLOBEX_S
    backoff_s: float = RECONNECT_BACKOFF_S
    _last_action: dt.datetime | None = None
    _armed_at: dt.datetime | None = None
    _in_episode: bool = False

    def check(
        self,
        now: dt.datetime,
        *,
        connected: bool,
        last_event_at: dt.datetime | None,
    ) -> WatchdogAction | None:
        """Jeden vzorek (à minutu): vrátí zásah, nebo None."""
        if self._armed_at is None:
            # Start enginu: subskripce teprve běží — měří se až od prvního vzorku
            self._armed_at = now
        if not connected:
            # Spadlé spojení řeší `run` sám; přepojovat zavřený socket nemá smysl
            return None
        if is_market_closed(now):
            self._in_episode = False
            return None
        limit = self.globex_silence_s if outside_us_rth(now) else self.rth_silence_s
        reference = last_event_at if last_event_at is not None else self._armed_at
        silence = (now - reference).total_seconds()
        if silence < limit:
            self._in_episode = False
            return None
        if (
            self._last_action is not None
            and (now - self._last_action).total_seconds() < self.backoff_s
        ):
            return None
        first = not self._in_episode
        self._in_episode = True
        self._last_action = now
        window = "US RTH" if not outside_us_rth(now) else "Globex"
        return WatchdogAction(
            reason=(
                f"tasty stream mlčí {silence / 60:.0f} min při otevřeném trhu "
                f"({window}, limit {limit / 60:.0f} min) — vynucené přepojení"
            ),
            alert=first,
            silence_s=silence,
        )
