"""Cum Δ — kumulativní delta flow s plnou klasifikací agresora (SPEC 4.5 + R2).

Dvě větve:
- **hot zóna (tick-by-tick)**: každý trade už nese Lee–Ready klasifikaci
  z HotZoneCollectoru → flowΔ = sign · size · Δ(K,s) · M; trades bez
  klasifikace (unknown) se nezapočítávají.
- **zbytek řetězce (1min)**: ΔVol = přírůstek kumulativního volume za minutu,
  znaménko z midpoint testu posledního last vs. aktuální bid/ask
  → flowΔ = sign · ΔVol · Δ(K,s) · M.

Δ(K,s) se bere z posledního platného Greeks snapshotu kontraktu (dodává volající).
CumΔ se resetuje na začátku obchodního dne (session start řídí engine).
"""

import datetime as dt
import logging
from dataclasses import dataclass

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.hotzone import ClassifiedTrade, TradeSide

logger = logging.getLogger(__name__)

_TRADE_SIGN = {TradeSide.BUY: 1, TradeSide.SELL: -1, TradeSide.UNKNOWN: 0}


@dataclass(frozen=True)
class FlowRow:
    """Minutový bod řady pro panel Cum Δ a persistenci do derived/."""

    ts_min: dt.datetime
    flow_delta: float
    cum_delta: float


def midpoint_sign(last: float, bid: float, ask: float) -> int:
    """Midpoint test (SPEC 4.5): last nad midem → +1, pod → −1, přesně na midu → 0."""
    mid = (bid + ask) / 2.0
    if last > mid:
        return 1
    if last < mid:
        return -1
    return 0


class CumDeltaTracker:
    """Denní agregátor flowΔ/CumΔ přes obě větve klasifikace."""

    def __init__(self, multiplier: float) -> None:
        self._multiplier = multiplier
        self._cum = 0.0
        self._minute_flow = 0.0
        self._last_volume: dict[OptionContractSpec, float] = {}
        # Čistý klasifikovaný objem per kontrakt (buy − sell, v kontraktech) —
        # vstup flow-adjusted OI odhadu (ADR-0011, #222)
        self._net_volume: dict[OptionContractSpec, float] = {}
        # Obchodní den (Globex seance), ke kterému kumulativ patří (#638)
        self._session_date: dt.date | None = None

    @property
    def cum_delta(self) -> float:
        return self._cum

    def net_volume(self, spec: OptionContractSpec) -> float:
        """Denní čistý klasifikovaný objem kontraktu (buy − sell; ADR-0011)."""
        return self._net_volume.get(spec, 0.0)

    def net_volumes(self) -> dict[OptionContractSpec, float]:
        """Kopie celé mapy čistého objemu — persistence řady netflow (#232)."""
        return dict(self._net_volume)

    def restore_net_volume(self, values: dict[OptionContractSpec, float]) -> None:
        """Naváže kumulativ z partice netflow po restartu uprostřed dne (#232).

        Jen chybějící klíče (setdefault): tok naměřený PO restartu má přednost
        a uložený kumulativ ho nesmí přepsat.
        """
        for spec, net in values.items():
            self._net_volume.setdefault(spec, net)

    def reset(self) -> None:
        """Reset na začátku obchodního dne (SPEC 4.5; volá `roll_session`, #638)."""
        self._cum = 0.0
        self._minute_flow = 0.0
        self._last_volume.clear()
        self._net_volume.clear()

    def roll_session(self, session_date: dt.date) -> bool:
        """Reset kumulativů na hranici Globex seance (#638, SPEC 4.5).

        Vrací True, když právě proběhl reset (přechod na nový obchodní den).
        První volání po startu procesu jen zafixuje seanci BEZ resetu —
        restart uprostřed seance nesmí zahodit dopolední tok; navázání
        z partic (flow/netflow seed) řeší runtime.

        Kotva obou kumulativů je open seance (17:00 CT): CumΔ tím sedí na osu
        obchodního dne (#512) a net objem na ranní OI archiv, který odráží
        pozice k předchozímu settle — tok od open je přesně to, co v něm není.
        """
        if self._session_date == session_date:
            return False
        first = self._session_date is None
        self._session_date = session_date
        if first:
            return False
        self.reset()
        return True

    def restore_cum(self, base: float) -> None:
        """Naváže CumΔ z flow partice po restartu uprostřed seance (#638).

        Přičítá základ k dosavadnímu (typicky nulovému) kumulativu — tok
        naměřený po restartu se tím neztrácí. Volat nejvýš jednou; hlídá
        runtime (pending flag), ne tracker.
        """
        self._cum += base

    def add_trade(self, trade: ClassifiedTrade, delta: float) -> float:
        """Hot zóna: flowΔ = sign · size · Δ · M; unknown klasifikace nepřispívá."""
        sign = _TRADE_SIGN[trade.side]
        flow = sign * trade.size * delta * self._multiplier
        self._net_volume[trade.spec] = self._net_volume.get(trade.spec, 0.0) + sign * trade.size
        self._apply(flow)
        return flow

    def add_bar(
        self,
        spec: OptionContractSpec,
        cumulative_volume: float,
        last: float,
        bid: float,
        ask: float,
        delta: float,
    ) -> float:
        """Zbytek řetězce: ΔVol za minutu × midpoint test × Δ × M.

        První bar dne přírůstek nemá (jen založí stav); pokles kumulativního
        volume je nekonzistence feedu → přírůstek 0 s varováním, nikdy záporný.
        """
        previous = self._last_volume.get(spec)
        self._last_volume[spec] = cumulative_volume
        if previous is None:
            return 0.0
        delta_volume = cumulative_volume - previous
        if delta_volume < 0.0:
            logger.warning(
                "Kumulativní volume kleslo (%s: %.0f → %.0f) — přírůstek ignoruji",
                spec,
                previous,
                cumulative_volume,
            )
            return 0.0
        sign = midpoint_sign(last, bid, ask)
        flow = sign * delta_volume * delta * self._multiplier
        self._net_volume[spec] = self._net_volume.get(spec, 0.0) + sign * delta_volume
        self._apply(flow)
        return flow

    def close_minute(self, ts_min: dt.datetime) -> FlowRow:
        """Uzavře minutu: vrátí bod řady (flowΔ minuty, průběžná CumΔ) a vynuluje minutu."""
        row = FlowRow(ts_min=ts_min, flow_delta=self._minute_flow, cum_delta=self._cum)
        self._minute_flow = 0.0
        return row

    def _apply(self, flow: float) -> None:
        self._cum += flow
        self._minute_flow += flow
