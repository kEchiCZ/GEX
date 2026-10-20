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

Mezera (ADR-0057 bod 3, jen `aggressor`): orchestrátor každou minutu hlásí stav
trade větve (`observe`) a v témž okamžiku řeže tisky (`seal`), takže interval
řádku M je (vzorek M−1, vzorek M] a oba jeho konce pokrývá vzorek. `begin_cycle`
ze vzorků zafixuje verdikt minuty. Minuta s mezerou má v řadě flowΔ i CumΔ NULL,
její tisky se zahodí (i z čistého objemu) a CumΔ pokračuje od poslední měřené
úrovně; při zavřeném trhu mezera nevzniká. Kontrakt bez baru v cyklu (fallback
řetězu, stará kotace) má po mezeře jen znovu založený stav objemu; bar bez
přírůstku (kotace z cache) počítadla tisků nevyprázdní (#1460).
"""

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import NamedTuple

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


def stored_sources(source: str) -> frozenset[str | None]:
    """Hodnoty sloupce `source` uložených řádků (flow, netflow), které patří zdroji (#1460).

    NULL = řádek z doby před zavedením sloupce (flow před #615 fází 3, netflow
    před #1460), tedy midpoint. Hybrid `dxfeed` (do #1460) nepatří žádnému
    zdroji. Navázání kumulativu po restartu bere jen řádky zdroje běžícího
    trackeru: engine nasazený s v6 v pauze 16:00–17:00 CT, která ještě patří
    seanci D, by jinak převzal midpointovou úroveň (ADR-0057 bod 8).
    """
    if source not in CUMDELTA_SOURCES:
        raise ValueError(f"Neznámý zdroj CumΔ {source!r} (povolené: {CUMDELTA_SOURCES})")
    if source == SOURCE_MIDPOINT:
        return frozenset({None, SOURCE_MIDPOINT})
    return frozenset({source})


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

    def shares(self) -> tuple[float | None, float | None]:
        """Podíl tisků se stranou z klasifikovaného a z CELÉHO přírůstku objemu.

        Celý přírůstek zahrnuje i strukturu (#1460) — pod tisky jediný objem
        s tokem; vzor `cumdelta_compare`. None = nulový jmenovatel, nic se
        neměřilo („nevíme ≠ nula", #1172).
        """
        classified = self.printed_volume + self.unknown_volume + self.fallback_volume
        total = classified + self.structured_volume
        return (
            self.printed_volume / classified if classified > 0 else None,
            self.printed_volume / total if total > 0 else None,
        )

    def as_dict(self) -> dict[str, float]:
        share, share_total = self.shares()
        return {
            "printed_volume": self.printed_volume,
            "unknown_volume": self.unknown_volume,
            "structured_volume": self.structured_volume,
            "fallback_volume": self.fallback_volume,
            "dropped_no_delta": float(self.dropped_no_delta),
            # Podíly bez měření 0.0 jako před #1460 (midpoint, v5 beze změny);
            # pod zdrojem `aggressor` je `day_stats` přepíše spárovanými (i None)
            "printed_share": share if share is not None else 0.0,
            "printed_share_total": share_total if share_total is not None else 0.0,
        }


@dataclass(frozen=True)
class FlowRow:
    """Minutový bod řady pro panel Cum Δ a persistenci do derived/."""

    ts_min: dt.datetime
    # None = mezera: trade větev v minutě neběžela (ADR-0057 bod 3), ne 0 ani
    # poslední hodnota. Midpoint zdroj (mechanika v5) píše vždy číslo.
    flow_delta: float | None
    cum_delta: float | None
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


class _PendingPrint(NamedTuple):
    """Tisk další seance přijatý před rollem trackeru — čeká na reset (#1460)."""

    session_date: dt.date
    spec: OptionContractSpec
    size: float
    aggressor: str | None
    delta: float | None


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
        #: Jen zdroj `midpoint` — pod tisky rozhoduje verdikt minuty (#1460).
        self.dx_active: bool = False
        self._breakdowns: dict[OptionContractSpec, BarBreakdown] = {}
        # Čistý klasifikovaný objem per kontrakt (buy − sell, v kontraktech) —
        # vstup flow-adjusted OI odhadu (ADR-0011, #222)
        self._net_volume: dict[OptionContractSpec, float] = {}
        # Obchodní den (Globex seance), ke kterému kumulativ patří (#638)
        self._session_date: dt.date | None = None
        # Model mezery (ADR-0057 bod 3), jen zdroj `aggressor`. Tisky od
        # posledního řezu (`seal` ve vzorku) leží v otevřeném úseku
        # (`_minute_flow`, `_minute_net`), řez je přesune do uzavřeného, který
        # čeká na verdikt v `close_minute`; tisky po řezu patří dalšímu řádku.
        # Čistý objem jde do `_net_volume` až řezem — FA a netflow čtou uprostřed
        # cyklu net do posledního řezu, tedy jen tisky, které verdikt pokrývá.
        # `_sealed_net` drží přírůstek uzavřeného úseku pro návrat při mezeře;
        # seed z partic (`restore_*`) do úseku nepatří, takže se s ním nevrací.
        self._minute_net: dict[OptionContractSpec, float] = {}
        self._sealed_flow = 0.0
        self._sealed_net: dict[OptionContractSpec, float] = {}
        # Proběhl od poslední uzávěrky řez? Reset seance ho nemaže — vzorek
        # openu přijde před resetem v `run_cycle`
        self._sealed = False
        # Denní čítače pokrytí v okamžiku posledního řezu: tiskové pokrytí
        # řádku sedí na tentýž interval jako jeho tok
        self._coverage_at_seal = CumDeltaCoverage()
        # Tisky další seance přijaté před rollem trackeru (#1460): reset běží
        # v `run_cycle` až po sweepu a tisky od openu 17:00 CT by jinak smazal.
        # `_next_session_sealed` = kolik z nich přišlo před posledním řezem.
        self._next_session: list[_PendingPrint] = []
        self._next_session_sealed = 0
        # Kontrakty s barem od posledního `begin_cycle` (jen zdroj `aggressor`):
        # kontrakt bez baru v cyklu (fallback řetězu s `volume=None`, kotace nad
        # `quote_max_age_s`, pád cyklu po `begin_cycle` mezi verdiktem a bary)
        # v dalším `begin_cycle` ztratí stav objemu — jeho další bar nese
        # přírůstek celé mezery a jen znovu založí stav jako po restartu (#1460).
        # Bar s přírůstkem 0 (zamrzlá kotace, kotace z cache) je krmený. Cyklus,
        # který spadl před `begin_cycle` (sweep), množinu nemaže ani řádek
        # nezapíše: další řádek i bar nesou tentýž delší interval.
        self._fed: set[OptionContractSpec] = set()
        # Kontrakty, jejichž tisky čekající na bar s přírůstkem zasáhla mezera
        # trade větve (bar s přírůstkem 0 v minutě s mezerou): rozklad dalšího
        # přírůstku je NULL a do pokrytí nejde (#1460, jen zdroj `aggressor`)
        self._gapped: set[OptionContractSpec] = set()
        # Tisky spárované s měřeným přírůstkem objemu (jen zdroj `aggressor`):
        # z nich jsou podíly v `day_stats`. Tisk z minuty bez baru kontraktu je
        # v pokrytí (řádek ho nese na intervalu toku), objem k němu ale chybí.
        self._paired_printed = 0.0
        self._paired_unknown = 0.0
        # Vzorky `observe`: AND otevřeného okna včetně hraničního vzorku (None =
        # okno bez vzorku), poslední vzorek (None = žádný od startu procesu)
        # a verdikt zafixovaný `begin_cycle`. Reset seance je nemaže — hranice
        # 17:00 CT by jinak vyšla jako mezera.
        self._window_ok: bool | None = None
        self._last_ok: bool | None = None
        self._verdict = False
        # Cyklus začal (`begin_cycle`) a minutu ještě neuzavřel — spadl-li mezi
        # tím, tisky zůstaly v otevřené minutě a verdikt se s ní přenáší
        self._cycle_open = False

    @property
    def cum_delta(self) -> float:
        """Průběžná CumΔ včetně neuzavřených tisků; pod tisky ji potvrdí až `close_minute`."""
        if self.prints:
            return self._cum + self._sealed_flow + self._minute_flow
        return self._cum

    @property
    def source(self) -> str:
        return self._source

    @property
    def prints(self) -> bool:
        """Dává znaménko jen tisk se stranou od burzy (zdroj `aggressor`, ADR-0057)?"""
        return self._source == SOURCE_AGGRESSOR

    @property
    def minute_measured(self) -> bool:
        """Je minuta cyklu měřená (verdikt z `begin_cycle`)? Zdroj `midpoint` mezeru nezná."""
        return self._verdict or not self.prints

    def observe(self, *, running: bool, market_closed: bool) -> None:
        """Vzorek stavu trade větve; orchestrátor ho dává každou minutu (ADR-0057 bod 3).

        Hned po něm následuje řez tisků (`seal`), takže vzorky ohraničují
        interval řádku. Minuta je měřená, když hraniční vzorek (poslední
        z minulého okna) i všechny vzorky do `begin_cycle` říkají „běží", nebo
        „trh zavřený" — při zavřeném trhu mezera nevzniká a tok je 0. Sekundární
        řetěz s cyklem po N minutách tak dostane AND přes celé své okno. Hranice
        prvního okna po startu procesu známá není: za zavřeného trhu nic
        nechybí, za otevřeného tracker neviděl tisky před svým vznikem → první
        minuta je mezera a restart je v řadě vidět.
        """
        ok = running or market_closed
        if self._window_ok is None:
            boundary = market_closed if self._last_ok is None else self._last_ok
            self._window_ok = boundary and ok
        else:
            self._window_ok = self._window_ok and ok
        self._last_ok = ok

    def begin_cycle(self) -> None:
        """Zafixuje verdikt minuty pro celý cyklus a otevře další okno vzorků.

        Rozklad barů, netflow, FA i `close_minute` čtou týž verdikt, i když tisky
        chodí mezi awaity cyklu. Bez vzorku a řezu od minulého cyklu není nic
        doloženo → mezera. Minutu, kterou spadlý cyklus neuzavřel, nese i tento —
        měřená je jen tehdy, byl-li měřený i ten.

        Pod tisky kontrakt, který v minulém cyklu nedostal bar, ztratí stav
        objemu (#1460): přírůstek jeho dalšího baru pokrývá celou mezeru, ne
        jednu minutu, a tisky od posledního baru nesou celou mezeru — bar jen
        znovu založí stav a vynuluje počítadla tisků, do rozkladu ani pokrytí
        nejde. Tok za mezeru nesou tisky započtené v okamžiku tisku.
        """
        carried = self._verdict if self._cycle_open else True
        cut = self._sealed or not self.prints
        self._verdict = bool(self._window_ok) and carried and cut
        self._window_ok = None
        self._cycle_open = True
        if self.prints:
            for spec in self._last_volume.keys() - self._fed:
                del self._last_volume[spec]
            self._fed.clear()

    def seal(self) -> None:
        """Řez tisků v okamžiku vzorku; orchestrátor ho volá hned po `observe` (#1460).

        Interval řádku M je (vzorek M−1, vzorek M], takže oba jeho konce pokrývá
        vzorek a verdikt (hraniční vzorek ∧ vzorky okna ∧ žádný reconnect mezi
        nimi) sedí přesně na tisky řádku. Uzávěrka minuty běží až po sweepu
        a zápisech (u druhého instrumentu i po celém cyklu prvního): výpadek
        mezi vzorkem a uzávěrkou by bez řezu skončil v řádku, který verdikt
        označil za měřený (ADR-0057 bod 3: částečná minuta je NULL celá).
        Tisky po řezu patří dalšímu řádku, čistý objem jde do denního až řezem.
        Pod midpointem (v5) se nepoužije — minuta trvá do `close_minute` jako dřív.
        """
        if not self.prints:
            return
        self._cut()
        self._sealed = True
        self._next_session_sealed = len(self._next_session)

    def _cut(self) -> None:
        """Otevřený úsek tisků → uzavřený (tok, čistý objem, stav tiskového pokrytí)."""
        self._sealed_flow += self._minute_flow
        self._minute_flow = 0.0
        for spec, net in self._minute_net.items():
            self._net_volume[spec] = self._net_volume.get(spec, 0.0) + net
            self._sealed_net[spec] = self._sealed_net.get(spec, 0.0) + net
        self._minute_net.clear()
        self._coverage_at_seal = CumDeltaCoverage(**vars(self._coverage))

    def day_stats(self) -> dict[str, float | str | None]:
        """Denní pokrytí toku podle zdroje — /status a Settings (#615 krok 5).

        Pod tisky jsou podíly jen z tisků spárovaných s měřeným přírůstkem
        objemu (#1460): tisky z minut bez baru kontraktu (fallback řetězu, stará
        kotace, první bar po startu) `printed_volume` nese (= Σ řádků flow),
        objem k nim ale chybí a podíl by nadsadily. Bez spárovaného objemu
        (start do fallbacku řetězu) je podíl None, ne 0 — nic se neměřilo
        (#1172). Pod midpointem beze změny (bez měření 0.0).
        """
        stats: dict[str, float | str | None] = {
            "source": self._source,
            **self._coverage.as_dict(),
        }
        if self.prints:
            share, share_total = replace(
                self._coverage,
                printed_volume=self._paired_printed,
                unknown_volume=self._paired_unknown,
            ).shares()
            stats["printed_share"] = share
            stats["printed_share_total"] = share_total
        return stats

    def take_breakdowns(self) -> dict[OptionContractSpec, BarBreakdown]:
        """Rozklady přírůstků od posledního odběru (řada printvol, #1007) — a vyprázdní je."""
        taken = self._breakdowns
        self._breakdowns = {}
        return taken

    def net_volume(self, spec: OptionContractSpec) -> float:
        """Denní čistý klasifikovaný objem kontraktu (buy − sell; ADR-0011).

        Pod tisky do posledního řezu (`seal`): tisky otevřeného úseku patří
        dalšímu řádku a FA ani netflow minuty je nesmí nést.
        """
        return self._net_volume.get(spec, 0.0)

    def net_volumes(self) -> dict[OptionContractSpec, float]:
        """Kopie celé mapy čistého objemu (pod tisky do posledního řezu) — řada netflow (#232)."""
        return dict(self._net_volume)

    def restore_net_volume(self, values: dict[OptionContractSpec, float]) -> None:
        """Naváže kumulativ z partice netflow po restartu uprostřed dne (#232).

        Přičítá uložený základ k toku naměřenému od startu procesu, stejně jako
        `restore_cum` (#1460): tisky zdroje `aggressor` chodí i před prvním
        cyklem (řez vzorku je přesune do denního ještě před seedem) a cyklus
        spadlý mezi barem a seedem nechá v mapě přírůstky — přepsání ani
        `setdefault` by jeden z obou dílů zahodily. Volat nejvýš jednou; hlídá
        runtime (pending flag), ne tracker.
        """
        for spec, net in values.items():
            self._net_volume[spec] = self._net_volume.get(spec, 0.0) + net

    def reset(self) -> None:
        """Reset na začátku obchodního dne (SPEC 4.5; volá `roll_session`, #638).

        Řez (`_sealed`) a vzorky se nemažou: vzorek openu přijde před resetem.
        """
        self._cum = 0.0
        self._minute_flow = 0.0
        self._sealed_flow = 0.0
        self._last_volume.clear()
        self._net_volume.clear()
        self._printed_since_bar.clear()
        self._unknown_since_bar.clear()
        self._coverage = CumDeltaCoverage()
        self._coverage_at_close = CumDeltaCoverage()
        self._coverage_at_seal = CumDeltaCoverage()
        self._breakdowns.clear()
        self._minute_net.clear()
        self._sealed_net.clear()
        self._fed.clear()
        self._gapped.clear()
        self._paired_printed = 0.0
        self._paired_unknown = 0.0
        self._cycle_open = False  # otevřená minuta zahozena, verdikt se nepřenáší

    def roll_session(self, session_date: dt.date) -> bool:
        """Reset kumulativů na hranici Globex seance (#638, SPEC 4.5).

        Vrací True, když právě proběhl reset (přechod na nový obchodní den).
        První volání po startu procesu jen zafixuje seanci BEZ resetu —
        restart uprostřed seance nesmí zahodit dopolední tok; navázání
        z partic (flow/netflow seed) řeší runtime.

        Kotva obou kumulativů je open seance (17:00 CT): CumΔ tím sedí na osu
        obchodního dne (#512) a net objem na ranní OI archiv, který odráží
        pozice k předchozímu settle — tok od open je přesně to, co v něm není.

        Pod tisky se po resetu přehrají tisky nové seance přijaté před rollem
        (#1460, `add_dx_trade(session_date=…)`): ty před posledním řezem do
        uzavřeného úseku, pozdější do otevřeného — jako by reset proběhl v openu.
        """
        if self._session_date == session_date:
            return False
        first = self._session_date is None
        self._session_date = session_date
        if first:
            return False
        pending, sealed_count = self._next_session, self._next_session_sealed
        self._next_session = []
        self._next_session_sealed = 0
        self.reset()
        before_cut = [item for item in pending[:sealed_count] if item.session_date == session_date]
        after_cut = [item for item in pending[sealed_count:] if item.session_date == session_date]
        dropped = len(pending) - len(before_cut) - len(after_cut)
        if dropped:
            logger.warning(
                "CumΔ: %d tisků jiné seance než %s zahozeno při rollu", dropped, session_date
            )
        for item in before_cut:
            self.add_dx_trade(item.spec, item.size, item.aggressor, item.delta)
        if before_cut:
            self._cut()
        for item in after_cut:
            self.add_dx_trade(item.spec, item.size, item.aggressor, item.delta)
        return True

    def promote(self) -> None:
        """Sekundár se v rollu stává aktivním řetězem (#1460, varianta C, ADR-0057 doplněk).

        Volá se při předání trackeru nové pipeline, před dalším vzorkem. Hranicí
        je poslední řez (vzorek minuty rollu), ve kterém aktivní řetěz D uzavřel
        svůj poslední řádek. Net řetězu (denní, uzavřený i otevřený úsek), vzorky,
        verdikt a tisky další seance zůstávají: net navazuje bez mezery a první
        verdikt aktivního řetězu pokryje i okno, které sekundár ještě neuzavřel
        (při mezeře v něm vrátí celý uzavřený úsek). Metriky aktivního řetězu
        začínají na hranici jako u nového trackeru:
        - CumΔ: kumulativ sekundáru do CumΔ symbolu nepatří, runtime ji naváže
          z flow partice aktivního řetězu; tok uzavřeného úseku leží před hranicí;
        - pokrytí a podíly: jen tisky po hranici, ostatní čítače od nuly;
        - rozklady a stav barů: rozklady sekundáru se nezapisují, první bar
          aktivního řetězu stav jen založí — jinak by jeho přírůstek a tisky
          sahaly přes hranici do doby sekundáru.

        Pod midpointem se nevolá (předání jen pod tisky).
        """
        if not self.prints:
            raise ValueError("promote: předání trackeru v rollu jen pod zdrojem tisků")
        now, cut = self._coverage, self._coverage_at_seal
        self._coverage = CumDeltaCoverage(
            printed_volume=now.printed_volume - cut.printed_volume,
            unknown_volume=now.unknown_volume - cut.unknown_volume,
            dropped_no_delta=now.dropped_no_delta - cut.dropped_no_delta,
        )
        self._coverage_at_close = CumDeltaCoverage()
        self._coverage_at_seal = CumDeltaCoverage()
        self._cum = 0.0
        self._sealed_flow = 0.0
        self._last_volume.clear()
        self._printed_since_bar.clear()
        self._unknown_since_bar.clear()
        self._breakdowns.clear()
        self._fed.clear()
        self._gapped.clear()
        self._paired_printed = 0.0
        self._paired_unknown = 0.0

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
        session_date: dt.date | None = None,
    ) -> float:
        """Jeden tisk dxFeed `TimeAndSale` (ADR-0032, ADR-0057).

        Pokrytí (se stranou, bez strany, bez delty) se měří v obou zdrojích
        stejně (#1071). Tisk bez delty se do pokrytí stran nepočítá, jen do
        `dropped_no_delta`; tisk bez strany (`UNDEFINED` — počtem ~0,05 %,
        objemem ES 1–15 % tisků za den, téměř vše bloky daleko OTM) do `unknown`.

        Zdroj `aggressor` (ADR-0057 bod 1–2): tisk se stranou dává
        net += sign · size, s deltou i flowΔ = sign · size · Δ · M; tisk bez
        delty jde mimo tok, čistý objem ale nese; tisk bez strany jde mimo obojí.
        Tok i čistý objem čekají v otevřeném úseku na řez a verdikt minuty
        (bod 3). `session_date` = obchodní den podle času příjmu: tisk jiné
        seance, než ve které tracker je, čeká stranou na `roll_session` (#1460)
        — reset v `run_cycle` přijde až po sweepu a tisky od openu by smazal.
        Zdroj `midpoint`: tisky jen měří pokrytí, znaménko dává bar větev;
        `session_date` nečte (v5 beze změny).
        """
        if size is None or size <= 0:
            return 0.0
        if (
            self.prints
            and session_date is not None
            and self._session_date is not None
            and session_date != self._session_date
        ):
            self._next_session.append(_PendingPrint(session_date, spec, size, aggressor, delta))
            return 0.0
        sign = _AGGRESSOR_SIGN.get(aggressor.upper() if isinstance(aggressor, str) else "", 0)
        if delta is None:
            self._coverage.dropped_no_delta += 1
            if self.prints and sign != 0:
                self._add_print_net(spec, sign * size)
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
        self._add_print_net(spec, sign * size)
        self._minute_flow += flow
        return flow

    def _add_print_net(self, spec: OptionContractSpec, net: float) -> None:
        """Čistý objem z tisku do otevřeného úseku; do denního ho přesune řez (`seal`)."""
        self._minute_net[spec] = self._minute_net.get(spec, 0.0) + net

    def add_volume(self, spec: OptionContractSpec, cumulative_volume: float) -> float:
        """Bar větev bez znaménka: přírůstek kumulativního volume, rozklad a pokrytí.

        Vrací přírůstek objemu za bar. První bar dne přírůstek nemá (jen založí
        stav); pokles kumulativního volume je nekonzistence feedu → přírůstek 0
        s varováním, nikdy záporný. Zdroj `aggressor` volá jen tuhle část —
        objem bez tisku jde mimo tok a jen se měří (ADR-0057 bod 1); `midpoint`
        ji volá z `add_bar`.

        Tisky od minulého baru jsou úplné, jen když trade větev celou dobu běžela:
        pod `midpoint` rozhoduje okamžitý `dx_active` (v5), pod tisky verdikt
        minuty (#1460). Jinak se přírůstek rozloží jako neznámý (NULL) a do
        pokrytí nejde. Pod tisky má minulý bar kontrakt vždy z minulého cyklu:
        po cyklu bez baru (fallback, stará kotace) stav objemu zahodí
        `begin_cycle` a bar jen znovu založí stav jako po restartu.

        Pod tisky bar bez přírůstku (kotace z cache mezi sweepy křídel, zamrzlá
        kotace, objem IBKR pozadu za tisky) počítadla tisků nevyprázdní — objem
        k nim přinese až bar s přírůstkem, jinak by jeho rozklad tisky ztratil
        a zbytek připsal struktuře (#1460). Vyprázdní je každý jiný bar: bar
        s přírůstkem (rozklad), bar s poklesem kumulativu i bar, který stav
        objemu zakládá (oba bez rozkladu). Rozklad baru s přírůstkem je NULL
        a do pokrytí nejde, je-li mezerou minuta toho baru nebo baru bez
        přírůstku od posledního vyprázdnění (`_gapped`). Verdikt ale končí
        řezem ve vzorku, kdežto bar po sweepu vyprázdní i tisky po řezu:
        výpadek mezi vzorkem a barem rozklad nezneplatní (limit metriky,
        ADR-0057 doplněk).
        """
        previous = self._last_volume.get(spec)
        self._last_volume[spec] = cumulative_volume
        if self.prints:
            self._fed.add(spec)
            if previous is not None and cumulative_volume == previous:
                if not self._verdict:
                    self._gapped.add(spec)
                return 0.0
            branch_ran = self._verdict and spec not in self._gapped
            self._gapped.discard(spec)
        else:
            branch_ran = self.dx_active
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
                printed=printed_total if branch_ran else None,
                structured=max(delta_volume - printed_total, 0.0) if branch_ran else None,
            )
        if self.prints and not branch_ran:
            return delta_volume
        # Pokrytí (#1071) se měří NEZÁVISLE na zdroji znaménka: v režimu midpoint
        # říká, co trade větev pokrývá — verdikt #1018 i srovnání před a po
        # přepnutí stojí na týchž číslech. Fallback se počítá jen s běžící trade
        # větví: bez tasty je „bez tisku" každý kontrakt a číslo by nic neměřilo.
        if printed > 0.0:
            self._coverage.structured_volume += max(delta_volume - printed - unknown, 0.0)
        elif branch_ran:
            self._coverage.fallback_volume += delta_volume
        if self.prints:
            self._paired_printed += printed
            self._paired_unknown += unknown
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

        Zdroj `aggressor` (ADR-0057 bod 3) uzavírá jen úsek do posledního řezu
        (`seal`); tisky po něm patří dalšímu řádku, tiskové pokrytí řádku sedí
        na týž úsek, pokrytí z barů na cyklus. Měřená minuta potvrdí tok úseku
        do CumΔ. Minuta s mezerou má flowΔ, CumΔ i pokrytí NULL a úsek se zahodí
        celý (částečná minuta se neextrapoluje): čistý objem i denní pokrytí se
        vrátí o jeho tisky a CumΔ pokračuje od poslední měřené úrovně. Bez řezu
        od minulé uzávěrky (vzorek selhal) nic nedokládá ani otevřený úsek —
        zahodí se i ten.
        """
        if self.prints:
            row = self._close_prints(ts_min)
        else:
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
                dropped_no_delta=(
                    now.dropped_no_delta - last.dropped_no_delta if measured else None
                ),
            )
            self._coverage_at_close = CumDeltaCoverage(**vars(now))
            self._minute_flow = 0.0
            self._minute_net.clear()
        self._cycle_open = False
        return row

    def _close_prints(self, ts_min: dt.datetime) -> FlowRow:
        """Uzávěrka minuty zdroje `aggressor` nad úsekem do posledního řezu."""
        now, last, cut = self._coverage, self._coverage_at_close, self._coverage_at_seal
        if self._verdict:
            self._cum += self._sealed_flow
            row = FlowRow(
                ts_min=ts_min,
                flow_delta=self._sealed_flow,
                cum_delta=self._cum,
                source=self._source,
                printed_volume=cut.printed_volume - last.printed_volume,
                unknown_volume=cut.unknown_volume - last.unknown_volume,
                structured_volume=now.structured_volume - last.structured_volume,
                fallback_volume=now.fallback_volume - last.fallback_volume,
                dropped_no_delta=cut.dropped_no_delta - last.dropped_no_delta,
            )
            self._coverage_at_close = CumDeltaCoverage(
                printed_volume=cut.printed_volume,
                unknown_volume=cut.unknown_volume,
                structured_volume=now.structured_volume,
                fallback_volume=now.fallback_volume,
                dropped_no_delta=cut.dropped_no_delta,
            )
        else:
            row = FlowRow(ts_min=ts_min, flow_delta=None, cum_delta=None, source=self._source)
            if self._sealed:
                # Návrat uzavřeného úseku; otevřený (po řezu) patří dalšímu řádku
                for spec, net in self._sealed_net.items():
                    self._net_volume[spec] = self._net_volume.get(spec, 0.0) - net
                self._coverage = CumDeltaCoverage(
                    printed_volume=now.printed_volume - (cut.printed_volume - last.printed_volume),
                    unknown_volume=now.unknown_volume - (cut.unknown_volume - last.unknown_volume),
                    structured_volume=last.structured_volume,
                    fallback_volume=last.fallback_volume,
                    dropped_no_delta=(
                        now.dropped_no_delta - (cut.dropped_no_delta - last.dropped_no_delta)
                    ),
                )
            else:
                # Bez řezu: uzavřený úsek je prázdný, otevřený nepokrývá žádný vzorek
                self._coverage = CumDeltaCoverage(**vars(last))
                self._minute_flow = 0.0
                self._minute_net.clear()
        self._sealed_flow = 0.0
        self._sealed_net.clear()
        self._sealed = False
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
