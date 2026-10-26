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

Mlčící opční univerzum jednoho instrumentu při tikajícím podkladu hlídá
`SilentUniverseWatchdog` (#1604) týmiž prahy a predikátem `universe_silence`,
který sdílí s trade větví CumΔ.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.compute.marketclock import is_market_closed, outside_us_rth
from gexlens_engine.compute.settle import session_bounds, trading_session_date

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


def universe_silence(
    now: dt.datetime, last_event_at: dt.datetime | None, *, since: dt.datetime | None = None
) -> tuple[float, float]:
    """Ticho opčního univerza a jeho práh v okamžiku `now` (s, s).

    Jediný predikát čerstvosti univerza pro trade větev CumΔ (#1460 krok 6)
    i hlídač univerza (#1604). Ticho se měří od posledního eventu univerza
    (jakýkoli typ), nejdřív od openu seance: v 17:00 CT, v nedělním openu i po
    svátku by poslední event z pauzy, pátku nebo svátku jinak dal ticho hned
    v první minutě. `since` posune začátek dál (hlídač: změna aktivní
    expirace). Práh je 180 s v US RTH a 600 s v Globexu. Zavřený trh řeší volající.
    """
    reference = session_bounds(trading_session_date(now))[0]
    for candidate in (last_event_at, since):
        if candidate is not None and candidate > reference:
            reference = candidate
    limit = SILENT_GLOBEX_S if outside_us_rth(now) else SILENT_RTH_S
    return (now - reference).total_seconds(), limit


#: Příčka žebříku hlídače univerza (Rozhodnuto 26. 10. B v #1385)
UniverseRemedy = Literal["resubscribe", "reconnect"]


@dataclass(frozen=True)
class UniverseAction:
    remedy: UniverseRemedy
    reason: str
    #: Alert jen na začátku epizody; další příčky v téže epizodě jdou do logu
    alert: bool
    silence_s: float


@dataclass
class SilentUniverseWatchdog:
    """Hlídač mlčícího opčního univerza jednoho instrumentu (#1604).

    Globální hlídač (#1228) měří poslední event celého streamu. Front future
    jde týmž streamem a subskribuje se první (#845), takže tikající podklad
    zamaskuje mlčící opce. Tady se měří poslední event univerza aktivního
    řetězu instrumentu (`TastyChainCache.last_event_among`), týmž predikátem
    jako trade větev (`universe_silence`). Po změně aktivní expirace (start,
    roll) se nové univerzum teprve subskribuje, proto se ticho měří od změny.

    Žebřík (Rozhodnuto 26. 10. B): napřed cílená resubskripce univerza, futures
    ani druhý instrument nedostanou díru. Když univerzum při dalším tiku pořád
    mlčí, vynucené přepojení celého streamu, pak backoff jako u #1228. Stav má
    každý instrument vlastní instanci.
    """

    symbol: str
    backoff_s: float = RECONNECT_BACKOFF_S
    _expiry: str | None = None
    _armed_at: dt.datetime | None = None
    _last_action: dt.datetime | None = None
    _last_remedy: UniverseRemedy | None = None
    _in_episode: bool = False

    def check(
        self,
        now: dt.datetime,
        *,
        connected: bool,
        expiry: str,
        last_event_at: dt.datetime | None,
    ) -> UniverseAction | None:
        """Jeden vzorek (à minutu) univerza aktivní expirace `expiry`: zásah, nebo None."""
        if expiry != self._expiry:
            self._expiry = expiry
            self._armed_at = now
        if not connected:
            # Spadlé spojení řeší `run` sám a reconnect přihlásí celé univerzum
            return None
        if is_market_closed(now):
            self._end_episode()
            return None
        silence, limit = universe_silence(now, last_event_at, since=self._armed_at)
        if silence < limit:
            self._end_episode()
            return None
        if self._last_remedy == "resubscribe":
            # Resubskripce do dalšího tiku nepomohla → celý stream znovu
            remedy: UniverseRemedy = "reconnect"
        elif (
            self._last_action is not None
            and (now - self._last_action).total_seconds() < self.backoff_s
        ):
            return None
        else:
            remedy = "resubscribe"
        first = not self._in_episode
        self._in_episode = True
        self._last_action = now
        self._last_remedy = remedy
        window = "US RTH" if not outside_us_rth(now) else "Globex"
        step = (
            "cílená resubskripce univerza"
            if remedy == "resubscribe"
            else "resubskripce nepomohla, vynucené přepojení"
        )
        return UniverseAction(
            remedy=remedy,
            reason=(
                f"opční řetěz {self.symbol} mlčí {silence / 60:.0f} min při otevřeném trhu "
                f"({window}, limit {limit / 60:.0f} min) — {step}"
            ),
            alert=first,
            silence_s=silence,
        )

    def _end_episode(self) -> None:
        self._in_episode = False
        self._last_remedy = None


def silent_universe_alert_message(symbol: str, silence_s: float, *, cumdelta_prints: bool) -> str:
    """Text alertu `feed_silent` k prvnímu zásahu hlídače univerza (#1604).

    Pod tisky (v6, ADR-0057 bod 3) doplní dopad na CumΔ instrumentu: trade
    větev se stejným predikátem přestala „běžet", minuty jsou mezera.
    """
    message = (
        f"Opční řetěz {symbol} z tastytrade mlčel {silence_s / 60:.0f} min, podklad tiká — "
        "engine univerzum znovu přihlásil. Pokud kotace nenaběhnou do 2 min, "
        "přepojí celý stream."
    )
    if cumdelta_prints:
        message += f" CumΔ {symbol} z tisků má od prahu ticha mezeru, dokud tisky znovu nenaběhnou."
    return message


def silent_alert_message(silence_s: float, *, cumdelta_prints: bool) -> str:
    """Text alertu `feed_silent` po vynuceném přepojení.

    Pod tisky (`cumdelta_prints`, v6, ADR-0057 bod 3) doplní dopad na CumΔ:
    trade větev se stejnými prahy přestala „běžet", minuty jsou mezera.
    """
    message = (
        f"Tastytrade stream mlčel {silence_s / 60:.0f} min "
        "při otevřeném trhu — engine ho přepojil sám. Pokud "
        "kotace nenaběhnou do 2 min, zkontroluj stav v Settings."
    )
    if cumdelta_prints:
        message += " CumΔ z tisků má od prahu ticha mezeru, dokud tisky znovu nenaběhnou."
    return message
