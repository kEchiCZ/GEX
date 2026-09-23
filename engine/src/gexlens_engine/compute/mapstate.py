"""Stav „tenká mapa" (#1245) — čisté funkce bez I/O.

Útes gammy (#576) říká, co po settle **odpadlo**; tenhle modul říká, co
**teď zbylo**. Referenční den 21. 9. 2026 po kvartálním OPEX: net gamma na
spotu u nuly, flip 7 651 při ceně 7 650, call wall, put wall i max pain na
7 650 — dealeři nemají co hedgovat, nic netlumí a nic nepinuje. Stav může
nastat i mimo OPEX: po prudkém pohybu, který cenu vyvedl z pozicování, nebo
v prvních dnech nové expirace.

Práh je **relativní** — percentil vlastní historie symbolu (#434): mediány
`total_gex` se mezi ES a NQ liší o dva až tři řády, absolutní práh v
jednotkách gammy je nepoužitelný. Bez dostatečné historie se podmínka
tenké gammy NEURČUJE (None), nikdy se nedosazuje „není tenká" jako default.

Fáze 1 jen měří a ukazuje; nic se podle stavu neblokuje.
"""

import datetime as dt
import statistics
from dataclasses import dataclass

from gexlens_engine.compute.tendency import WALL_WEAK_DOMINANCE

#: Verze definice — ukládá se k řádku seance, prahy se budou kalibrovat.
MAP_STATE_VERSION = 1
#: Percentil klouzavé historie, pod kterým je gamma „tenká".
THIN_PERCENTILE = 0.25
#: Klouzavé okno seancí pro percentil a minimální vzorek, pod kterým se
#: tenká gamma neurčuje (percentil z hrstky dnů by byl náhoda).
WINDOW_SESSIONS = 20
MIN_SAMPLE = 10
#: Slitá mapa: flip, obě zdi a max pain do tohoto podílu ceny od sebe.
FUSED_MAP_PCT = 0.0025
#: Kolik ze tří podmínek musí platit.
THIN_CONDITIONS_MIN = 2


@dataclass(frozen=True)
class MapInputs:
    """Minutový vstup — všechno už engine počítá, nic nového se neměří."""

    ts_min: dt.datetime
    spot: float
    total_gex: float | None
    #: Gamma Dyn GEX profilu v místě ceny (`gexfield.gamma_at_price`).
    gamma_at_price: float | None
    call_wall_dom: float | None
    put_wall_dom: float | None
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None


@dataclass(frozen=True)
class MapHistory:
    """Prahy z klouzavého okna seancí téhož symbolu (percentil mediánů)."""

    gex_abs_threshold: float | None
    gamma_abs_threshold: float | None
    #: Kolik seancí práh počítalo (menší z obou řad).
    sample: int


@dataclass(frozen=True)
class MapState:
    thin: bool
    #: Podmínky: True/False = změřeno, None = bez dat (nezapočítává se).
    thin_gamma: bool | None
    weak_walls: bool | None
    fused: bool | None
    reasons: tuple[str, ...]
    gex_abs: float | None
    gamma_abs: float | None
    #: Rozpětí flip/zdi/max pain jako podíl ceny; None bez některé úrovně.
    spread_pct: float | None
    version: int = MAP_STATE_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "thin": self.thin,
            "thin_gamma": self.thin_gamma,
            "weak_walls": self.weak_walls,
            "fused": self.fused,
            "reasons": list(self.reasons),
            "gex_abs": self.gex_abs,
            "gamma_abs": self.gamma_abs,
            "spread_pct": self.spread_pct,
            "version": self.version,
        }


def percentile(values: list[float], share: float) -> float:
    """Percentil seřazené řady s lineární interpolací (share 0–1)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentil prázdné řady")
    position = share * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def history_thresholds(
    gex_abs_medians: list[float],
    gamma_abs_medians: list[float | None],
    *,
    min_sample: int = MIN_SAMPLE,
) -> MapHistory:
    """Prahy tenké gammy z mediánů předchozích seancí (jen řádky s hodnotou).

    Obě řady se hodnotí zvlášť: backfill z levels partic zná `total_gex`, ale
    gamma v místě ceny se historicky neukládala — ta řada se naplní až za běhu.
    """
    gex = [value for value in gex_abs_medians if value is not None]
    gamma = [value for value in gamma_abs_medians if value is not None]
    return MapHistory(
        gex_abs_threshold=percentile(gex, THIN_PERCENTILE) if len(gex) >= min_sample else None,
        gamma_abs_threshold=(
            percentile(gamma, THIN_PERCENTILE) if len(gamma) >= min_sample else None
        ),
        sample=min(len(gex), len(gamma)),
    )


def map_state(inputs: MapInputs, history: MapHistory | None) -> MapState:
    """Stav mapy pro jednu minutu: aspoň dvě ze tří podmínek = tenká mapa."""
    gex_abs = abs(inputs.total_gex) if inputs.total_gex is not None else None
    gamma_abs = abs(inputs.gamma_at_price) if inputs.gamma_at_price is not None else None

    # 1. Tenká gamma: celkové GEX i gamma v místě ceny pod percentilem historie.
    # Obě naráz — celkové GEX může být malé jen vyrušením dvou velkých stran,
    # zatímco v místě ceny gamma je.
    thin_gamma: bool | None = None
    if (
        history is not None
        and history.gex_abs_threshold is not None
        and history.gamma_abs_threshold is not None
        and gex_abs is not None
        and gamma_abs is not None
    ):
        thin_gamma = (
            gex_abs <= history.gex_abs_threshold and gamma_abs <= history.gamma_abs_threshold
        )

    # 2. Slabé zdi: obě dominance pod prahem (zeď bez dominance = zeď není).
    weak_walls: bool | None = None
    if inputs.call_wall_dom is not None or inputs.put_wall_dom is not None:
        weak_walls = all(
            dom is None or dom < WALL_WEAK_DOMINANCE
            for dom in (inputs.call_wall_dom, inputs.put_wall_dom)
        )

    # 3. Slitá mapa: flip, obě zdi a max pain do FUSED_MAP_PCT ceny od sebe.
    levels = [inputs.flip, inputs.call_wall, inputs.put_wall, inputs.max_pain]
    spread_pct: float | None = None
    fused: bool | None = None
    if all(level is not None for level in levels) and inputs.spot > 0:
        present = [level for level in levels if level is not None]
        spread_pct = (max(present) - min(present)) / inputs.spot
        fused = spread_pct <= FUSED_MAP_PCT

    reasons: list[str] = []
    if thin_gamma:
        reasons.append("tenká gamma (celkové GEX i gamma u ceny pod 25. percentilem historie)")
    if weak_walls:
        reasons.append("slabé zdi (dominance obou pod 25 %)")
    if fused:
        reasons.append(f"slitá mapa (flip, zdi a max pain do {spread_pct:.2%} ceny)")
    thin = sum(1 for flag in (thin_gamma, weak_walls, fused) if flag) >= THIN_CONDITIONS_MIN
    return MapState(
        thin=thin,
        thin_gamma=thin_gamma,
        weak_walls=weak_walls,
        fused=fused,
        reasons=tuple(reasons),
        gex_abs=gex_abs,
        gamma_abs=gamma_abs,
        spread_pct=spread_pct,
    )


@dataclass(frozen=True)
class SessionMapStats:
    """Řádek seance pro tabulku `map_state` — historie pro relativní prahy."""

    session_date: dt.date
    symbol: str
    gex_abs_median: float
    gex_abs_p10: float
    #: None u backfillu z partic (gamma u ceny se historicky neukládala).
    gamma_abs_median: float | None
    dom_median: float | None
    #: Podíl minut seance ve stavu tenká mapa; None bez vyhodnocení.
    thin_share: float | None
    sample_minutes: int
    version: int = MAP_STATE_VERSION


def session_stats(
    session_date: dt.date,
    symbol: str,
    gex_abs: list[float],
    gamma_abs: list[float],
    dominance: list[float],
    thin_flags: list[bool],
) -> SessionMapStats | None:
    """Agregát seance z minutových vzorků (jen minuty v US RTH); None bez vzorků."""
    if not gex_abs:
        return None
    return SessionMapStats(
        session_date=session_date,
        symbol=symbol,
        gex_abs_median=statistics.median(gex_abs),
        gex_abs_p10=percentile(gex_abs, 0.10),
        gamma_abs_median=statistics.median(gamma_abs) if gamma_abs else None,
        dom_median=statistics.median(dominance) if dominance else None,
        thin_share=(sum(1 for flag in thin_flags if flag) / len(thin_flags))
        if thin_flags
        else None,
        sample_minutes=len(gex_abs),
    )
