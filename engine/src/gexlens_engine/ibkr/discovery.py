"""ChainDiscovery (SPEC 3.2): enumerace opčních řetězců, expirací a pásma strikes.

Discovery je čistě popisná vrstva — vrací specifikace kontraktů; kvalifikaci
(conId) a subskripce řeší SubscriptionScheduler (issue #7). Politika
auto-rozšíření pásma je zdůvodněna v docs/adr/0002-strike-band-expansion.md.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from gexlens_engine.config import Settings

# Mapování secType podkladu → secType opce (SPEC 3.2: ES futures → FOP, akcie/indexy → OPT)
_OPTION_SEC_TYPE = {"FUT": "FOP", "STK": "OPT", "IND": "OPT"}

# Růst pásma při auto-rozšíření (ADR-0002)
EXPANSION_GROWTH = 1.5


@dataclass(frozen=True)
class Underlying:
    """Podkladový instrument, nad kterým se hledají opční řetězce."""

    symbol: str
    sec_type: str  # FUT | STK | IND
    exchange: str  # CME pro ES futures, SMART pro akcie/indexy
    con_id: int


@dataclass(frozen=True)
class ExpiryInfo:
    """Jedna kombinace tradingClass × expirace (položka selektoru expirace v UI)."""

    trading_class: str
    expiry: str  # YYYYMMDD
    exchange: str
    multiplier: str
    strikes: tuple[float, ...]


@dataclass(frozen=True)
class StrikeBand:
    """Pásmo strikes ±range_points bodů od centru (SPEC 3.2)."""

    strikes: tuple[float, ...]
    center: float
    range_points: float

    @property
    def low(self) -> float:
        return self.center - self.range_points

    @property
    def high(self) -> float:
        return self.center + self.range_points


@dataclass(frozen=True)
class OptionContractSpec:
    """Specifikace jednoho opčního kontraktu pásma (vstup pro kvalifikaci a subskripce)."""

    symbol: str
    sec_type: str  # FOP | OPT
    expiry: str
    strike: float
    right: str  # C | P
    exchange: str
    trading_class: str
    multiplier: str


class OptionChainLike(Protocol):
    """Strukturální podoba ib_async.OptionChain (atributy záměrně v camelCase).

    Členy jsou read-only properties, aby protokol splnily i dataclassy
    s konkrétnějšími typy kolekcí (list vs. Collection).
    """

    @property
    def exchange(self) -> str: ...

    @property
    def tradingClass(self) -> str: ...

    @property
    def multiplier(self) -> str: ...

    @property
    def expirations(self) -> Collection[str]: ...

    @property
    def strikes(self) -> Collection[float]: ...


class ChainClientLike(Protocol):
    """Minimální rozhraní klienta pro discovery (mock: gexlens_engine.ibkr.mock.MockIB)."""

    async def reqSecDefOptParamsAsync(
        self,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> Sequence[OptionChainLike]: ...


def option_sec_type(underlying_sec_type: str) -> str:
    try:
        return _OPTION_SEC_TYPE[underlying_sec_type]
    except KeyError as exc:
        raise ValueError(
            f"Nepodporovaný secType podkladu: {underlying_sec_type!r} (očekávám FUT/STK/IND)"
        ) from exc


def select_band(strikes: Collection[float], spot: float, range_points: float) -> StrikeBand:
    """Vybere strikes v pásmu spot ± range_points, centrované na aktuální spot."""
    chosen = tuple(k for k in sorted(strikes) if spot - range_points <= k <= spot + range_points)
    return StrikeBand(strikes=chosen, center=spot, range_points=range_points)


def should_expand(band: StrikeBand, spot: float, threshold: float) -> bool:
    """True, pokud se spot přiblížil k okraji pásma na méně než threshold × šířka pásma."""
    width = band.high - band.low
    return (spot - band.low) < threshold * width or (band.high - spot) < threshold * width


def build_contracts(
    underlying: Underlying, info: ExpiryInfo, band: StrikeBand
) -> list[OptionContractSpec]:
    """Kompletní seznam kontraktů pásma: každý strike × C/P."""
    sec_type = option_sec_type(underlying.sec_type)
    return [
        OptionContractSpec(
            symbol=underlying.symbol,
            sec_type=sec_type,
            expiry=info.expiry,
            strike=strike,
            right=right,
            exchange=info.exchange,
            trading_class=info.trading_class,
            multiplier=info.multiplier,
        )
        for strike in band.strikes
        for right in ("C", "P")
    ]


class ChainDiscovery:
    """Enumerace řetězců přes reqSecDefOptParams + správa pásma strikes."""

    def __init__(self, client: ChainClientLike, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def discover(self, underlying: Underlying) -> list[ExpiryInfo]:
        """Vrátí všechny dvojice tradingClass × expirace, seřazené podle expirace.

        FOP se filtruje na burzu podkladu (CME), OPT na agregát SMART (SPEC 3.2).
        """
        fut_fop_exchange = underlying.exchange if underlying.sec_type == "FUT" else ""
        chains = await self._client.reqSecDefOptParamsAsync(
            underlying.symbol,
            fut_fop_exchange,
            underlying.sec_type,
            underlying.con_id,
        )
        wanted_exchange = underlying.exchange if underlying.sec_type == "FUT" else "SMART"
        infos: list[ExpiryInfo] = []
        for chain in chains:
            if chain.exchange != wanted_exchange:
                continue
            strikes = tuple(sorted(chain.strikes))
            for expiry in sorted(chain.expirations):
                infos.append(
                    ExpiryInfo(
                        trading_class=chain.tradingClass,
                        expiry=expiry,
                        exchange=chain.exchange,
                        multiplier=chain.multiplier,
                        strikes=strikes,
                    )
                )
        infos.sort(key=lambda info: (info.expiry, info.trading_class))
        return infos

    def initial_band(self, info: ExpiryInfo, spot: float) -> StrikeBand:
        return select_band(info.strikes, spot, self._settings.strike_range_points)

    def maybe_expand(self, info: ExpiryInfo, band: StrikeBand, spot: float) -> StrikeBand:
        """Auto-rozšíření pásma (SPEC 3.2, ADR-0002).

        Když se spot přiblíží k okraji na < threshold šířky pásma, pásmo se
        recentruje na aktuální spot a rozšíří o EXPANSION_GROWTH.
        """
        if should_expand(band, spot, self._settings.strike_range_expand_threshold):
            return select_band(info.strikes, spot, band.range_points * EXPANSION_GROWTH)
        return band
