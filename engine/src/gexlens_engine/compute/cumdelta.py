"""Cum Δ — kumulativní delta flow s klasifikací agresora (SPEC 4.5 + R2, ADR-0057).

Zdroj znaménka toku a čistého objemu určuje mechanika setupů (#1460):
- **`aggressor`** (od mechaniky v6, ADR-0057): jen tisky dxFeed `TimeAndSale`
  se stranou od burzy → flowΔ = sign · size · Δ(K,s) · M, net += sign · size.
  Bar větev (přírůstek kumulativního volume ze snímku) znaménko nedává, jen
  rozkládá objem na tisky a zbytek (`printvol`, pokrytí); objem bez tisku,
  tisk bez strany a tisk bez delty jdou mimo tok.
- **`midpoint`** (mechanika v5): ΔVol = přírůstek kumulativního volume za
  minutu, znaménko z midpoint testu posledního last vs. aktuální bid/ask
  → flowΔ = sign · ΔVol · Δ(K,s) · M. Tisky jen měří pokrytí (#1071).

Δ(K,s) se bere z posledního platného Greeks snapshotu kontraktu (dodává volající).
CumΔ se resetuje na začátku obchodního dne (session start řídí engine).
"""

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from gexlens_engine.compute.settle import session_bounds, trading_session_date
from gexlens_engine.compute.setups import CUMDELTA_PRINTS
from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)


#: dxFeed `aggressorSide` (CME tag 5797) → znaménko; cokoli jiného = bez strany
_AGGRESSOR_SIGN = {"BUY": 1, "SELL": -1}
#: Zdroje znaménka toku: `midpoint` = minutový test pro celý řetěz (v5),
#: `aggressor` = jen tisky se stranou od burzy (v6, ADR-0057). Hodnota jde do
#: sloupce `flow.source` — zlom midpoint → tisky nesou řádky, ne datum.
SOURCE_MIDPOINT = "midpoint"
SOURCE_AGGRESSOR = "aggressor"
CUMDELTA_SOURCES = (SOURCE_MIDPOINT, SOURCE_AGGRESSOR)
#: Zdroj živých trackerů enginu; přepíná ho jen bump mechaniky (#1403), návrat
#: pinem image (ADR-0057 bod 10). Testy volí zdroj explicitním parametrem.
LIVE_SOURCE = SOURCE_AGGRESSOR if CUMDELTA_PRINTS else SOURCE_MIDPOINT
#: Okno sklonu CumΔ v minutách (ADR-0057 bod 4) — jediná délka pro tendenci
#: i potvrzení COMBINED v news-engine (#1460, #1589), viz `flow_window_sum`.
FLOW_WINDOW_MIN = 10


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
    tisky bez strany, `structured` = přírůstek objemu bez tisku (nohy spreadů,
    bloky — CME je jako trade nevysílá, ADR-0027), `fallback` = kontrakt-minuty
    bez jediného tisku. Měří se v obou zdrojích stejně (#1071); pod `midpoint`
    dává unknown i fallback znaménko midpoint test, pod `aggressor` jdou mimo
    tok (ADR-0057 bod 2).
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
    # Zdroj znaménka (`CUMDELTA_SOURCES`, ADR-0057 bod 8): partice před fází 3
    # mají NULL = midpoint. Srovnání řad před/po přepnutí stojí na tomhle
    # sloupci, ne na datu.
    source: str | None = None
    # Pokrytí trade větví ZA TUTO MINUTU (#1071): kontrakty s tiskem se stranou,
    # bez strany, bez tisku (struktura) a bez jediného tisku (fallback),
    # plus tisky zahozené pro chybějící deltu. None = trade větev neběžela
    # (tasty odpojené) — nula by tvrdila „nic nepokryto", což není měření.
    printed_volume: float | None = None
    unknown_volume: float | None = None
    structured_volume: float | None = None
    fallback_volume: float | None = None
    dropped_no_delta: int | None = None


def midpoint_sign(last: float, bid: float, ask: float) -> int:
    """Midpoint test (SPEC 4.5): last nad midem → +1, pod → −1, přesně na midu → 0."""
    mid = (bid + ask) / 2.0
    if last > mid:
        return 1
    if last < mid:
        return -1
    return 0


class CumDeltaTracker:
    """Denní agregátor flowΔ/CumΔ a čistého objemu; zdroj znaménka viz modul."""

    def __init__(self, multiplier: float, source: str = LIVE_SOURCE) -> None:
        if source not in CUMDELTA_SOURCES:
            raise ValueError(f"Neznámý zdroj CumΔ {source!r} (povolené: {CUMDELTA_SOURCES})")
        self._multiplier = multiplier
        self._source = source
        self._cum = 0.0
        self._minute_flow = 0.0
        self._last_volume: dict[OptionContractSpec, float] = {}
        # Tisky od posledního baru kontraktu (#615 fáze 3): bar větev z nich
        # pozná, kolik přírůstku objemu přišlo jako tisk (rozklad, pokrytí)
        self._printed_since_bar: dict[OptionContractSpec, float] = {}
        self._unknown_since_bar: dict[OptionContractSpec, float] = {}
        self._coverage = CumDeltaCoverage()
        # Stav pokrytí při poslední uzávěrce minuty — FlowRow nese přírůstek (#1071)
        self._coverage_at_close = CumDeltaCoverage()
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

    @property
    def prints(self) -> bool:
        """Dává znaménko jen tisk se stranou od burzy (zdroj `aggressor`, ADR-0057)?"""
        return self._source == SOURCE_AGGRESSOR

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
        self._coverage_at_close = CumDeltaCoverage()
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

    def add_dx_trade(
        self,
        spec: OptionContractSpec,
        size: float | None,
        aggressor: str | None,
        delta: float | None,
    ) -> float:
        """Jeden tisk dxFeed `TimeAndSale` (ADR-0032, ADR-0057).

        Pokrytí (se stranou, bez strany, bez delty) se měří v obou zdrojích
        stejně (#1071). Tisk bez delty se do pokrytí stran nepočítá, jen do
        `dropped_no_delta`; tisk bez strany (`UNDEFINED` — počtem ~0,05 %,
        objemem ES 1–15 % tisků za den, téměř vše bloky daleko OTM) do `unknown`.

        Zdroj `aggressor` (ADR-0057 bod 1–2): tisk se stranou dává
        net += sign · size, s deltou i flowΔ = sign · size · Δ · M; tisk bez
        delty jde mimo tok, čistý objem ale nese; tisk bez strany jde mimo obojí.
        Zdroj `midpoint`: tisky jen měří pokrytí, znaménko dává bar větev.
        """
        if size is None or size <= 0:
            return 0.0
        sign = _AGGRESSOR_SIGN.get(aggressor.upper() if isinstance(aggressor, str) else "", 0)
        if delta is None:
            self._coverage.dropped_no_delta += 1
            if self.prints and sign != 0:
                self._net_volume[spec] = self._net_volume.get(spec, 0.0) + sign * size
            return 0.0
        if sign == 0:
            self._unknown_since_bar[spec] = self._unknown_since_bar.get(spec, 0.0) + size
            self._coverage.unknown_volume += size
            return 0.0
        self._printed_since_bar[spec] = self._printed_since_bar.get(spec, 0.0) + size
        self._coverage.printed_volume += size
        if not self.prints:
            return 0.0
        flow = sign * size * delta * self._multiplier
        self._net_volume[spec] = self._net_volume.get(spec, 0.0) + sign * size
        self._apply(flow)
        return flow

    def add_volume(self, spec: OptionContractSpec, cumulative_volume: float) -> float:
        """Bar větev bez znaménka: přírůstek kumulativního volume, rozklad a pokrytí.

        Vrací přírůstek objemu za bar. První bar dne přírůstek nemá (jen založí
        stav); pokles kumulativního volume je nekonzistence feedu → přírůstek 0
        s varováním, nikdy záporný. Zdroj `aggressor` volá jen tuhle část —
        objem bez tisku jde mimo tok a jen se měří (ADR-0057 bod 1); `midpoint`
        ji volá z `add_bar`.
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
        # Pokrytí (#1071) se měří NEZÁVISLE na zdroji znaménka: v režimu midpoint
        # říká, co trade větev pokrývá — verdikt #1018 i srovnání před a po
        # přepnutí stojí na týchž číslech. Fallback se počítá jen s běžící trade
        # větví: bez tasty je „bez tisku" každý kontrakt a číslo by nic neměřilo.
        if printed > 0.0:
            self._coverage.structured_volume += max(delta_volume - printed - unknown, 0.0)
        elif self.dx_active:
            self._coverage.fallback_volume += delta_volume
        return delta_volume

    def add_bar(
        self,
        spec: OptionContractSpec,
        cumulative_volume: float,
        last: float,
        bid: float,
        ask: float,
        delta: float,
    ) -> float:
        """Bar větev zdroje `midpoint`: přírůstek volume × midpoint test × Δ × M.

        Rozklad a pokrytí dělá `add_volume`. Pod zdrojem `aggressor` bar
        znaménko nedává (ADR-0057 bod 1): proběhne jen `add_volume` a tok je 0.
        """
        delta_volume = self.add_volume(spec, cumulative_volume)
        if self.prints or delta_volume <= 0.0:
            return 0.0
        sign = midpoint_sign(last, bid, ask)
        flow = sign * delta_volume * delta * self._multiplier
        self._net_volume[spec] = self._net_volume.get(spec, 0.0) + sign * delta_volume
        self._apply(flow)
        return flow

    def close_minute(self, ts_min: dt.datetime) -> FlowRow:
        """Uzavře minutu: vrátí bod řady (flowΔ minuty, průběžná CumΔ) a vynuluje minutu.

        Pokrytí za minutu (#1071) = rozdíl denních čítačů od poslední uzávěrky;
        bez trade větve zůstává NULL.
        """
        measured = self.dx_active
        now, last = self._coverage, self._coverage_at_close
        row = FlowRow(
            ts_min=ts_min,
            flow_delta=self._minute_flow,
            cum_delta=self._cum,
            source=self._source,
            printed_volume=now.printed_volume - last.printed_volume if measured else None,
            unknown_volume=now.unknown_volume - last.unknown_volume if measured else None,
            structured_volume=(
                now.structured_volume - last.structured_volume if measured else None
            ),
            fallback_volume=now.fallback_volume - last.fallback_volume if measured else None,
            dropped_no_delta=now.dropped_no_delta - last.dropped_no_delta if measured else None,
        )
        self._coverage_at_close = CumDeltaCoverage(**vars(now))
        self._minute_flow = 0.0
        return row

    def _apply(self, flow: float) -> None:
        self._cum += flow
        self._minute_flow += flow


def flow_window_sum(
    rows: Iterable[tuple[dt.datetime, float | None]],
    end: dt.datetime,
    minutes: int = FLOW_WINDOW_MIN,
) -> float | None:
    """Σ flowΔ řádků s ts ∈ (end − minutes, end] v seanci minuty `end`; neúplné okno → None.

    Sklon CumΔ pro tendenci i news-engine (ADR-0057 bod 4, #1460, #1589) —
    jedna čistá funkce místo rozdílu CumΔ dvou řádků. `rows` = (ts_min,
    flow_delta) z partic flow, klidně ze dvou UTC dnů, v libovolném pořadí
    a s řádky mimo okno (i po `end`); NULL flow_delta = minuta s mezerou.

    Řádek nese tok od předchozí uzávěrky minuty, takže chybějící řádek uvnitř
    okna tok neztratí (přejde do dalšího řádku). Součet je úplný, když:
    - řádek `end` existuje (tok poslední minuty je uzavřený);
    - žádný řádek v okně nemá NULL (tok mezery chybí);
    - před oknem je doložená uzávěrka: hraniční řádek `end − minutes`
      s libovolnou hodnotou, i NULL — bez něj by první řádek okna nesl i tok
      zpoza okna. Začíná-li okno přesně začátkem seance, je uzávěrkou roll
      trackeru, a ten dokládá jen řádek té minuty (pozdější roll by tok
      prvních minut zahodil);
    - okno nezačíná před začátkem seance (#1581: CumΔ se v 17:00 CT nuluje);
    - v okně není duplicitní ts (dvojí zápis nejde odlišit od dvou uzávěrek).

    Za zavřeného trhu nese řada nuly, takže pauza dá 0.0, ne None. `end` je
    ts_min řádku: celá minuta s časovou zónou — naivní čas by
    `trading_session_date` vyložil v zóně stroje.
    """
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError(f"flow_window_sum: end {end!r} nemá časovou zónu")
    if end.second or end.microsecond:
        raise ValueError(f"flow_window_sum: end {end!r} není celá minuta (ts_min)")
    if minutes < 1:
        raise ValueError(f"flow_window_sum: okno {minutes} min, musí být ≥ 1")
    step = dt.timedelta(minutes=1)
    boundary = end - minutes * step
    first = boundary + step
    session_start = session_bounds(trading_session_date(end))[0]
    if first < session_start:
        return None
    anchor = first if first == session_start else boundary
    anchored = False
    window: dict[dt.datetime, float | None] = {}
    for ts, flow in rows:
        if ts == anchor:
            anchored = True
        if boundary < ts <= end:
            if ts in window:
                return None
            window[ts] = flow
    if not anchored or end not in window:
        return None
    total = 0.0
    for ts in sorted(window):
        flow = window[ts]
        if flow is None:
            return None
        total += flow
    return total
