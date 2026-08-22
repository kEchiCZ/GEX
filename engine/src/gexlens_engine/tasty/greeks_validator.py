"""Greeks validátor (#614, poslední kus): tasty × IBKR modely se hlídají navzájem.

Prahy jsou MĚŘENÉ — 2× p95 |odchylky| z 5 čistých shadow seancí (20 M řádků,
finále 22. 8. 2026 v #614). Za zdravého provozu je nad prahem ~1–2 % kontraktů
(okraje řetězce); rozjezd modelů (zamrzlé greeks po rollu, vadná IV jedné
strany) jich zvedne desítky procent. Alert `greeks_suspect` proto střílí až
na PODÍLU podezřelých > práh po N minut v řadě — stejný duch hystereze jako
křížová kontrola #517, jejíž kalibrace se osvědčila.

Rozhodnutí uživatele 22. 8.: **jen hlásit** (alert + /status), žádný
automatický zásah do fallbacku. Podíl se počítá výhradně z kontraktů, kde
greeks dodaly OBĚ strany — jinak by ES kvůli 40% díře tasty greeks (#810)
alarmoval trvale.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Pod tímhle počtem párů se podíl nevyhodnocuje (víkend, tenký řetěz) —
#: 3 podezřelé z 10 nejsou signál, ale šum malého vzorku.
MIN_PAIRS = 20


@dataclass(frozen=True)
class GreeksThresholds:
    """|Δ| prahy „podezřelého" kontraktu — 2× p95 z měření (#614)."""

    delta: float
    gamma: float
    iv: float


#: Měřené prahy per podklad (finále #614, 22. 8. 2026). Nezměřený symbol
#: dostává ES hodnoty — konzervativnější (menší prahy = citlivější).
THRESHOLDS: Mapping[str, GreeksThresholds] = {
    "ES": GreeksThresholds(delta=0.04, gamma=0.0012, iv=0.25),
    "NQ": GreeksThresholds(delta=0.10, gamma=0.0005, iv=0.20),
}
DEFAULT_THRESHOLDS = THRESHOLDS["ES"]


def thresholds_for(symbol: str) -> GreeksThresholds:
    return THRESHOLDS.get(symbol, DEFAULT_THRESHOLDS)


def is_suspicious(
    symbol: str,
    *,
    delta_ibkr: float | None,
    delta_tasty: float | None,
    gamma_ibkr: float | None,
    gamma_tasty: float | None,
    iv_ibkr: float | None,
    iv_tasty: float | None,
) -> bool | None:
    """None = pár nejde vyhodnotit (některé pole chybí na některé straně)."""
    pairs = [
        (delta_ibkr, delta_tasty),
        (gamma_ibkr, gamma_tasty),
        (iv_ibkr, iv_tasty),
    ]
    if any(a is None or b is None for a, b in pairs):
        return None
    limits = thresholds_for(symbol)
    assert delta_ibkr is not None and delta_tasty is not None
    assert gamma_ibkr is not None and gamma_tasty is not None
    assert iv_ibkr is not None and iv_tasty is not None
    return (
        abs(delta_ibkr - delta_tasty) > limits.delta
        or abs(gamma_ibkr - gamma_tasty) > limits.gamma
        or abs(iv_ibkr - iv_tasty) > limits.iv
    )


@dataclass(frozen=True)
class GreeksAlert:
    symbol: str
    share: float
    checked: int
    message: str


class GreeksValidator:
    """Hystereze nad minutovými podíly podezřelých kontraktů per podklad."""

    def __init__(
        self,
        *,
        share_threshold: float = 0.20,
        minutes_threshold: int = 3,
        cooldown_minutes: int = 60,
    ) -> None:
        self._share_threshold = share_threshold
        self._minutes_threshold = minutes_threshold
        self._cooldown_minutes = cooldown_minutes
        self._streak: dict[str, int] = {}
        self._cooldown: dict[str, int] = {}
        #: Poslední vyhodnocené podíly pro /status (symbol → podíl 0–1)
        self.last_shares: dict[str, float] = {}

    def observe(
        self, checked: Mapping[str, int], suspicious: Mapping[str, int]
    ) -> list[GreeksAlert]:
        """Jedna minuta porovnání; vrací alerty za symboly, kde série dozrála."""
        alerts: list[GreeksAlert] = []
        for symbol in checked:
            n = checked[symbol]
            if n < MIN_PAIRS:
                # Malý vzorek podíl nevyhodnocuje ANI nenuluje sérii — víkendová
                # minuta nesmí resetovat rozjetou detekci
                self.last_shares.pop(symbol, None)
                continue
            share = suspicious.get(symbol, 0) / n
            self.last_shares[symbol] = share
            if self._cooldown.get(symbol, 0) > 0:
                self._cooldown[symbol] -= 1
            if share <= self._share_threshold:
                self._streak[symbol] = 0
                continue
            self._streak[symbol] = self._streak.get(symbol, 0) + 1
            if self._streak[symbol] < self._minutes_threshold:
                continue
            if self._cooldown.get(symbol, 0) > 0:
                continue
            self._cooldown[symbol] = self._cooldown_minutes
            alerts.append(
                GreeksAlert(
                    symbol=symbol,
                    share=share,
                    checked=n,
                    message=(
                        f"Greeks modely se rozjely ({symbol}): {share:.0%} z {n} kontraktů "
                        f"nad měřeným prahem po {self._streak[symbol]} min — tasty greeks "
                        "jsou pro fallback řetězu nespolehlivé (#614)."
                    ),
                )
            )
        return alerts

    def status_fields(self) -> dict[str, object]:
        """Pole do /status — podíly poslední vyhodnocené minuty per podklad."""
        if not self.last_shares:
            return {}
        return {
            "tasty_greeks_mismatch": {
                symbol: round(share, 4) for symbol, share in sorted(self.last_shares.items())
            }
        }
