"""Walls módy (SPEC 4.4): Peak, Center, Smooth (EMA), Flip, Ridge.

Vstupem je časová řada vrstev heatmapy (per t slovník strike → hodnota metriky,
zvlášť call/put vrstva — viz compute.heatmap). Vše čisté funkce.

- Peak: argmax metriky per t.
- Center: vážené těžiště per t.
- Smooth: EMA Peak řady (span 15 min, α = 2/(span+1)).
- Flip: zero-gamma řada z levels (SPEC 4.2) — walls ji jen předávají.
- Ridge: lokální maxima profilu s prominence filtrem, spojená mezi sousedními
  časy nejbližším strikem do více souběžných hřebenů.
"""

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

Layer = Mapping[float, float]


class WallsMode(enum.Enum):
    PEAK = "peak"
    CENTER = "center"
    SMOOTH = "smooth"
    FLIP = "flip"
    RIDGE = "ridge"


@dataclass
class RidgeTrack:
    """Jeden hřeben: posloupnost (index času, strike) spojená nejbližším strikem."""

    points: list[tuple[int, float]] = field(default_factory=list)

    @property
    def last_strike(self) -> float:
        return self.points[-1][1]

    @property
    def last_t(self) -> int:
        return self.points[-1][0]


def peak_of_layer(layer: Layer) -> float | None:
    """Argmax metriky; None pro prázdnou/nulovou vrstvu (není co ukázat)."""
    if not layer:
        return None
    strike = max(layer, key=lambda k: layer[k])
    return strike if layer[strike] > 0.0 else None


def center_of_layer(layer: Layer) -> float | None:
    """Vážené těžiště vrstvy (|hodnota| jako váha, funguje i pro signed vrstvy)."""
    total = sum(abs(v) for v in layer.values())
    if total == 0.0:
        return None
    return sum(k * abs(v) for k, v in layer.items()) / total


def peak_series(layers: Sequence[Layer]) -> list[float | None]:
    return [peak_of_layer(layer) for layer in layers]


def center_series(layers: Sequence[Layer]) -> list[float | None]:
    return [center_of_layer(layer) for layer in layers]


def smooth_series(values: Sequence[float | None], span: int = 15) -> list[float | None]:
    """EMA řady (None mezery drží poslední EMA stav, dokud nepřijde první hodnota)."""
    if span < 1:
        raise ValueError(f"EMA span musí být ≥ 1, dostal jsem {span}")
    alpha = 2.0 / (span + 1.0)
    ema: float | None = None
    result: list[float | None] = []
    for value in values:
        if value is not None:
            ema = value if ema is None else alpha * value + (1.0 - alpha) * ema
        result.append(ema)
    return result


def local_maxima(layer: Layer, prominence_ratio: float = 0.1) -> list[float]:
    """Lokální maxima profilu s relativním prominence filtrem.

    Prominence vrcholu = jeho výška mínus nejvyšší sedlo směrem k vyššímu
    vrcholu; vrcholy s prominencí < prominence_ratio × globální maximum
    se zahazují (šum profilu netvoří hřeben).
    """
    strikes = sorted(layer)
    values = [layer[k] for k in strikes]
    n = len(strikes)
    if n == 0:
        return []
    global_max = max(values)
    if global_max <= 0.0:
        return []
    threshold = prominence_ratio * global_max

    maxima: list[float] = []
    for i in range(n):
        left = values[i - 1] if i > 0 else float("-inf")
        right = values[i + 1] if i < n - 1 else float("-inf")
        if values[i] <= left or values[i] <= right:
            continue
        prominence = _prominence(values, i)
        if prominence >= threshold:
            maxima.append(strikes[i])
    return maxima


def _prominence(values: Sequence[float], index: int) -> float:
    """Prominence vrcholu: výška nad nejvyšším sedlem směrem k vyššímu vrcholu."""
    height = values[index]
    saddles: list[float] = []
    for step in (-1, 1):
        lowest = height
        i = index + step
        saddle: float | None = None
        while 0 <= i < len(values):
            lowest = min(lowest, values[i])
            if values[i] > height:
                saddle = lowest
                break
            i += step
        if saddle is not None:
            saddles.append(saddle)
    if not saddles:
        return height  # globální maximum — prominence = plná výška
    return height - max(saddles)


def ridge_tracks(
    layers: Sequence[Layer],
    prominence_ratio: float = 0.1,
    max_strike_gap: float | None = None,
) -> list[RidgeTrack]:
    """Spojí lokální maxima mezi sousedními časy nejbližším strikem (SPEC 4.4 Ridge).

    Nespárované maximum zakládá nový hřeben; hřeben bez pokračování končí.
    max_strike_gap omezuje, jak daleko smí hřeben mezi minutami přeskočit.
    """
    tracks: list[RidgeTrack] = []
    open_tracks: list[RidgeTrack] = []
    for t, layer in enumerate(layers):
        maxima = local_maxima(layer, prominence_ratio)
        candidates = [
            (abs(track.last_strike - strike), strike, track)
            for strike in maxima
            for track in open_tracks
            if track.last_t == t - 1
            and (max_strike_gap is None or abs(track.last_strike - strike) <= max_strike_gap)
        ]
        candidates.sort(key=lambda item: item[0])
        matched_strikes: set[float] = set()
        matched_tracks: set[int] = set()
        for _, strike, track in candidates:
            if strike in matched_strikes or id(track) in matched_tracks:
                continue
            track.points.append((t, strike))
            matched_strikes.add(strike)
            matched_tracks.add(id(track))
        for strike in maxima:
            if strike not in matched_strikes:
                new_track = RidgeTrack(points=[(t, strike)])
                tracks.append(new_track)
                open_tracks.append(new_track)
        open_tracks = [track for track in open_tracks if track.last_t == t]
    return tracks


def compute_walls(
    mode: WallsMode,
    layers: Sequence[Layer],
    *,
    flip_series: Sequence[float | None] | None = None,
    smooth_span: int = 15,
    prominence_ratio: float = 0.1,
) -> list[float | None] | list[RidgeTrack]:
    """Dispatcher pro API: jedna metrika vrstvy → linie/hřebeny dle módu."""
    if mode is WallsMode.PEAK:
        return peak_series(layers)
    if mode is WallsMode.CENTER:
        return center_series(layers)
    if mode is WallsMode.SMOOTH:
        return smooth_series(peak_series(layers), span=smooth_span)
    if mode is WallsMode.FLIP:
        if flip_series is None:
            raise ValueError("Mód FLIP vyžaduje flip_series z levels (SPEC 4.2)")
        return list(flip_series)
    if mode is WallsMode.RIDGE:
        return ridge_tracks(layers, prominence_ratio)
    raise ValueError(f"Neznámý walls mód: {mode!r}")
