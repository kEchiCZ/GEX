"""Cum Δ — kumulativní delta flow s plnou klasifikací agresora (SPEC 4.5 + R2).

Dvě větve:
- **trade větev**: každý trade nese stranu agresora → flowΔ = sign · size ·
  Δ(K,s) · M; trades bez určené strany (unknown) se nezapočítávají. Zdrojem
  je dxFeed `TimeAndSale` pro celý sbíraný řetěz (ADR-0032, #615 fáze 3); IBKR
  tick-by-tick zóna z původního návrhu SPEC 3.4 se nikdy nenapojila a byla
  rozhodnutím 3. 9. 2026 nahrazena (#1006).
- **zbytek řetězce (1min)**: ΔVol = přírůstek kumulativního volume za minutu,
  znaménko z midpoint testu posledního last vs. aktuální bid/ask
  → flowΔ = sign · ΔVol · Δ(K,s) · M. Zároveň fallback pro provoz bez
  tastytrade větve.

Δ(K,s) se bere z posledního platného Greeks snapshotu kontraktu (dodává volající).
CumΔ se resetuje na začátku obchodního dne (session start řídí engine).
"""

import datetime as dt
import enum
import logging
from dataclasses import dataclass

from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)


class TradeSide(enum.Enum):
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedTrade:
    """Trade s určenou stranou agresora (vstup trade větve CumΔ)."""

    spec: OptionContractSpec
    price: float
    size: float
    ts: float
    side: TradeSide


_TRADE_SIGN = {TradeSide.BUY: 1, TradeSide.SELL: -1, TradeSide.UNKNOWN: 0}
#: dxFeed `aggressorSide` (CME tag 5797) → znaménko; cokoli jiného = bez strany
_AGGRESSOR_SIGN = {"BUY": 1, "SELL": -1}
#: Zdroje znaménka toku (ADR-0032): `midpoint` = dnešní minutový test pro celý
#: řetěz, `dxfeed` = tisky TimeAndSale se stranou od burzy, midpoint jen fallback
CUMDELTA_SOURCES = ("midpoint", "dxfeed")


@dataclass(frozen=True)
class BarBreakdown:
    """Rozklad přírůstku objemu kontraktu za jeden bar (#1007 krok 2).

    `printed` = Σ tisků TimeAndSale (se stranou i bez) od minulého baru,
    `structured` = přírůstek bez tisku (nohy spreadů, bloky — CME je jako
    trade nevysílá). Obojí `None`, když trade větev pro kontrakt neběžela
    (tastytrade odpojené, symbol bez univerza) — nula by lhala „100 %
    struktura".
    """

    volume_delta: float
    printed: float | None
    structured: float | None


@dataclass
class CumDeltaCoverage:
    """Denní pokrytí toku podle zdroje (#615 fáze 3) — do /status a pro srovnání.

    Objemy v kontraktech: `printed` = tisky se stranou od burzy, `unknown` =
    tisky bez strany (znaménko dodá midpoint), `structured` = přírůstek objemu
    bez tisku (nohy spreadů, bloky — CME je jako trade nevysílá, ADR-0027),
    `fallback` = kontrakt-minuty bez jediného tisku, klasifikované midpointem.
    """

    printed_volume: float = 0.0
    unknown_volume: float = 0.0
    structured_volume: float = 0.0
    fallback_volume: float = 0.0
    dropped_no_delta: int = 0

    def as_dict(self) -> dict[str, float]:
        classified = self.printed_volume + self.unknown_volume + self.fallback_volume
        return {
            "printed_volume": self.printed_volume,
            "unknown_volume": self.unknown_volume,
            "structured_volume": self.structured_volume,
            "fallback_volume": self.fallback_volume,
            "dropped_no_delta": float(self.dropped_no_delta),
            # podíl klasifikovaného objemu se stranou od burzy
            "printed_share": self.printed_volume / classified if classified > 0 else 0.0,
        }


@dataclass(frozen=True)
class FlowRow:
    """Minutový bod řady pro panel Cum Δ a persistenci do derived/."""

    ts_min: dt.datetime
    flow_delta: float
    cum_delta: float
    # CVD podkladu (#829) — druhá řada panelu, plní ji FuturesCvdTracker.
    # None = bez tasty větve nebo instrument nemá registrovaný front future.
    futures_cvd_delta: float | None = None
    futures_cvd: float | None = None
    # Zdroj znaménka (ADR-0032): partice před fází 3 mají NULL = midpoint.
    # Srovnání řad před/po přepnutí stojí na tomhle sloupci, ne na datu.
    source: str | None = None


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

    def __init__(self, multiplier: float, source: str = "midpoint") -> None:
        if source not in CUMDELTA_SOURCES:
            raise ValueError(f"Neznámý zdroj CumΔ {source!r} (povolené: {CUMDELTA_SOURCES})")
        self._multiplier = multiplier
        self._source = source
        self._cum = 0.0
        self._minute_flow = 0.0
        self._last_volume: dict[OptionContractSpec, float] = {}
        # Tisky od posledního baru kontraktu (#615 fáze 3): bar větev z nich
        # pozná, kolik přírůstku objemu už má znaménko od burzy
        self._printed_since_bar: dict[OptionContractSpec, float] = {}
        self._unknown_since_bar: dict[OptionContractSpec, float] = {}
        self._coverage = CumDeltaCoverage()
        #: Běží trade větev pro tento instrument? Nastavuje orchestrátor každý
        #: cyklus (tasty připojené + univerzum); bez toho je rozklad NULL.
        self.dx_active: bool = False
        self._breakdowns: dict[OptionContractSpec, BarBreakdown] = {}
        # Čistý klasifikovaný objem per kontrakt (buy − sell, v kontraktech) —
        # vstup flow-adjusted OI odhadu (ADR-0011, #222)
        self._net_volume: dict[OptionContractSpec, float] = {}
        # Obchodní den (Globex seance), ke kterému kumulativ patří (#638)
        self._session_date: dt.date | None = None

    @property
    def cum_delta(self) -> float:
        return self._cum

    @property
    def source(self) -> str:
        return self._source

    def day_stats(self) -> dict[str, float | str]:
        """Denní pokrytí toku podle zdroje — /status a Settings (#615 krok 5)."""
        return {"source": self._source, **self._coverage.as_dict()}

    def take_breakdowns(self) -> dict[OptionContractSpec, BarBreakdown]:
        """Rozklady přírůstků od posledního odběru (řada printvol, #1007) — a vyprázdní je."""
        taken = self._breakdowns
        self._breakdowns = {}
        return taken

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
        self._printed_since_bar.clear()
        self._unknown_since_bar.clear()
        self._coverage = CumDeltaCoverage()
        self._breakdowns.clear()

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

    def add_dx_trade(
        self,
        spec: OptionContractSpec,
        size: float | None,
        aggressor: str | None,
        delta: float | None,
    ) -> float:
        """Jeden tisk dxFeed `TimeAndSale` (ADR-0032, #615 fáze 3).

        V režimu `dxfeed` přispívá do toku hned: flowΔ = sign · size · Δ · M.
        V režimu `midpoint` se jen měří pokrytí (paralelní běh před přepnutím).
        Tisk bez delty se nepočítá vůbec — jeho objem doklasifikuje bar větev
        midpointem, jako by tisk nepřišel. Tisk bez strany (`UNDEFINED`,
        ~0,05 %) se eviduje zvlášť: znaménko mu dá až midpoint v bar větvi.
        """
        if size is None or size <= 0:
            return 0.0
        if delta is None:
            self._coverage.dropped_no_delta += 1
            return 0.0
        sign = _AGGRESSOR_SIGN.get(aggressor.upper() if isinstance(aggressor, str) else "", 0)
        if sign == 0:
            self._unknown_since_bar[spec] = self._unknown_since_bar.get(spec, 0.0) + size
            self._coverage.unknown_volume += size
            return 0.0
        self._printed_since_bar[spec] = self._printed_since_bar.get(spec, 0.0) + size
        self._coverage.printed_volume += size
        if self._source != "dxfeed":
            return 0.0
        flow = sign * size * delta * self._multiplier
        self._net_volume[spec] = self._net_volume.get(spec, 0.0) + sign * size
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
        """Bar větev: přírůstek kumulativního volume × midpoint test × Δ × M.

        První bar dne přírůstek nemá (jen založí stav); pokles kumulativního
        volume je nekonzistence feedu → přírůstek 0 s varováním, nikdy záporný.

        V režimu `dxfeed` (ADR-0032) je bar větev jen fallback per kontrakt:
        - přišly tisky se stranou → ty už jsou v toku; midpoint dostane jen
          tisky bez strany a zbytek přírůstku (nohy spreadů, bloky — CME je
          jako trade nevysílá) zůstává **mimo tok** jako strukturovaný objem;
        - tisk nepřišel žádný → celý přírůstek midpointem (výpadek tasty,
          kontrakt mimo tasty řetěz) a počítá se jako fallback.
        """
        previous = self._last_volume.get(spec)
        self._last_volume[spec] = cumulative_volume
        printed = self._printed_since_bar.pop(spec, 0.0)
        unknown = self._unknown_since_bar.pop(spec, 0.0)
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
        if delta_volume > 0.0:
            printed_total = printed + unknown
            self._breakdowns[spec] = BarBreakdown(
                volume_delta=delta_volume,
                printed=printed_total if self.dx_active else None,
                structured=max(delta_volume - printed_total, 0.0) if self.dx_active else None,
            )
        if self._source == "dxfeed" and printed > 0.0:
            volume_to_sign = min(unknown, delta_volume)
            self._coverage.structured_volume += max(delta_volume - printed - unknown, 0.0)
        else:
            volume_to_sign = delta_volume
            if self._source == "dxfeed":
                self._coverage.fallback_volume += delta_volume
        if volume_to_sign <= 0.0:
            return 0.0
        sign = midpoint_sign(last, bid, ask)
        flow = sign * volume_to_sign * delta * self._multiplier
        self._net_volume[spec] = self._net_volume.get(spec, 0.0) + sign * volume_to_sign
        self._apply(flow)
        return flow

    def close_minute(self, ts_min: dt.datetime) -> FlowRow:
        """Uzavře minutu: vrátí bod řady (flowΔ minuty, průběžná CumΔ) a vynuluje minutu."""
        row = FlowRow(
            ts_min=ts_min, flow_delta=self._minute_flow, cum_delta=self._cum, source=self._source
        )
        self._minute_flow = 0.0
        return row

    def _apply(self, flow: float) -> None:
        self._cum += flow
        self._minute_flow += flow
