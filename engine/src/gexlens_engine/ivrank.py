"""Kolektor IV Ranku (#871) — tři řady denní IV po vzoru `volregime`.

Jednou po settle (a s backfillem při prvním běhu) doplní za seanci:

* **ibkr** — 30d IV index podkladu (`reqHistoricalData`,
  `whatToShow=OPTION_IMPLIED_VOLATILITY`; sonda 26. 8.: ES 252 barů/rok).
  Historický request, žádná market data linka. První běh stáhne rok zpět,
  další už jen dotažení posledních dnů — restart tak díru sám zacelí.
* **tasty** — hotový IV index + rank + percentile z `/market-metrics`
  (futures potvrzeny sondou 26. 8.). Čísla se PŘEBÍRAJÍ, nepočítají;
  historie roste až ode dneška.
* **own_atm** — vlastní ATM IV z věčného `oi_eod` (#519): expirace nejblíž
  tenoru ~7 dní, strike nejblíž `und_price`, průměr C/P. Nezávislá kotva.

Rank = poloha v min–max klouzavého okna, percentil = podíl dnů pod hodnotou
(`volregime.percentile_of`); obojí per řada, okno 252, pod MIN_SAMPLE se
neurčuje (ADR-0028 — žádný „bezpečný default"). Řady se NIKDY nemíchají:
konstrukce se liší (IBKR 0,119 vs. tasty 0,156 týž den) a společný percentil
by lhal.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.compute.volregime import percentile_of
from gexlens_engine.storage.ivrank_store import (
    SOURCE_IBKR,
    SOURCE_OWN_ATM,
    SOURCE_TASTY,
    IvRankRepository,
)
from gexlens_engine.storage.oi_archive import oi_eod_table

logger = logging.getLogger(__name__)

#: Odklad po settle (vzor #713) — IBKR bar dne musí existovat.
SETTLE_GRACE_MINUTES = 10

#: Klouzavé okno a minimální vzorek pro rank/percentil (vzor ADR-0028).
WINDOW_DAYS = 252
MIN_SAMPLE = 60

#: Tenor vlastní ATM řady — expirace nejblíž 7 kalendářním dnům. 0DTE IV
#: intradenně kolabuje k nule a mezi dny se nedá srovnávat; týdenní tenor
#: je nejkratší stabilní, který věčný archiv (5–10 expirací) vždy nese.
OWN_ATM_TENOR_DAYS = 7

#: Kolik posledních dnů dotahuje denní IBKR request (díry po restartech).
IBKR_TOPUP_DURATION = "10 D"
IBKR_BACKFILL_DURATION = "1 Y"


class TastyMetricsLike(Protocol):
    """Minimální podoba TastySession pro market metrics."""

    async def get_json(self, path: str) -> dict[str, Any]: ...


def rank_of(value: float, history: list[float]) -> float | None:
    """Poloha hodnoty mezi min a max okna (0–1); degenerované okno → None."""
    if not history:
        return None
    low = min(history)
    high = max(history)
    if high <= low:
        return None
    return max(0.0, min(1.0, (value - low) / (high - low)))


def window_context(
    series: list[tuple[dt.date, float]], day: dt.date, value: float
) -> tuple[float | None, float | None, int]:
    """(rank, percentil, vzorek) hodnoty vůči oknu WINDOW_DAYS PŘED dnem.

    Okno se dívá jen dozadu (walk-forward z konstrukce): dnešní hodnota se
    srovnává s minulostí, ne sama se sebou.
    """
    history = [iv for session, iv in series if session < day][-WINDOW_DAYS:]
    if len(history) < MIN_SAMPLE:
        return None, None, len(history)
    return rank_of(value, history), percentile_of(value, history), len(history)


@dataclass
class IvRankCollector:
    """Jednou po settle doplní všechny tři řady; první běh backfilluje IBKR."""

    symbol: str
    repository: IvRankRepository
    db: Engine
    ib: Any | None = None
    tasty: TastyMetricsLike | None = None

    _evaluated_for: dt.date | None = field(default=None, init=False)
    _ibkr_backfilled: bool = field(default=False, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if now < boundary or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus per seance i při chybě
        await self._run(now, session)

    async def _run(self, now: dt.datetime, session: dt.date) -> None:
        for label, step in (
            ("ibkr", self._collect_ibkr),
            ("tasty", self._collect_tasty),
            ("own_atm", self._collect_own_atm),
        ):
            try:
                await step(now, session)
            except Exception:
                logger.exception("IV rank %s: řada %s selhala — pokračuji", self.symbol, label)
        self._log_crosscheck()

    def _log_crosscheck(self) -> None:
        """Rozdíl řad ibkr × tasty do logu (#871 AC) — konstrukce se liší
        a trvalý velký rozdíl by znamenal chybu čtení jedné z nich."""
        latest = {str(row["source"]): row for row in self.repository.latest(self.symbol)}
        ibkr = latest.get(SOURCE_IBKR)
        tasty = latest.get(SOURCE_TASTY)
        if not ibkr or not tasty:
            return
        ibkr_pct = ibkr.get("iv_percentile")
        tasty_pct = tasty.get("iv_percentile")
        logger.info(
            "IV rank %s: křížová kontrola — ibkr IV %.4f (pctl %s) vs. tasty IV %.4f (pctl %s)",
            self.symbol,
            float(str(ibkr["iv"])),
            f"{float(str(ibkr_pct)):.2f}" if ibkr_pct is not None else "—",
            float(str(tasty["iv"])),
            f"{float(str(tasty_pct)):.2f}" if tasty_pct is not None else "—",
        )

    # ── ibkr: 30d IV index podkladu ────────────────────────────────

    async def _front_future(self) -> Any | None:
        from ib_async import Future

        if self.ib is None:
            return None
        details = await self.ib.reqContractDetailsAsync(Future(self.symbol, exchange="CME"))
        today = dt.date.today().strftime("%Y%m%d")
        candidates = sorted(
            (item.contract for item in details if item.contract is not None),
            key=lambda contract: str(contract.lastTradeDateOrContractMonth),
        )
        for contract in candidates:
            if str(contract.lastTradeDateOrContractMonth)[:8] >= today:
                return contract
        return None

    async def _collect_ibkr(self, now: dt.datetime, session: dt.date) -> None:
        if self.ib is None:
            return
        duration = IBKR_TOPUP_DURATION if self._ibkr_backfilled else IBKR_BACKFILL_DURATION
        contract = await self._front_future()
        if contract is None:
            logger.warning("IV rank %s: front future nenalezen", self.symbol)
            return
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="OPTION_IMPLIED_VOLATILITY",
            useRTH=True,
            formatDate=1,
        )
        self._ibkr_backfilled = True
        if not bars:
            logger.warning("IV rank %s: IBKR nevrátila žádné IV bary", self.symbol)
            return
        fresh = {self._bar_date(bar): float(bar.close) for bar in bars if bar.close > 0}
        series = self.repository.series(self.symbol, SOURCE_IBKR)
        merged = dict(series)
        merged.update(fresh)
        ordered = sorted(merged.items())
        existing = {day for day, _ in series}
        written = 0
        for day, iv in fresh.items():
            if day in existing and day != max(fresh):
                continue  # historie se nemění; přepisuje se jen poslední den
            rank, pct, sample = window_context(ordered, day, iv)
            self.repository.upsert(
                session_date=day,
                symbol=self.symbol,
                source=SOURCE_IBKR,
                iv=iv,
                iv_rank=rank,
                iv_percentile=pct,
                sample=sample,
                computed_at=now,
            )
            written += 1
        if written:
            logger.info("IV rank %s: ibkr řada +%d dnů (%s)", self.symbol, written, duration)

    @staticmethod
    def _bar_date(bar: Any) -> dt.date:
        value = bar.date
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])

    # ── tasty: hotový rank z market metrics ────────────────────────

    async def _collect_tasty(self, now: dt.datetime, session: dt.date) -> None:
        if self.tasty is None:
            return
        symbol = f"%2F{self.symbol}"  # /ES — lomítko musí být URL-encoded
        payload = await self.tasty.get_json(f"/market-metrics?symbols={symbol}")
        items = payload.get("data", {}).get("items", [])
        for item in items:
            if item.get("symbol") != f"/{self.symbol}":
                continue
            iv = _num(item.get("implied-volatility-index"))
            rank = _num(item.get("implied-volatility-index-rank"))
            pct = _num(item.get("implied-volatility-percentile"))
            if iv is None:
                return
            self.repository.upsert(
                session_date=session,
                symbol=self.symbol,
                source=SOURCE_TASTY,
                iv=iv,
                iv_rank=rank,
                iv_percentile=pct,
                # Tasty velikost okna nepublikuje — 0 říká „vzorek neznáme"
                sample=0,
                computed_at=now,
            )
            logger.info(
                "IV rank %s: tasty IV %.3f, rank %s, percentile %s",
                self.symbol,
                iv,
                f"{rank:.2f}" if rank is not None else "—",
                f"{pct:.2f}" if pct is not None else "—",
            )
            return

    # ── own_atm: vlastní ATM IV z věčného archivu ──────────────────

    async def _collect_own_atm(self, now: dt.datetime, session: dt.date) -> None:
        record = await asyncio.to_thread(self._own_atm_iv, session)
        if record is None:
            return
        iv = record
        series = self.repository.series(self.symbol, SOURCE_OWN_ATM)
        ordered = sorted({**dict(series), session: iv}.items())
        rank, pct, sample = window_context(ordered, session, iv)
        self.repository.upsert(
            session_date=session,
            symbol=self.symbol,
            source=SOURCE_OWN_ATM,
            iv=iv,
            iv_rank=rank,
            iv_percentile=pct,
            sample=sample,
            computed_at=now,
        )

    def _own_atm_iv(self, session: dt.date) -> float | None:
        """ATM IV expirace nejblíž tenoru ~7 dní; None bez použitelného snímku."""
        stmt = select(
            oi_eod_table.c.expiry,
            oi_eod_table.c.strike,
            oi_eod_table.c.right,
            oi_eod_table.c.iv,
            oi_eod_table.c.und_price,
        ).where(
            oi_eod_table.c.symbol == self.symbol,
            oi_eod_table.c.date == session,
            oi_eod_table.c.iv.is_not(None),
        )
        with self.db.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        if not rows:
            return None
        target = session + dt.timedelta(days=OWN_ATM_TENOR_DAYS)

        def expiry_date(raw: str) -> dt.date | None:
            try:
                return dt.datetime.strptime(str(raw), "%Y%m%d").date()
            except ValueError:
                return None

        expiries = sorted(
            {
                parsed
                for row in rows
                if (parsed := expiry_date(row.expiry)) is not None and parsed >= session
            },
            key=lambda day: abs((day - target).days),
        )
        if not expiries:
            return None
        chosen = expiries[0].strftime("%Y%m%d")
        anchor = next((float(row.und_price) for row in rows if row.und_price is not None), None)
        if anchor is None:
            return None
        by_strike: dict[float, dict[str, float]] = {}
        for row in rows:
            if str(row.expiry) != chosen:
                continue
            by_strike.setdefault(float(row.strike), {})[str(row.right)] = float(row.iv)
        candidates = sorted(
            (
                (strike, sides)
                for strike, sides in by_strike.items()
                if "C" in sides and "P" in sides
            ),
            key=lambda item: abs(item[0] - anchor),
        )
        if not candidates:
            return None
        _, sides = candidates[0]
        return (sides["C"] + sides["P"]) / 2


def _num(raw: object) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
