"""ChainDiscovery (SPEC 3.2): enumerace opčních řetězců, expirací a pásma strikes.

Discovery je čistě popisná vrstva — vrací specifikace kontraktů; kvalifikaci
(conId) a subskripce řeší SubscriptionScheduler (issue #7). Politika
auto-rozšíření pásma je zdůvodněna v docs/adr/0002-strike-band-expansion.md.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from gexlens_engine.config import Settings

logger = logging.getLogger(__name__)

# Mapování secType podkladu → secType opce (SPEC 3.2: ES futures → FOP, akcie/indexy → OPT)
_OPTION_SEC_TYPE = {"FUT": "FOP", "STK": "OPT", "IND": "OPT"}


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
    """Pásmo strikes jako denní obálka [low, high] (SPEC 3.2, ADR-0002).

    Obálka se během dne nikdy nezužuje ani neposouvá — jen roste; reset na
    spot ± strike_range_points dělá volající na začátku obchodního dne.
    """

    strikes: tuple[float, ...]
    low: float
    high: float

    @property
    def width(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class BandExpansion:
    """Výsledek auto-rozšíření: nová obálka + co se stalo (pro UI/alerty)."""

    band: StrikeBand
    expanded: bool
    # Strop šířky dosažen — obálka se posouvá za spotem a vzdálený okraj se
    # obětuje; jediný moment, kdy se křídlo ztrácí (alert do UI)
    capped: bool


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


def band_between(strikes: Collection[float], low: float, high: float) -> StrikeBand:
    """Obálka [low, high] se strikes, které do ní padají."""
    chosen = tuple(k for k in sorted(strikes) if low <= k <= high)
    return StrikeBand(strikes=chosen, low=low, high=high)


def select_band(strikes: Collection[float], spot: float, range_points: float) -> StrikeBand:
    """Výchozí denní obálka: spot ± range_points (reset na začátku obchodního dne)."""
    return band_between(strikes, spot - range_points, spot + range_points)


def should_expand(band: StrikeBand, spot: float, threshold: float) -> bool:
    """True, pokud se spot přiblížil k okraji obálky na méně než threshold × šířka."""
    return (spot - band.low) < threshold * band.width or (band.high - spot) < threshold * band.width


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

    def maybe_expand(self, info: ExpiryInfo, band: StrikeBand, spot: float) -> BandExpansion:
        """Auto-rozšíření obálky (SPEC 3.2, ADR-0002: grow-only).

        Když se spot přiblíží k okraji na < threshold šířky, prodlouží se JEN
        ten okraj, ke kterému se blíží (na spot ± strike_range_points) — druhá
        strana zůstává a křídla se neztrácejí. Šířku omezuje
        strike_range_max_points: při dosažení stropu se obálka posouvá za
        spotem a vzdálený okraj se obětuje (capped=True → alert do UI).
        """
        if not should_expand(band, spot, self._settings.strike_range_expand_threshold):
            return BandExpansion(band=band, expanded=False, capped=False)

        reach = self._settings.strike_range_points
        low = min(band.low, spot - reach)
        high = max(band.high, spot + reach)

        capped = False
        max_width = self._settings.strike_range_max_points
        if high - low > max_width:
            capped = True
            # Posouvá se jen aktivní okraj — druhý se dotáhne na strop šířky
            if spot + reach > band.high:
                low = high - max_width
            else:
                high = low + max_width
            logger.warning(
                "Obálka strikes dosáhla stropu %g b — posouvám a obětuji vzdálený okraj "
                "(nová obálka %g–%g)",
                max_width,
                low,
                high,
            )

        new_band = band_between(info.strikes, low, high)
        return BandExpansion(band=new_band, expanded=True, capped=capped)
