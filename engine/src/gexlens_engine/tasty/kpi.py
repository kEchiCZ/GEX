"""KPI stability tastytrade/dxFeed streamu (#1214).

Rozhodnutí B v #610 (17. 9. 2026): tasty zůstává rozšíření/fallback, dokud
stream není měřitelně stabilní. Tenhle modul dva týdny sbírá per obchodní
seanci to, co dnes existuje jen jako okamžité počítadlo v `/status`:

| KPI | zdroj | cíl pro B2 |
|---|---|---|
| výpadky spojení / seance | `DxLinkStream.reconnects` | ≤ 2 |
| rate-limit události / seance a minuty v rate limitu | `rate_limited`, `heals` | 0 po startu |
| podíl RTH minut bez jediného eventu | `last_event_at` | < 0,5 % |
| podíl RTH minut, kde tasty mlčí u > 5 % společných kontraktů | křížová kontrola (#517) | < 1 % |
| podíl sledovaných symbolů s tasty greeks (RTH, průměr) | `field_counts()["greeks"]` | ≥ 95 % |

Vstupy jsou hotová čísla z minutového cyklu (žádné vlastní I/O na stream),
uzavřená seance jde jako řádek do `data/reports/tasty-kpi.jsonl` a do logu.
Rozpracovaná seance se po každém vzorku ukládá do `tasty-kpi-current.json`
a při startu načte: restart enginu uprostřed seance (18. 9. 2026 23:05 →
řádek s `rth_minutes 0`) by jinak zahodil celou RTH část dne.
Čistá logika bez závislostí na síti — testuje se syntetickými vzorky.
"""

import datetime as dt
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from gexlens_engine.compute.marketclock import outside_us_rth
from gexlens_engine.compute.settle import trading_session_date

logger = logging.getLogger(__name__)

#: Minuta bez eventu = poslední event starší než tohle (60 s = KEEPALIVE serveru)
SILENT_AFTER_S = 60.0
#: Minuta „tasty mrtvá“ = tasty strana mlčí u víc než tohoto podílu společných kontraktů
TASTY_DEAD_SHARE_MIN = 0.05

#: Cíle z #1214 — vyhodnocení „prošlo“ per seance
TARGET_MAX_DROPS = 2
TARGET_MAX_RATE_LIMIT = 0
TARGET_MAX_SILENT_SHARE = 0.005
TARGET_MAX_DEAD_SHARE = 0.01
TARGET_MIN_GREEKS_SHARE = 0.95


@dataclass
class SessionKpi:
    """Agregát jedné obchodní seance (Globex den, ADR-0023)."""

    session: str
    minutes: int = 0
    rth_minutes: int = 0
    rth_silent: int = 0
    rth_tasty_dead: int = 0
    drops: int = 0
    rate_limit_events: int = 0
    rate_limit_minutes: int = 0
    heals: int = 0
    errors: int = 0
    greeks_share_sum: float = 0.0
    greeks_share_n: int = 0
    disconnected_minutes: int = 0
    first_minute: str | None = None
    last_minute: str | None = None

    @property
    def silent_share(self) -> float | None:
        return self.rth_silent / self.rth_minutes if self.rth_minutes else None

    @property
    def tasty_dead_share(self) -> float | None:
        return self.rth_tasty_dead / self.rth_minutes if self.rth_minutes else None

    @property
    def greeks_share(self) -> float | None:
        return self.greeks_share_sum / self.greeks_share_n if self.greeks_share_n else None

    def verdicts(self) -> dict[str, bool | None]:
        """Per KPI: prošlo (True), neprošlo (False), neměřeno (None)."""
        silent = self.silent_share
        dead = self.tasty_dead_share
        greeks = self.greeks_share
        return {
            "drops": self.drops <= TARGET_MAX_DROPS,
            "rate_limit": self.rate_limit_events <= TARGET_MAX_RATE_LIMIT,
            "silent": None if silent is None else silent <= TARGET_MAX_SILENT_SHARE,
            "tasty_dead": None if dead is None else dead <= TARGET_MAX_DEAD_SHARE,
            "greeks": None if greeks is None else greeks >= TARGET_MIN_GREEKS_SHARE,
        }

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = asdict(self)
        payload["silent_share"] = self.silent_share
        payload["tasty_dead_share"] = self.tasty_dead_share
        payload["greeks_share"] = self.greeks_share
        payload["verdicts"] = self.verdicts()
        payload["passed"] = all(v is not False for v in self.verdicts().values())
        return payload


@dataclass
class _Counters:
    reconnects: int = 0
    rate_limited: int = 0
    heals: int = 0
    errors: int = 0


@dataclass
class StreamKpi:
    """Sběrač KPI: `observe` jednou za minutu z hotových počítadel, seance se uzavírá sama."""

    report_path: Path | None = None
    #: Rozpracovaná seance přes restart (#1214); None = jen v paměti (testy)
    state_path: Path | None = None
    current: SessionKpi | None = None
    last_closed: SessionKpi | None = None
    _seen: _Counters = field(default_factory=_Counters)
    _primed: bool = False
    _last_minute_key: str | None = None

    def __post_init__(self) -> None:
        if self.state_path is not None and self.current is None:
            self.current = _load_session(self.state_path)
            if self.current is not None:
                # Minuta před restartem se nepočítá dvakrát; počítadla streamu
                # začínají od nuly, takže první vzorek jen primuje (jako po startu)
                self._last_minute_key = self.current.last_minute
                logger.info(
                    "tasty KPI: navazuji na rozpracovanou seanci %s (%d min, RTH %d)",
                    self.current.session,
                    self.current.minutes,
                    self.current.rth_minutes,
                )

    def observe(
        self,
        now: dt.datetime,
        *,
        connected: bool,
        reconnects: int,
        rate_limited: int,
        heals: int,
        errors: int,
        last_event_at: dt.datetime | None,
        rate_limit_active: bool,
        tasty_dead_share: float | None,
        greeks_share: float | None,
    ) -> None:
        """Jeden vzorek za minutu (opakované volání v téže minutě je no-op)."""
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        if minute_key == self._last_minute_key:
            return
        self._last_minute_key = minute_key
        session = trading_session_date(now).isoformat()
        if self.current is None or self.current.session != session:
            self._close_session()
            self.current = SessionKpi(session=session, first_minute=minute_key)
        kpi = self.current
        kpi.minutes += 1
        kpi.last_minute = minute_key
        if not connected:
            kpi.disconnected_minutes += 1
        # Počítadla ve streamu jen rostou (reset = restart enginu): první vzorek
        # jen zapamatuje stav, aby se noční historie nepřičetla dnešní seanci
        if self._primed:
            kpi.drops += max(0, reconnects - self._seen.reconnects)
            kpi.rate_limit_events += max(0, rate_limited - self._seen.rate_limited)
            kpi.heals += max(0, heals - self._seen.heals)
            kpi.errors += max(0, errors - self._seen.errors)
        self._seen = _Counters(reconnects, rate_limited, heals, errors)
        self._primed = True
        if rate_limit_active:
            kpi.rate_limit_minutes += 1
        if not outside_us_rth(now):
            kpi.rth_minutes += 1
            silent = last_event_at is None or (now - last_event_at).total_seconds() > SILENT_AFTER_S
            if silent:
                kpi.rth_silent += 1
            if tasty_dead_share is not None and tasty_dead_share > TASTY_DEAD_SHARE_MIN:
                kpi.rth_tasty_dead += 1
            if greeks_share is not None:
                kpi.greeks_share_sum += greeks_share
                kpi.greeks_share_n += 1
        self._save_state()

    def _save_state(self) -> None:
        """Rozpracovaná seance na disk — atomicky (tmp + replace), à minutu pár set B."""
        if self.state_path is None or self.current is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.current), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("tasty KPI: zápis stavu %s selhal (%s)", self.state_path, exc)

    def _close_session(self) -> None:
        if self.current is None:
            return
        closed = self.current
        self.last_closed = closed
        payload = closed.as_dict()
        logger.info(
            "tasty KPI %s: výpadky %d, rate limit %d× (%d min), heal %d, chyby %d, "
            "RTH bez eventu %s, tasty mrtvá %s, greeks %s — %s",
            closed.session,
            closed.drops,
            closed.rate_limit_events,
            closed.rate_limit_minutes,
            closed.heals,
            closed.errors,
            _pct(closed.silent_share),
            _pct(closed.tasty_dead_share),
            _pct(closed.greeks_share),
            "PROŠLO" if payload["passed"] else "NEPROŠLO",
        )
        if self.report_path is not None:
            try:
                self.report_path.parent.mkdir(parents=True, exist_ok=True)
                with self.report_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.warning("tasty KPI: zápis %s selhal (%s)", self.report_path, exc)

    def status_fields(self) -> dict[str, object]:
        """Do `/status.tasty_kpi`: dnešní rozpracovaná seance + poslední uzavřená."""
        return {
            "today": self.current.as_dict() if self.current else None,
            "last_session": self.last_closed.as_dict() if self.last_closed else None,
        }


def _load_session(path: Path) -> SessionKpi | None:
    """Načte rozpracovanou seanci; poškozený/cizí soubor = začít znovu (log, ne pád)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("tasty KPI: stav %s nejde načíst (%s) — začínám od nuly", path, exc)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("session"), str):
        logger.warning("tasty KPI: stav %s má neznámý tvar — začínám od nuly", path)
        return None
    known = {f.name for f in fields(SessionKpi)}
    try:
        return SessionKpi(**{k: v for k, v in raw.items() if k in known})
    except TypeError as exc:
        logger.warning("tasty KPI: stav %s nejde sestavit (%s) — začínám od nuly", path, exc)
        return None


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"
