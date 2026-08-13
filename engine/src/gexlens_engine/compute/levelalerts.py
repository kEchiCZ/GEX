"""Cenové alerty na klíčové úrovně (#675): přiblížení k flipu / zdem.

Detekce „cena se blíží k úrovni" nad minutovými levels (flip, call/put wall).
Proti prostému prahu má dvě pojistky proti spamu:

- **Cooldown per úroveň** — po vystřelení daná úroveň mlčí aspoň cooldown_s.
- **Re-arm hystereze** — úroveň se znovu odjistí až poté, co cena zónu
  opustí (vzdálenost > rearm_ratio × near_points). Konsolidace přilepená
  ke zdi tak vystřelí jednou, ne každou minutu.

Práh se zadává v bodech; volající ho typicky odvodí z kroku striků řetězce
(`strike_step`), takže škáluje per symbol (ES krok 5 b, NQ širší) bez
per-symbol konfigurace. Fáze 2 #575 sem přidá hranu tlumící zóny.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from gexlens_engine.compute.levels import GexLevels

# Popisky úrovní do textu alertu (klíč = atribut GexLevels)
WATCHED_LEVELS: tuple[tuple[str, str], ...] = (
    ("flip", "flip"),
    ("call_wall", "call wall"),
    ("put_wall", "put wall"),
)


def strike_step(strikes: Sequence[float]) -> float:
    """Krok mřížky striků = nejmenší kladný rozdíl sousedů; 0 bez dvou striků."""
    ordered = sorted(set(strikes))
    if len(ordered) < 2:
        return 0.0
    return min(b - a for a, b in itertools.pairwise(ordered) if b > a)


@dataclass(frozen=True)
class ProximityAlert:
    """Jedno vystřelení: cena vstoupila do zóny úrovně."""

    level_name: str
    label: str
    level: float
    price: float
    distance: float


@dataclass
class LevelProximityWatcher:
    """Stavová detekce přiblížení; jeden watcher per (symbol, expirace)."""

    near_points: float
    cooldown_s: float
    #: Násobek near_points, za který musí cena odejít, než se úroveň odjistí
    rearm_ratio: float = 2.0
    _armed: dict[str, bool] = field(default_factory=dict, init=False)
    _last_fire: dict[str, float] = field(default_factory=dict, init=False)

    def observe(self, levels: GexLevels, *, spot: float, now: float) -> list[ProximityAlert]:
        """Vyhodnotí jednu minutu; vrací alerty k publikaci (typicky 0–1).

        `now` je monotónní čas v sekundách (time.monotonic) — cooldown nesmí
        rozhodit posun systémových hodin.
        """
        fired: list[ProximityAlert] = []
        if self.near_points <= 0:
            return fired
        for name, label in WATCHED_LEVELS:
            level = getattr(levels, name)
            if level is None:
                # Úroveň v profilu neexistuje → stav se drží; až se objeví,
                # rozhodne vzdálenost jako obvykle
                continue
            distance = abs(spot - level)
            armed = self._armed.get(name, True)
            if distance <= self.near_points:
                last = self._last_fire.get(name)
                if armed and (last is None or now - last >= self.cooldown_s):
                    fired.append(
                        ProximityAlert(
                            level_name=name,
                            label=label,
                            level=level,
                            price=spot,
                            distance=distance,
                        )
                    )
                    self._last_fire[name] = now
                self._armed[name] = False
            elif distance > self.near_points * self.rearm_ratio:
                self._armed[name] = True
        return fired
