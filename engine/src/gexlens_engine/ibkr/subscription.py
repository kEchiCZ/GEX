"""Hlídání chyb subskripce market data (#417).

TWS posílá error 354 „Requested market data is not subscribed" per jednotlivý
request. V provozu chodí sporadicky i s platnou subskripcí — při krátkém
výpadku farmy (Warning 2103/2104) neprojde ~1 request ze sweepu a další minuta
ho doplní. Jednotlivý výskyt proto NENÍ závada a nesmí shodit stav spojení.

Teprve shluk znamená, že kontrakt opravdu předplacený nemáme (typicky nový
ticker mimo naše subskripce) — to je stav, o kterém uživatel vědět chce, včetně
toho, KTERÝ kontrakt TWS odmítla. Detektor proto počítá výskyty v klouzavém
okně; po překročení prahu ohlásí alert a na `cooldown_s` se umlčí, aby minutový
sweep neposlal desítky hlášení o téže věci.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# „Requested market data is not subscribed" — per-request, viz docstring modulu
NOT_SUBSCRIBED_ERROR_CODE = 354

# Kolik odlišných kontraktů se vypíše do alertu (zbytek se shrne počtem)
MAX_ALERT_CONTRACTS = 5

# Diagnostika #772: kolik posledních záznamů se drží pro /status a Settings
RECENT_LIMIT = 50
# Okno „za poslední hodinu" — kumulativní číslo od startu nemá měřítko
HOUR_WINDOW_S = 3600.0


@dataclass(frozen=True)
class SubscriptionErrorRecord:
    """Jeden pozorovaný error 354 pro diagnostiku (#772) — ts je epoch UTC."""

    ts: float
    contract: str
    symbol: str


@dataclass(frozen=True)
class SubscriptionErrorAlert:
    """Podklad pro alert `subscription_error` — co a kolikrát TWS odmítla."""

    count: int
    window_s: float
    #: Popisky dotčených kontraktů (nejvýš `MAX_ALERT_CONTRACTS`), seřazené podle četnosti
    contracts: tuple[str, ...]
    #: Kolik odlišných kontraktů se do výpisu nevešlo
    hidden_contracts: int
    #: Nejčastěji dotčený podklad — alerty v UI jsou vázané na symbol
    symbol: str
    message: str


class SubscriptionErrorTracker:
    """Klouzavé okno výskytů error 354; shluk → jeden alert, pak cooldown."""

    def __init__(self, *, threshold: int, window_s: float, cooldown_s: float) -> None:
        self._threshold = threshold
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._events: deque[tuple[float, str, str]] = deque()
        self._last_alert_ts: float | None = None
        self._total = 0
        # Diagnostika #772: posledních N záznamů + hodinové okno. Kumulativní
        # číslo od startu nemá měřítko („23" po dni běhu neříká nic) a bez
        # záznamů se nedalo dohledat, KTERÉ kontrakty a KDY selhaly.
        self._recent: deque[SubscriptionErrorRecord] = deque(maxlen=RECENT_LIMIT)
        self._hour: deque[float] = deque()
        # Omilostněné okno (#772): resubskripce nové seance o půlnoci UTC vyrobí
        # nárazově ~20 chyb — je to očekávaný přechodový stav, ne porucha, a bez
        # výjimky by alertovací práh (#417) pravidelně o půlnoci falešně střílel.
        self._excused_until: float | None = None
        self._excused = 0

    @property
    def total(self) -> int:
        """Celkový počet pozorovaných chyb za běh — diagnostika ve status logu."""
        return self._total

    @property
    def excused(self) -> int:
        """Kolik výskytů spadlo do omilostněného okna resubskripce (#772)."""
        return self._excused

    def excuse(self, duration_s: float, *, now: float) -> None:
        """Omilostni následujících `duration_s` sekund — plánovaná resubskripce.

        Výskyty se dál počítají (total, okno, záznamy), jen neplní alertovací
        práh: přechod seance je očekávaný, ale v diagnostice vidět být má.
        """
        candidate = now + duration_s
        if self._excused_until is None or candidate > self._excused_until:
            self._excused_until = candidate

    def window_count(self, now: float) -> int:
        """Počet výskytů za posledních `HOUR_WINDOW_S` sekund (#772)."""
        while self._hour and now - self._hour[0] > HOUR_WINDOW_S:
            self._hour.popleft()
        return len(self._hour)

    def recent_records(self) -> tuple[SubscriptionErrorRecord, ...]:
        """Posledních až `RECENT_LIMIT` záznamů, nejnovější poslední (#772)."""
        return tuple(self._recent)

    def observe(
        self, contract_label: str, symbol: str, *, now: float, wall_now: float | None = None
    ) -> SubscriptionErrorAlert | None:
        """Jeden výskyt error 354; vrací alert jen při překročení prahu v okně.

        Vrátí `None` i tehdy, když práh překročen je, ale od posledního alertu
        neuplynul `cooldown_s` — jinak by minutový sweep hlásil totéž pořád dokola.
        `now` je monotonic (okna a prahy), `wall_now` epoch pro záznamy v UI.
        """
        self._total += 1
        self._recent.append(
            SubscriptionErrorRecord(
                ts=wall_now if wall_now is not None else time.time(),
                contract=contract_label,
                symbol=symbol,
            )
        )
        self._hour.append(now)
        if self._excused_until is not None and now < self._excused_until:
            # Očekávaná chyba přechodu seance: do prahu se nepočítá vůbec —
            # kdyby jen prošla oknem, doznívající náraz by práh stejně naplnil
            self._excused += 1
            return None
        self._events.append((now, contract_label, symbol))
        while self._events and now - self._events[0][0] > self._window_s:
            self._events.popleft()
        if len(self._events) < self._threshold:
            return None
        if self._last_alert_ts is not None and now - self._last_alert_ts < self._cooldown_s:
            return None
        self._last_alert_ts = now
        return self._build_alert()

    def _build_alert(self) -> SubscriptionErrorAlert:
        counts: dict[str, int] = {}
        symbols: dict[str, int] = {}
        for _, label, symbol in self._events:
            counts[label] = counts.get(label, 0) + 1
            if symbol:
                symbols[symbol] = symbols.get(symbol, 0) + 1
        # Nejčastější kontrakty první; při shodě abecedně, ať je výpis stabilní
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        dominant = sorted(symbols.items(), key=lambda item: (-item[1], item[0]))
        shown = tuple(f"{label} ({hits}×)" for label, hits in ordered[:MAX_ALERT_CONTRACTS])
        hidden = max(0, len(ordered) - MAX_ALERT_CONTRACTS)
        count = len(self._events)
        detail = ", ".join(shown)
        if hidden > 0:
            detail += f" a {hidden} dalších"
        message = (
            f"TWS odmítla market data ({count}× za {self._window_s:g} s, error "
            f"{NOT_SUBSCRIBED_ERROR_CODE} „not subscribed“): {detail}. "
            "Zkontroluj subskripce v Market Data Subscription Manager — pokud je "
            "kontrakt předplacený, jde nejspíš o výpadek TWS farem a data se vrátí sama."
        )
        return SubscriptionErrorAlert(
            count=count,
            window_s=self._window_s,
            contracts=shown,
            hidden_contracts=hidden,
            symbol=dominant[0][0] if dominant else "",
            message=message,
        )


def contract_label(contract: object) -> str:
    """Čitelný popisek kontraktu z ib_async `Contract` (odolný vůči None/neúplným).

    Error 354 bez rozpoznaného kontraktu (ib_async ho pro některé requesty nezná)
    nesmí handler shodit — vrací se zástupné „neznámý kontrakt".
    """
    if contract is None:
        return "neznámý kontrakt"
    symbol = getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")
    if not symbol:
        return "neznámý kontrakt"
    parts = [str(symbol)]
    right = getattr(contract, "right", "")
    strike = getattr(contract, "strike", 0)
    if right and strike:
        parts.append(f"{strike:g}{right}")
    expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
    if expiry:
        parts.append(str(expiry))
    exchange = getattr(contract, "exchange", "")
    if exchange:
        parts.append(f"@{exchange}")
    return " ".join(parts)
