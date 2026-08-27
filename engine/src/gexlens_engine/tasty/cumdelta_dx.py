"""Stínové CumΔ z dxFeed TimeAndSale (#615 fáze 3, shadow — rozhodnutí 27. 8.).

Živé CumΔ (SPEC 4.5) se NEMĚNÍ a detektory z téhle řady nečtou nic. Modul
počítá paralelní minutovou řadu z TimeAndSale eventů (aggressorSide přímo od
burzy) nad aktivním řetězem, rozdělenou po zónách vlastnictví (ADR-0025):

- **hot zóna ATM±1**: vlastní IBKR tick-by-tick — tady se měří jen pro
  srovnání obou zdrojů nad týmž tokem,
- **prstenec ATM±15 mimo hot**: zóna, kterou by fáze 3 převzala místo
  midpoint testu — hlavní řada,
- **mimo ±15**: ignoruje se (vlastnictví se nemění).

Spread legy (schváleno 12. 8.): počítají se DÁL do hlavní řady (parita
s dnešním chováním), ale vede se i řada bez nich a měří se jejich podíl
na objemu. Trady bez určené strany se do toku nezapočítávají a počítají se
zvlášť — pokrytí aggressorSide rozhoduje, jestli midpoint fallback zůstává.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)

#: Zóny vlastnictví (ADR-0025 + SPEC R2): hot drží IBKR, prstenec by převzal dxFeed
HOT_ZONE_STRIKES = 1
RING_ZONE_STRIKES = 15

_AGGRESSOR_SIGN = {"BUY": 1, "SELL": -1}


@dataclass(frozen=True)
class DxFlowRow:
    """Minutový bod stínové řady — podklad pro srovnání a spread_leg verdikt."""

    ts_min: dt.datetime
    #: Prstenec ATM±15 mimo hot zónu — tok, který by fáze 3 převzala
    flow_ring: float
    cum_ring: float
    #: Táž zóna bez spread legů — čistý směrový záměr (outright)
    flow_ring_outright: float
    cum_ring_outright: float
    #: Hot zóna ATM±1 — srovnání dxFeed vs. IBKR tick-by-tick nad týmž tokem
    flow_hot: float
    cum_hot: float
    #: Počty za minutu (prstenec): celkem / spread legy / bez určené strany
    trades: int
    spread_trades: int
    unknown_side: int
    #: Σ size za minutu (prstenec) a z toho spread legy — podíl pro ADR
    volume: float
    spread_volume: float
    #: Trady zahozené kvůli chybějícímu spotu/deltě — díra v měření, ne v trhu
    dropped_no_context: int


class DxCumDeltaShadow:
    """Stavový akumulátor jedné pipeline (symbol); minutu uzavírá volající.

    `on_trade` běží synchronně v callbacku DxLinkStream — žádné zámky,
    stejná konvence jako TradesRecorder.
    """

    def __init__(self, multiplier: float) -> None:
        self._multiplier = multiplier
        #: streamer symbol → spec aktivního řetězu; plní denní obnova map
        self._by_streamer: dict[str, OptionContractSpec] = {}
        self._strikes: list[float] = []
        self._spot: float | None = None
        self._session_date: dt.date | None = None
        # Akumulátory minuty
        self._flow_ring = 0.0
        self._flow_ring_outright = 0.0
        self._flow_hot = 0.0
        self._trades = 0
        self._spread_trades = 0
        self._unknown_side = 0
        self._volume = 0.0
        self._spread_volume = 0.0
        self._dropped = 0
        # Kumulativy dne
        self._cum_ring = 0.0
        self._cum_ring_outright = 0.0
        self._cum_hot = 0.0
        # Denní součty pro /status (podíl spread legů a pokrytí strany)
        self._day_trades = 0
        self._day_spread_trades = 0
        self._day_unknown = 0
        self._day_volume = 0.0
        self._day_spread_volume = 0.0
        self._day_dropped = 0

    def set_universe(self, by_streamer: dict[str, OptionContractSpec]) -> None:
        """Aktivní řetěz: mapa streamer → spec; strikes se odvodí z ní."""
        self._by_streamer = dict(by_streamer)
        self._strikes = sorted({spec.strike for spec in by_streamer.values()})

    def set_spot(self, spot: float | None) -> None:
        """Referenční spot pro určení ATM — hranice zón se přepočítá dalším tradem."""
        self._spot = spot

    def roll_session(self, session_date: dt.date) -> bool:
        """Nový obchodní den → reset kumulativ (stejně jako živé CumΔ)."""
        if self._session_date == session_date:
            return False
        self._session_date = session_date
        self._cum_ring = 0.0
        self._cum_ring_outright = 0.0
        self._cum_hot = 0.0
        self._day_trades = 0
        self._day_spread_trades = 0
        self._day_unknown = 0
        self._day_volume = 0.0
        self._day_spread_volume = 0.0
        self._day_dropped = 0
        return True

    def day_stats(self) -> dict[str, float]:
        """Denní souhrn pro /status: podíly rozhodující o ADR (spread) a fallbacku."""
        return {
            "cum_ring": self._cum_ring,
            "cum_ring_outright": self._cum_ring_outright,
            "cum_hot": self._cum_hot,
            "trades": float(self._day_trades),
            "spread_volume_share": (
                self._day_spread_volume / self._day_volume if self._day_volume > 0 else 0.0
            ),
            "unknown_side_share": (
                self._day_unknown / self._day_trades if self._day_trades > 0 else 0.0
            ),
            "dropped_no_context": float(self._day_dropped),
        }

    def _zone(self, strike: float) -> str | None:
        """'hot' / 'ring' / None podle vzdálenosti v pořadí striků od ATM."""
        if self._spot is None or not self._strikes:
            return None
        atm_index = min(
            range(len(self._strikes)),
            key=lambda index: abs(self._strikes[index] - self._spot),  # type: ignore[operator]
        )
        try:
            strike_index = self._strikes.index(strike)
        except ValueError:
            return None
        distance = abs(strike_index - atm_index)
        if distance <= HOT_ZONE_STRIKES:
            return "hot"
        if distance <= RING_ZONE_STRIKES:
            return "ring"
        return None

    def on_trade(
        self,
        streamer_symbol: str,
        size: float | None,
        aggressor: str | None,
        spread_leg: bool | None,
        delta: float | None,
    ) -> None:
        """Jeden TimeAndSale print; delta dodává volající z tasty Greeks cache."""
        spec = self._by_streamer.get(streamer_symbol)
        if spec is None or size is None or size <= 0:
            return
        zone = self._zone(spec.strike)
        if zone is None:
            # Mimo ±15, nebo bez spotu/strike v žebříku — mimo měření
            if self._spot is None:
                self._dropped += 1
                self._day_dropped += 1
            return
        if delta is None:
            self._dropped += 1
            self._day_dropped += 1
            return
        sign = _AGGRESSOR_SIGN.get(aggressor.upper() if isinstance(aggressor, str) else "", 0)
        if zone == "ring":
            self._trades += 1
            self._day_trades += 1
            self._volume += size
            self._day_volume += size
            if spread_leg:
                self._spread_trades += 1
                self._day_spread_trades += 1
                self._spread_volume += size
                self._day_spread_volume += size
            if sign == 0:
                self._unknown_side += 1
                self._day_unknown += 1
                return
            flow = sign * size * delta * self._multiplier
            self._flow_ring += flow
            if not spread_leg:
                self._flow_ring_outright += flow
        elif sign != 0:
            self._flow_hot += sign * size * delta * self._multiplier

    def close_minute(self, ts_min: dt.datetime) -> DxFlowRow:
        """Uzavře minutu: kumulativy + reset minutových akumulátorů."""
        self._cum_ring += self._flow_ring
        self._cum_ring_outright += self._flow_ring_outright
        self._cum_hot += self._flow_hot
        row = DxFlowRow(
            ts_min=ts_min,
            flow_ring=self._flow_ring,
            cum_ring=self._cum_ring,
            flow_ring_outright=self._flow_ring_outright,
            cum_ring_outright=self._cum_ring_outright,
            flow_hot=self._flow_hot,
            cum_hot=self._cum_hot,
            trades=self._trades,
            spread_trades=self._spread_trades,
            unknown_side=self._unknown_side,
            volume=self._volume,
            spread_volume=self._spread_volume,
            dropped_no_context=self._dropped,
        )
        self._flow_ring = 0.0
        self._flow_ring_outright = 0.0
        self._flow_hot = 0.0
        self._trades = 0
        self._spread_trades = 0
        self._unknown_side = 0
        self._volume = 0.0
        self._spread_volume = 0.0
        self._dropped = 0
        return row
