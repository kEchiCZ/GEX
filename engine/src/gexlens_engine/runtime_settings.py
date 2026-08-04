"""Nastavení laditelná za běhu ze Settings UI (issue #438).

Do #438 engine z tabulky `settings` četl jen `strike_range_points` a
`subscription_alert_enabled`. Ostatní pole v UI (retence, disk limit, velikost
dávky, šířka hot zóny) se sice uložila, ale engine je nikdy nepřečetl — uživatel
změnil hodnotu, UI ji ukázalo a nestalo se nic.

Tento modul drží jediný seznam laditelných klíčů i s mezemi a rozhoduje, co
změna vyžaduje: část hodnot stačí přepsat (retence a disk limit se čtou až při
nočním purge), část si vynutí restart pipeline (šířka pásma, dávka, hot zóna
se promítají do sestavení subskripcí).

Meze nejsou vycucané: vycházejí z ADR-0001 (naměřená kapacita účtu) a
z invariantů konfigurace, aby uživatel v UI nemohl engine rozbít.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from gexlens_engine.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSetting:
    """Jeden laditelný klíč: meze, typ a dopad změny."""

    key: str
    #: Dolní mez; callable kvůli mezím odvozeným z jiné konfigurace
    minimum: float | Callable[[Settings], float]
    maximum: float | Callable[[Settings], float]
    integer: bool = False
    #: Změna se projeví až po znovupostavení pipeline (subskripce, obálka)
    restarts_pipelines: bool = False

    def bounds(self, settings: Settings) -> tuple[float, float]:
        low = self.minimum(settings) if callable(self.minimum) else self.minimum
        high = self.maximum(settings) if callable(self.maximum) else self.maximum
        return low, high


RUNTIME_SETTINGS: tuple[RuntimeSetting, ...] = (
    # Obálka strikes: min. smysluplné pásmo; strop je invariant konfigurace
    # (strike_range_max_points ≥ 2× šířka), viz ADR-0002
    RuntimeSetting(
        key="strike_range_points",
        minimum=50.0,
        maximum=lambda s: s.strike_range_max_points / 2,
        restarts_pipelines=True,
    ),
    # Dávka subskripcí musí zůstat pod kapacitou market data lines účtu
    # (ADR-0001: naměřeno ≥ 150) s rezervou na hot zónu a podklad
    RuntimeSetting(
        key="batch_size",
        minimum=10,
        maximum=lambda s: float(s.market_data_lines),
        integer=True,
        restarts_pipelines=True,
    ),
    # Hot zóna se stejně ořízne na počet pokrytelný tick-by-tick streamy
    # (ADR-0001: strop 5), širší hodnota jen rozšíří midpoint klasifikaci
    RuntimeSetting(
        key="hot_zone_width", minimum=1, maximum=50, integer=True, restarts_pipelines=True
    ),
    # Retence i disk limit se čtou až nočním purge jobem — restart netřeba
    RuntimeSetting(key="retention_days", minimum=1, maximum=3650, integer=True),
    RuntimeSetting(key="disk_limit_gb", minimum=0.5, maximum=1000.0),
)


def coerce_setting(value: object, spec: RuntimeSetting, settings: Settings) -> float | int | None:
    """Hodnota z UI převedená a sevřená do mezí; `None` = nepoužitelná.

    Bool se odmítá schválně: v Pythonu je podtypem int, takže `True` by jinak
    prošlo jako 1 a tiše přepsalo číselné nastavení.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number:  # NaN
        return None
    low, high = spec.bounds(settings)
    clamped = min(max(number, low), high)
    return int(round(clamped)) if spec.integer else clamped


def apply_runtime_settings(settings: Settings, values: dict[str, object]) -> bool:
    """Promítne hodnoty ze Settings UI do konfigurace.

    Vrací `True`, když některá změna vyžaduje znovupostavení pipeline.
    """
    restart = False
    for spec in RUNTIME_SETTINGS:
        raw = values.get(spec.key)
        if raw is None:
            continue
        new_value = coerce_setting(raw, spec, settings)
        if new_value is None:
            logger.warning(
                "Runtime nastavení %s: nepoužitelná hodnota %r — ignoruji", spec.key, raw
            )
            continue
        current = getattr(settings, spec.key)
        if new_value == current:
            continue
        logger.info(
            "Runtime změna %s: %s → %s%s",
            spec.key,
            current,
            new_value,
            " (pipeline se překlopí)" if spec.restarts_pipelines else "",
        )
        setattr(settings, spec.key, new_value)
        restart = restart or spec.restarts_pipelines
    return restart
