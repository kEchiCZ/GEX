"""Minutové bary podkladu z tisků tasty — záložní bar minuty (#1772, ADR-0025 dodatek).

Při výpadku real-time barů IBKR (`reqRealTimeBars`) dostane bar minuty každý
konzument pipeline (partice, push `price.{sym}`, detektor setupů, sondy T9,
paper) z agregace tisků dxFeed `TimeAndSale` front future, které engine už
odebírá kvůli CVD podkladu (#829) a fallbacku spotu (#614). Žádná další
subskripce ani spojení (ADR-0027 platí beze změny).

Měření 23. 11. 2026 v RTH (#1772): 32/32 minut ES i NQ, high a low vždy shodné
s IBKR, open a close se liší jen hraničním tiskem zařazeným do sousední minuty
(ES ≤ 1 tik, NQ ≤ 8 tiků, medián 0), objem ±0,2 %; po M:10 nedorazil žádný
tisk minuty M−1 (0 ze 173 762), takže bar je v čase cyklu konečný.

Minuta tisku se bere z jeho času (`time` z burzy), ne z příchodu: cyklus v M:10
uzavírá minutu M−1, zatímco tisky minuty M už tečou.

**Úplnost** (jinak mezera, ne neúplný bar): minuta M platí jen tehdy, když
- spojení běží bez reconnectu od začátku minuty (`subscribed_at` ≤ M),
- první tisk streameru dorazil nejpozději v začátku minuty (symbol přidaný do
  běžící subskripce, roll kontraktu) a
- tok je doložený až za konec minuty (dorazil tisk s časem ≥ M + 1 min), takže
  stream během minuty neutichl.
Při 23. 11. naměřené první minutě dávky (připojeno 16:13:27) chyběla polovina
objemu a open byl o 24 tiků jinde — přesně případ, který tahle pravidla vyřadí.
"""

import datetime as dt
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from gexlens_engine.ibkr.underlying import Bar, contract_mismatch
from gexlens_engine.storage.parquet_store import BAR_SOURCE_TASTY_TRADES

logger = logging.getLogger(__name__)

#: Minuta bez odběru (symbol bez běžící pipeline) se po této době zahodí
_BUCKET_KEEP = dt.timedelta(minutes=5)
#: Kolik posledních párů IBKR × tasty drží stráž kontraktu (#1232)
AGREEMENT_PAIRS = 30


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class _Bucket:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FuturesMinuteBar:
    """Bar minuty z tisků tasty se streamerem, ze kterého vznikl."""

    bar: Bar
    streamer: str


class FuturesBarAggregator:
    """Agregace tisků front futures na minutové bary podle času tisku.

    Sleduje jen registrované streamer symboly; týmž callbackem tečou i opční
    tisky, ty projdou bez práce. Čistá třída bez IO, hodiny jsou injektované.
    """

    def __init__(self, clock: Callable[[], dt.datetime] = _utc_now) -> None:
        self._clock = clock
        #: streamer → instrument a obráceně (jeden front future per instrument)
        self._symbols: dict[str, str] = {}
        self._streamers: dict[str, str] = {}
        self._buckets: dict[str, dict[dt.datetime, _Bucket]] = {}
        #: Příchod prvního tisku streameru (hodiny enginu)
        self._first_print_at: dict[str, dt.datetime] = {}
        #: Nejpozdější čas tisku (z burzy), který dorazil
        self._last_print_ts: dict[str, dt.datetime] = {}
        #: Poslední minuta, kterou si pipeline už vzala — pozdější tisk do ní je pozdní
        self._taken: dict[str, dt.datetime] = {}
        self.late_prints = 0
        self.spread_legs = 0

    def register(self, symbol: str, streamer: str) -> None:
        """Naváže streamer front future na instrument; roll odpojí starý streamer."""
        if self._streamers.get(symbol) == streamer:
            return
        old = self._streamers.get(symbol)
        if old is not None:
            for state in (
                self._symbols,
                self._buckets,
                self._first_print_at,
                self._last_print_ts,
                self._taken,
            ):
                state.pop(old, None)
            logger.info("Bary podkladu %s z tasty: roll streameru %s → %s", symbol, old, streamer)
        self._streamers[symbol] = streamer
        self._symbols[streamer] = symbol

    def on_event(self, event_type: str, values: list[object]) -> None:
        """EventCallback DxLinkStreamu — pořadí polí dle `tasty.stream.EVENT_FIELDS`
        (`eventSymbol, time, price, size, aggressorSide, spreadLeg, …`)."""
        if event_type != "TimeAndSale" or not values:
            return
        streamer = str(values[0])
        if streamer not in self._symbols:
            return
        self._first_print_at.setdefault(streamer, self._clock())
        if len(values) > 5 and _flag(values[5]):
            # Noha spreadu nese cenu spreadu, ne kontraktu
            self.spread_legs += 1
            return
        time_ms = _number(values[1]) if len(values) > 1 else None
        price = _number(values[2]) if len(values) > 2 else None
        size = _number(values[3]) if len(values) > 3 else None
        if time_ms is None or time_ms <= 0 or price is None or price <= 0 or size is None:
            return
        ts = dt.datetime.fromtimestamp(time_ms / 1000.0, tz=dt.UTC)
        last = self._last_print_ts.get(streamer)
        if last is None or ts > last:
            self._last_print_ts[streamer] = ts
        minute = ts.replace(second=0, microsecond=0)
        taken = self._taken.get(streamer)
        if taken is not None and minute <= taken:
            self.late_prints += 1
            return
        buckets = self._buckets.setdefault(streamer, {})
        bucket = buckets.get(minute)
        if bucket is None:
            for old in [key for key in buckets if key < minute - _BUCKET_KEEP]:
                del buckets[old]
            buckets[minute] = _Bucket(price, price, price, price, size)
            return
        bucket.high = max(bucket.high, price)
        bucket.low = min(bucket.low, price)
        bucket.close = price
        bucket.volume += size

    def take(
        self, symbol: str, minute: dt.datetime, subscribed_at: dt.datetime | None
    ) -> FuturesMinuteBar | None:
        """Bar minuty `minute` (začátek minuty, UTC), je-li úplný; jinak None.

        Volá ho cyklus pipeline každou minutu (i když IBKR dodává — páry pro
        stráž kontraktu a hodinovou shodu). Minuta se tím uzavře: tisk, který
        do ní dorazí později, se jen započítá do `late_prints`.
        """
        streamer = self._streamers.get(symbol)
        if streamer is None:
            return None
        buckets = self._buckets.get(streamer, {})
        bucket = buckets.pop(minute, None)
        for old in [key for key in buckets if key < minute]:
            del buckets[old]
        taken = self._taken.get(streamer)
        if taken is None or minute > taken:
            self._taken[streamer] = minute
        if bucket is None:
            return None
        first = self._first_print_at.get(streamer)
        last = self._last_print_ts.get(streamer)
        complete = (
            subscribed_at is not None
            and subscribed_at <= minute
            and first is not None
            and first <= minute
            and last is not None
            and last >= minute + dt.timedelta(minutes=1)
        )
        if not complete:
            return None
        bar = Bar(
            ts=minute,
            open=bucket.open,
            high=bucket.high,
            low=bucket.low,
            close=bucket.close,
            volume=bucket.volume,
            source=BAR_SOURCE_TASTY_TRADES,
        )
        return FuturesMinuteBar(bar=bar, streamer=streamer)

    def status_fields(self) -> dict[str, object]:
        """Diagnostika do /status: pozdní tisky po uzavření minuty a nohy spreadů."""
        return {"futures_bars": {"late_prints": self.late_prints, "spread_legs": self.spread_legs}}


@dataclass
class BarAgreement:
    """Shoda barů IBKR × tasty na týchž minutách — stráž kontraktu a hodinový log.

    Stráž (#1232): bar z tasty se za výpadku IBKR použije, jen když posledních
    `AGREEMENT_PAIRS` párů nehlásí jiný kontrakt (`contract_mismatch` > tolerance).
    Málo párů (start bez IBKR, roll streameru tasty) = nelze rozhodnout → bar
    se použije, stejně jako u doplnění svíček (`_fill_bar_gaps`).
    """

    _streamer: str | None = None
    _pairs: deque[tuple[dt.datetime, float, Bar]] = field(
        default_factory=lambda: deque(maxlen=AGREEMENT_PAIRS)
    )
    # Hodinové počitadlo
    _count: int = 0
    _exact: int = 0
    _max_diff: dict[str, float] = field(default_factory=dict)
    _volume_ratios: list[float] = field(default_factory=list)

    def add(self, measured: Bar, tasty: FuturesMinuteBar) -> None:
        """Zapíše pár téže minuty (IBKR změřený, tasty z tisků)."""
        if tasty.streamer != self._streamer:
            self._streamer = tasty.streamer
            self._pairs.clear()
        self._pairs.append((measured.ts, measured.close, tasty.bar))
        self._count += 1
        fields = ("open", "high", "low", "close")
        diffs = {name: abs(getattr(measured, name) - getattr(tasty.bar, name)) for name in fields}
        if all(diff == 0 for diff in diffs.values()):
            self._exact += 1
        for name, diff in diffs.items():
            self._max_diff[name] = max(self._max_diff.get(name, 0.0), diff)
        if measured.volume > 0:
            self._volume_ratios.append(tasty.bar.volume / measured.volume)

    def contract_deviation(self, streamer: str) -> float | None:
        """Medián relativní odchylky close tasty od IBKR přes poslední páry; None = nelze."""
        if streamer != self._streamer or not self._pairs:
            return None
        measured = {ts: close for ts, close, _ in self._pairs}
        return contract_mismatch(measured, [bar for _, _, bar in self._pairs])

    def hourly_summary(self) -> str | None:
        """Text shody za uplynulé období a vynulování; None = žádný pár."""
        if self._count == 0:
            return None
        ratios = sorted(self._volume_ratios)
        ratio = (
            f"objem tasty/IBKR medián {ratios[len(ratios) // 2]:.3f} "
            f"({ratios[0]:.3f}–{ratios[-1]:.3f})"
            if ratios
            else "objem IBKR 0"
        )
        diff = ", ".join(f"{name} {value:g}" for name, value in self._max_diff.items())
        text = (
            f"párů {self._count}, přesná shoda OHLC {self._exact}/{self._count}, "
            f"max odchylka v bodech: {diff}; {ratio}"
        )
        self._count = 0
        self._exact = 0
        self._max_diff = {}
        self._volume_ratios = []
        return text


def _flag(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN → None
