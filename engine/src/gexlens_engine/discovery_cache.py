"""Cache posledního úspěšného IBKR discovery per symbol (#1153, varianta A).

Založení pipeline stálo stoprocentně na IBKR sec-def farmě: front futures
(`reqContractDetails`), opční řetěz (`reqSecDefOptParams`) a úvodní spot
z IBKR tickeru. Když IBKR vypadne dřív, než se pipeline založí (souběh
s mobilem = Error 1100, restart enginu během výpadku), tasty větev má spot,
řetěz, OI i svíčky, ale nemá co obsloužit — 14. 9. 2026 tak NQ stál celé
odpoledne bez grafu, zatímco ES (založený minutu před výpadkem) jel z tasty.

Řešení: poslední úspěšné discovery se uloží do JSON v `derived/` a při
selhání IBKR se z něj pipeline založí („degradovaný start"). IBKR převezme
po zotavení stejnými fallbacky jako u běžící pipeline (#614). Cache je
**jen pro start** — nic z ní se nemíchá do dat (ADR-0025 pravidlo 2) a
degradace je viditelná (alert + log, pravidlo 5).

Co cache zvládne a co ne: expirace jsou z IBKR známé týdny dopředu, takže
i pár dní starý záznam obsahuje dnešní 0DTE; strikes se od discovery
mohou lišit — pásmo se roztahuje za běhu (ADR-0002) a chybějící vzdálené
striky doplní další úspěšné discovery. Bez jediného úspěšného discovery
v historii (čistá instalace) fallback není.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from gexlens_engine.ibkr.discovery import ExpiryInfo

logger = logging.getLogger(__name__)

#: Starší záznam se nepoužije: striky i tradingClass mohly za tu dobu odjet
MAX_AGE_DAYS = 14
CACHE_FILENAME = "discovery_cache.json"


@dataclass(frozen=True)
class FrontFuture:
    """Front futures kontrakt tak, jak ho vrátilo IBKR discovery."""

    symbol: str
    con_id: int
    exchange: str
    multiplier: str
    last_trade_date: str  # YYYYMMDD
    local_symbol: str
    trading_class: str


@dataclass(frozen=True)
class CachedDiscovery:
    symbol: str
    stored_at: dt.datetime
    front: FrontFuture
    expiries: tuple[ExpiryInfo, ...]

    def unexpired(self, today: dt.date) -> tuple[ExpiryInfo, ...]:
        """Expirace, které dnes ještě platí (řazení discovery = podle expirace)."""
        return tuple(info for info in self.expiries if info.expiry >= today.strftime("%Y%m%d"))


class DiscoveryCache:
    """JSON soubor {symbol: záznam}; zápis celý (soubory jsou malé, symbolů pár)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read_all(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Discovery cache %s nejde načíst (%s) — ignoruje se", self._path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def store(self, front: FrontFuture, expiries: list[ExpiryInfo]) -> None:
        """Přepíše záznam symbolu; selhání zápisu se jen zaloguje (start nesmí spadnout)."""
        data = self._read_all()
        data[front.symbol] = {
            "stored_at": dt.datetime.now(dt.UTC).isoformat(),
            "front": asdict(front),
            "expiries": [
                {
                    "trading_class": info.trading_class,
                    "expiry": info.expiry,
                    "exchange": info.exchange,
                    "multiplier": info.multiplier,
                    "strikes": list(info.strikes),
                }
                for info in expiries
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Discovery cache %s nejde zapsat: %s", self._path, exc)

    def load(self, symbol: str, *, today: dt.date) -> CachedDiscovery | None:
        """Záznam symbolu, pokud není starší než MAX_AGE_DAYS a má dnešní expiraci."""
        raw = self._read_all().get(symbol)
        if not isinstance(raw, dict):
            return None
        try:
            stored_at = dt.datetime.fromisoformat(str(raw["stored_at"]))
            front_raw = raw["front"]
            front = FrontFuture(
                symbol=str(front_raw["symbol"]),
                con_id=int(front_raw["con_id"]),
                exchange=str(front_raw["exchange"]),
                multiplier=str(front_raw["multiplier"]),
                last_trade_date=str(front_raw["last_trade_date"]),
                local_symbol=str(front_raw["local_symbol"]),
                trading_class=str(front_raw["trading_class"]),
            )
            expiries = tuple(
                ExpiryInfo(
                    trading_class=str(item["trading_class"]),
                    expiry=str(item["expiry"]),
                    exchange=str(item["exchange"]),
                    multiplier=str(item["multiplier"]),
                    strikes=tuple(float(value) for value in item["strikes"]),
                )
                for item in raw["expiries"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Discovery cache %s: záznam %s je poškozený (%s)", self._path, symbol, exc
            )
            return None
        if stored_at.tzinfo is None:
            stored_at = stored_at.replace(tzinfo=dt.UTC)
        if (dt.datetime.now(dt.UTC) - stored_at).days > MAX_AGE_DAYS:
            return None
        cached = CachedDiscovery(symbol=symbol, stored_at=stored_at, front=front, expiries=expiries)
        if not cached.unexpired(today):
            return None
        # Front kontrakt po expiraci je k ničemu — spot by ukazoval mrtvý kontrakt
        if front.last_trade_date < today.strftime("%Y%m%d"):
            return None
        return cached
