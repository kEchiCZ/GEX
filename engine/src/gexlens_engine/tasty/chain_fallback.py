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

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import (
    FEED_TASTY,
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
) -> dict[OptionContractSpec, CachedQuote]:
    """Cache kotací poskládaná z tasty stavů — tvarem shodná se `scheduler.quotes()`.

    Kontrakt se vezme jen s ČERSTVOU kotací i greeks (ADR-0025 pravidlo 2:
    vlastník dodá hodnotu celou, nebo nedodá nic). Chybějící kontrakt se
    prostě nevrátí — runtime takový spec přeskakuje stejně jako u IBKR,
    takže se nikde nemusí řešit zvláštní případ.

    `updated_at` odpovídá stáří NEJSTARŠÍ použité hodnoty, ne okamžiku „teď":
    stáří kotace se propisuje do `stale_age` snímku i do prahu
    `quote_max_age_s`, takže tvrdit u dvě minuty staré tasty hodnoty nulové
    stáří by obešlo ochranu #306.
    """
    if chain is None:
        return {}
    quotes: dict[OptionContractSpec, CachedQuote] = {}
    for spec in specs:
        streamer = chain.streamer_symbol(spec)
        if streamer is None:
            continue
        state = cache.state(streamer)
        if state is None:
            continue
        quote, greeks = state.quote, state.greeks
        if quote.updated_at is None or greeks.updated_at is None:
            continue
        quote_age_ms = (now_utc_ts - quote.updated_at.timestamp()) * 1000
        greeks_age_ms = (now_utc_ts - greeks.updated_at.timestamp()) * 1000
        if quote_age_ms > max_age_ms or greeks_age_ms > max_age_ms:
            continue
        if quote.bid is None or quote.ask is None:
            continue
        if greeks.iv is None or greeks.delta is None or greeks.gamma is None:
            continue
        if greeks.theta is None or greeks.vega is None:
            continue
        oldest_age_s = max(quote_age_ms, greeks_age_ms) / 1000
        quotes[spec] = CachedQuote(
            snapshot=QuoteSnapshot(
                bid=quote.bid,
                ask=quote.ask,
                # Denní objem ani poslední cenu z odebíraných dxFeed eventů
                # nejde získat ve stejné sémantice jako z IBKR — viz modul
                last=None,
                volume=None,
                iv=greeks.iv,
                delta=greeks.delta,
                gamma=greeks.gamma,
                theta=greeks.theta,
                vega=greeks.vega,
            ),
            updated_at=now_monotonic - oldest_age_s,
            stale=False,
            # Greeks jsou měřené dxFeedem, ne dopočtené naším BS modelem
            source=GREEKS_SOURCE_MODEL,
            feed=FEED_TASTY,
        )
    return quotes
