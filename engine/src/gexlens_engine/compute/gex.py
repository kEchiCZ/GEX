"""GEX výpočty (SPEC 4.1): GEX_1pt/GEX_1pct per strike a strana, NetGEX, TotalGEX.

Znaménkový model dealera je vyměnitelná strategie (strategy pattern) — výchozí
naivní model předpokládá dealery long call gamma / short put gamma
(NetGEX = GEX_C − GEX_P). Budoucí flow-based odhad implementuje stejné
rozhraní a API výpočtu se nemění (R6/issue #13 AC).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GexInput:
    """Vstup výpočtu pro jeden kontrakt: Γ a OI z posledního snapshotu."""

    strike: float
    right: str  # C | P
    gamma: float
    oi: float


@dataclass(frozen=True)
class StrikeGex:
    """GEX hodnoty jednoho striku; call/put složky bez znaménka, net dle modelu."""

    strike: float
    call_gex_1pt: float
    put_gex_1pt: float
    net_gex_1pt: float
    net_gex_1pct: float


@dataclass(frozen=True)
class GexResult:
    per_strike: tuple[StrikeGex, ...]  # seřazeno podle striku
    total_gex_1pt: float
    total_gex_1pct: float

    def net_by_strike(self) -> dict[float, float]:
        """NetGEX_1pt per strike — vstup pro levels a walls (SPEC 4.2, 4.4)."""
        return {row.strike: row.net_gex_1pt for row in self.per_strike}


class SignModelLike(Protocol):
    """Znaménkový model dealera: znaménko příspěvku kontraktu do NetGEX."""

    def sign(self, option: GexInput) -> float: ...


class NaiveDealerModel:
    """Výchozí model (SPEC 4.1): dealeři long call gamma (+1), short put gamma (−1)."""

    def sign(self, option: GexInput) -> float:
        return 1.0 if option.right == "C" else -1.0


def gex_1pt(gamma: float, oi: float, multiplier: float) -> float:
    """GEX_1pt = Γ · OI · M  [$ delta-hedge / 1 bod pohybu podkladu]."""
    return gamma * oi * multiplier


def gex_1pct(gamma: float, oi: float, multiplier: float, spot: float) -> float:
    """GEX_1pct = Γ · OI · M · S² · 0.01  [$ / 1 % pohyb podkladu]."""
    return gamma * oi * multiplier * spot**2 * 0.01


class GexEngine:
    """Agregace GEX přes řetězec s vyměnitelným znaménkovým modelem."""

    def __init__(self, sign_model: SignModelLike | None = None) -> None:
        self._sign_model = sign_model if sign_model is not None else NaiveDealerModel()

    def compute(self, options: Sequence[GexInput], spot: float, multiplier: float) -> GexResult:
        calls: dict[float, float] = {}
        puts: dict[float, float] = {}
        nets: dict[float, float] = {}
        for option in options:
            if option.right not in ("C", "P"):
                raise ValueError(f"Neplatná strana opce: {option.right!r} (očekávám C/P)")
            value_1pt = gex_1pt(option.gamma, option.oi, multiplier)
            side = calls if option.right == "C" else puts
            side[option.strike] = side.get(option.strike, 0.0) + value_1pt
            signed = self._sign_model.sign(option) * value_1pt
            nets[option.strike] = nets.get(option.strike, 0.0) + signed

        pct_factor = spot**2 * 0.01
        per_strike = tuple(
            StrikeGex(
                strike=strike,
                call_gex_1pt=calls.get(strike, 0.0),
                put_gex_1pt=puts.get(strike, 0.0),
                net_gex_1pt=nets[strike],
                net_gex_1pct=nets[strike] * pct_factor,
            )
            for strike in sorted(nets)
        )
        total_1pt = sum(row.net_gex_1pt for row in per_strike)
        return GexResult(
            per_strike=per_strike,
            total_gex_1pt=total_1pt,
            total_gex_1pct=total_1pt * pct_factor,
        )
