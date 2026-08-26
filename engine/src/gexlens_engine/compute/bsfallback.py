"""Hlídka objemu BS fallback greeks (#877, follow-up #862) — čistá logika.

BS dopočet z mid (#547) je správný fallback, ale 24.–25. 8. běžel 29 hodin
v kuse (NQ ~62 řádků/min) a nikdo to neviděl: greeks validátor (#614) měří
mismatch tasty×IBKR, podíl BS-dopočtených striků nehlídal nikdo. Hlídka
sleduje podíl per cyklus a hlásí EPIZODU: podíl nad prahem nepřetržitě déle
než `min_duration_s`. Krátké nárazy kolem restartu TWS (blip 23. 8.
21:02–21:32 měl mezery, epizodu nesloží) alert spouštět nesmí.

Tlumení po vzoru #517: jeden alert při vzniku epizody, pak nejdřív po
`cooldown_s` („pořád trvá"), a jedno oznámení o návratu do normálu.
"""

from dataclasses import dataclass, field

#: Podíl striků s BS greeks, od kterého se počítá epizoda. Zdravý provoz má
#: 0 % (26. 8. celý den); bouře #862 běžela na ~40–100 % blízkých řetězů.
SHARE_THRESHOLD = 0.20

#: Jak dlouho musí podíl držet nad prahem, než je to epizoda (ne blip).
MIN_DURATION_S = 900.0

#: Připomínka běžící epizody nejdřív po hodině — minutový cyklus nesmí spamovat.
COOLDOWN_S = 3600.0


@dataclass
class BsFallbackWatcher:
    """Epizody vysokého podílu BS greeks; `observe` vrací text alertu, nebo None."""

    symbol: str
    threshold: float = SHARE_THRESHOLD
    min_duration_s: float = MIN_DURATION_S
    cooldown_s: float = COOLDOWN_S

    #: Aktuální podíl (0–1) pro /status — plní se každým cyklem.
    share: float = field(default=0.0, init=False)
    #: Začátek běžící epizody (monotonic); None = podíl pod prahem.
    episode_started: float | None = field(default=None, init=False)
    _last_alert: float | None = field(default=None, init=False)
    _alerted: bool = field(default=False, init=False)

    def observe(self, *, bs_count: int, total: int, now: float) -> str | None:
        """Jeden cyklus: podíl + stav epizody. Vrací zprávu k publikaci, nebo None."""
        self.share = bs_count / total if total > 0 else 0.0
        if self.share < self.threshold:
            recovered = self._alerted
            self.episode_started = None
            self._last_alert = None
            self._alerted = False
            if recovered:
                return (
                    f"{self.symbol}: TWS model greeks se vrátil — BS fallback skončil "
                    f"(podíl {self.share:.0%})."
                )
            return None
        if self.episode_started is None:
            self.episode_started = now
        duration = now - self.episode_started
        if duration < self.min_duration_s:
            return None
        if self._last_alert is not None and now - self._last_alert < self.cooldown_s:
            return None
        self._last_alert = now
        self._alerted = True
        minutes = int(duration // 60)
        return (
            f"{self.symbol}: greeks jedou z BS fallbacku (#547) — {self.share:.0%} striků "
            f"už {minutes} min. TWS model nedodává; při bouři #862 pomohl až restart TWS "
            f"(farmy usopt/usfuture)."
        )

    def status_fields(self) -> dict[str, object]:
        """Pole do /status: podíl + případný začátek epizody (epoch ISO nejde
        z monotonic — hlásí se délka v sekundách)."""
        out: dict[str, object] = {"share": round(self.share, 4)}
        if self.episode_started is not None:
            out["episode"] = True
        return out


def episode_seconds(watcher: BsFallbackWatcher, now: float) -> float | None:
    """Délka běžící epizody v sekundách; None mimo epizodu (pro testy a UI)."""
    if watcher.episode_started is None:
        return None
    return max(0.0, now - watcher.episode_started)


__all__ = [
    "COOLDOWN_S",
    "MIN_DURATION_S",
    "SHARE_THRESHOLD",
    "BsFallbackWatcher",
    "episode_seconds",
]
