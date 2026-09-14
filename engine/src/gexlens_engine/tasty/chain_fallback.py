"""Fallback celého opčního řetězu na tastytrade (#614 fáze 2b).

Fáze 2a zachránila cenu podkladu, ale heatmapa i GEX stojí na řetězu — při
souběhu s mobilem (error 10197) tedy graf pořád zamrzl, jen se pod ním hýbala
cena. Tahle vrstva dodá při výpadku IBKR i řetěz: kotace, greeks a OI.

Spouštěč se NEvymýšlí znovu. Detektor #517 fáze A už každou minutu počítá,
na kolika kontraktech mlčí jen IBKR, a jeho prahy jsou měřené na 3 016
minutách historie (70 % kontraktů, 3 minuty v řadě). Stav `ibkr_suspect` je
přesně „IBKR mlčí, tasty data má" — tedy podmínka fallbacku. Fallback tak
dědí kalibraci fáze 1, jak žádá DoD #614.

Pravidla ADR-0025, která tu platí:

* **2 — žádné mergování.** Kontrakt se převezme z tasty jen celý; bez čerstvé
  kotace NEBO bez čerstvých greeks se vynechá úplně. Nikdy bid z IBKR
  a gamma z tasty.
* **3 — přepnutí jen na hranici snímku.** Verdikt chodí jednou za minutu ze
  shadow smyčky, takže přepnutí padne mezi cykly, ne doprostřed výpočtu.
* **5 — hystereze a viditelný stav.** Návrat vyžaduje souvislou čistou sérii;
  tiché přepnutí je zakázané, proto `switched` a zpráva pro alert i /status.

Co fallback vědomě NEdodá: kumulativní denní objem. Z něj se počítá CumΔ
a net objem, a tasty ho ve stejné sémantice nemá (viz `QuoteSnapshot`).
Během fallbacku proto tyhle řady stojí a v snímku jsou `None` — díra, kterou
je vidět, místo nuly, která lže (#465).
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.compute.gexfield import fallback_greeks
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import (
    FEED_TASTY,
    GREEKS_SOURCE_COMPUTED,
    GREEKS_SOURCE_MODEL,
    CachedQuote,
    QuoteSnapshot,
)
from gexlens_engine.tasty.crosscheck import CrossCheckVerdict
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

logger = logging.getLogger(__name__)

#: Kolik čistých minut V ŘADĚ vrátí řetěz zpět na IBKR. Delší než tři minuty,
#: kterými se fallback zapíná: přepnutí zdroje celého řetězu překreslí profil,
#: takže kmitání sem a tam stojí víc než o pár minut pozdější návrat.
DEFAULT_RECOVER_MINUTES = 5

#: Max stáří tasty hodnoty vůči okamžiku snímku. Shodné s `shadow.MAX_AGE_MS`,
#: aby se fallback rozhodoval nad týmiž daty, jaká měří porovnání.
MAX_AGE_MS = 120_000

ChainSourceName = Literal["ibkr", "tasty"]


@dataclass(frozen=True)
class ChainDecision:
    """Zdroj řetězu pro nadcházející snímek; `switched` je hrana, ne stav."""

    source: ChainSourceName
    switched: bool = False
    message: str = ""


class ChainFallback:
    """Stavový automat nad verdikty křížové kontroly — kdo dodává řetěz.

    Instance je JEDNA pro celý engine, ne per symbol: market data lines jsou
    vlastnost účtu, takže výpadek IBKR (10197 i pád farmy) bere ES i NQ naráz
    a rozdělený stav by znamenal jen dvě různá místa, kde se to pokazí.
    """

    def __init__(self, *, recover_minutes: int = DEFAULT_RECOVER_MINUTES) -> None:
        self._recover_minutes = max(1, recover_minutes)
        self._source: ChainSourceName = "ibkr"
        self._clean_streak = 0

    @property
    def active_source(self) -> ChainSourceName:
        return self._source

    def observe(self, verdict: CrossCheckVerdict) -> ChainDecision:
        """Zpracuje minutový verdikt křížové kontroly a vrátí zdroj pro další snímek."""
        if verdict.state == "ibkr_suspect":
            self._clean_streak = 0
            if self._source == "ibkr":
                self._source = "tasty"
                return ChainDecision(
                    source="tasty",
                    switched=True,
                    message="Opční řetěz přebírá tastytrade — " + verdict.message.rstrip("."),
                )
            return ChainDecision(source="tasty")

        if self._source == "ibkr":
            return ChainDecision(source="ibkr")

        # Za fallbacku se vracíme jen po skutečně čisté minutě. `state == "ok"`
        # samo nestačí: detektor ho vrací i pro minutu NAD prahem, která zatím
        # nenaplnila sérii do alertu (`streak > 0`) — návrat na takové minutě
        # by řetěz přepínal přesně v okamžiku, kdy se IBKR zase kazí.
        if verdict.state == "ok" and verdict.streak == 0:
            self._clean_streak += 1
            if self._clean_streak >= self._recover_minutes:
                self._source = "ibkr"
                self._clean_streak = 0
                return ChainDecision(
                    source="ibkr",
                    switched=True,
                    message="Opční řetěz zpět na IBKR — feed se zotavil",
                )
            return ChainDecision(source="tasty")

        # `quiet` (mlčí oba) ani `insufficient` (málo kontraktů) o zdraví IBKR
        # nic neříkají — sérii jen nulují, fallback drží. Tichý trh nesmí
        # vypadat jako uzdravení.
        self._clean_streak = 0
        return ChainDecision(source="tasty")


def tasty_chain_quotes(
    specs: Sequence[OptionContractSpec],
    chain: ChainSymbols | None,
    cache: TastyChainCache,
    *,
    now_utc_ts: float,
    now_monotonic: float,
    max_age_ms: int = MAX_AGE_MS,
    stream_alive_ts: float | None = None,
    spot: float | None = None,
) -> dict[OptionContractSpec, CachedQuote]:
    """Cache kotací poskládaná z tasty stavů — tvarem shodná se `scheduler.quotes()`.

    Kontrakt se vezme jen s kotací i greeks pohromadě (ADR-0025 pravidlo 2:
    vlastník dodá hodnotu celou, nebo nedodá nic). Chybějící kontrakt se
    prostě nevrátí — runtime takový spec přeskakuje stejně jako u IBKR,
    takže se nikde nemusí řešit zvláštní případ.

    Stáří (#306 vs. dxFeed): dxFeed posílá Quote/Greeks JEN při změně
    (event-on-change, žádné periodické snímky), takže kotace deep OTM striku
    nezměněná 20 min je na živém streamu pořád aktuální. Bez `stream_alive_ts`
    platí přísné pravidlo „hodnota mladší než `max_age_ms`" (14. 9. 2026 tím
    ES při fallbacku ztrácel 44 ze 160 kontraktů a zdi z OI těch striků
    zmizely). Se `stream_alive_ts` (UTC ts posledního eventu celého streamu)
    se za stáří bere ŽIVOST STREAMU: mrtvý stream (> max_age) = nic, živý
    stream = všechny kontrakty s hodnotami, `updated_at` nese stáří streamu,
    takže `stale_age` snímku i práh `quote_max_age_s` dál něco měří.

    Greeks: dxFeed Greeks na řídkých / deep OTM sériích nechodí (#810) — se
    `spot` se pro kontrakt s kotací bez greeks dopočtou vlastní BS greeks
    z mid (#547, `fallback_greeks`, `source = computed`), stejně jako to dělá
    IBKR cesta i extended expirace (#616). Bez spotu se kontrakt vynechá celý
    (ADR-0025 pravidlo 2: půl hodnoty odjinud nikdy).
    """
    if chain is None:
        return {}
    stream_age_ms: float | None = None
    if stream_alive_ts is not None:
        stream_age_ms = (now_utc_ts - stream_alive_ts) * 1000
        if stream_age_ms > max_age_ms:
            return {}  # stream mrtvý — poslední hodnoty už nikdo nepotvrzuje
    quotes: dict[OptionContractSpec, CachedQuote] = {}
    now_utc = dt.datetime.fromtimestamp(now_utc_ts, tz=dt.UTC)
    settle_by_expiry: dict[str, dt.datetime] = {}
    for spec in specs:
        streamer = chain.streamer_symbol(spec)
        if streamer is None:
            continue
        state = cache.state(streamer)
        if state is None:
            continue
        quote, greeks = state.quote, state.greeks
        if quote.updated_at is None or quote.bid is None or quote.ask is None:
            continue
        quote_age_ms = (now_utc_ts - quote.updated_at.timestamp()) * 1000
        greeks_age_ms = (
            (now_utc_ts - greeks.updated_at.timestamp()) * 1000
            if greeks.updated_at is not None
            else None
        )
        if stream_age_ms is not None:
            # Živý stream potvrzuje i nezměněné hodnoty — stáří = stáří streamu
            quote_age_ms = stream_age_ms
            if greeks_age_ms is not None:
                greeks_age_ms = stream_age_ms
        elif quote_age_ms > max_age_ms:
            continue
        greeks_ok = (
            greeks_age_ms is not None
            and greeks_age_ms <= max_age_ms
            and greeks.iv is not None
            and greeks.delta is not None
            and greeks.gamma is not None
            and greeks.theta is not None
            and greeks.vega is not None
        )
        source = GREEKS_SOURCE_MODEL
        if greeks_ok:
            assert greeks.iv is not None and greeks.delta is not None  # greeks_ok
            assert greeks.gamma is not None and greeks.theta is not None
            assert greeks.vega is not None
            iv, delta, gamma = greeks.iv, greeks.delta, greeks.gamma
            theta, vega = greeks.theta, greeks.vega
            oldest_age_s = max(quote_age_ms, greeks_age_ms or 0.0) / 1000
        else:
            if spot is None or quote.bid <= 0.0 or quote.ask < quote.bid:
                continue
            settle = settle_by_expiry.get(spec.expiry)
            if settle is None:
                settle = settle_ts(dt.datetime.strptime(spec.expiry, "%Y%m%d").date())
                settle_by_expiry[spec.expiry] = settle
            computed = fallback_greeks(
                spot=spot,
                strike=spec.strike,
                right=spec.right,
                mid=(quote.bid + quote.ask) / 2.0,
                settle=settle,
                now=now_utc,
            )
            if computed is None:
                continue  # mimo no-arbitrage pásmo / nekonvergence — díra, ne výmysl
            iv, delta, gamma = computed.iv, computed.delta, computed.gamma
            theta, vega = computed.theta, computed.vega
            source = GREEKS_SOURCE_COMPUTED
            oldest_age_s = quote_age_ms / 1000
        quotes[spec] = CachedQuote(
            snapshot=QuoteSnapshot(
                bid=quote.bid,
                ask=quote.ask,
                # Denní objem ani poslední cenu z odebíraných dxFeed eventů
                # nejde získat ve stejné sémantice jako z IBKR — viz modul
                last=None,
                volume=None,
                iv=iv,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
            ),
            updated_at=now_monotonic - oldest_age_s,
            stale=False,
            # model = měřené dxFeedem; computed = vlastní BS z mid (#547)
            source=source,
            feed=FEED_TASTY,
        )
    return quotes
