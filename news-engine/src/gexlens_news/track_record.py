"""Track record — mechanické equity křivky (#298, SPEC 7.3).

Noční sebe-kontrola systému, žádný obchodní signál: bez exekučních nákladů,
point-in-time (S11) — stav dne d se počítá výhradně z closes ≤ d a pozice se
otevírá **na následující open** (vstup na close, ze kterého je stav teprve
spočtený, by byl look-ahead). Kalibrační období je z reportu vyloučené (5.6);
hranice pinnuté v ADR-0021.

Strategie (symbol ES):

* ``buy_hold``   — koupit na open prvního vyhodnocovacího dne a držet,
* ``state``      — long při RiskOn, flat při Neutral, flat/short (konfig.)
  při RiskOff; přepnutí pozice na open dne po potvrzovacím close,
* ``signals_news`` / ``signals_combined`` — vstup na první bar po ts signálu,
  výstup na první bar po expiry_ts; trade se do křivky propíše v den výstupu.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.sentwaves import DailyClose, assess_state, detect_waves
from gexlens_engine.storage.sentiment import sentiment_daily, signals, track_record
from gexlens_news.bars import BarsRepository

logger = logging.getLogger(__name__)

# ADR-0021: vyhodnocovací období začíná, až má adaptivní práh (5.6) z čeho
# žít — aspoň tolik UZAVŘENÝCH vln v každém směru; do té doby je práh 0 a
# stav by stál jen na MA podmínce (in-sample šum, ne kalibrovaný systém)
MIN_WAVES_PER_DIRECTION = 3

STRATEGY_BUY_HOLD = "buy_hold"
STRATEGY_STATE = "state"
STRATEGY_SIGNALS_NEWS = "signals_news"
STRATEGY_SIGNALS_COMBINED = "signals_combined"


@dataclass(frozen=True)
class SessionBar:
    """Denní open/close seance z 1min archivu."""

    date: dt.date
    open: float
    close: float


@dataclass(frozen=True)
class EquityPoint:
    date: dt.date
    equity: float
    drawdown: float


def evaluation_start(
    closes: list[DailyClose], *, min_waves: int = MIN_WAVES_PER_DIRECTION
) -> dt.date | None:
    """První den, kdy má práh potvrzení oboustrannou historii vln (ADR-0021)."""
    waves = detect_waves(closes)
    seen: dict[str, int] = {"RiskOn": 0, "RiskOff": 0}
    for wave in waves:
        if wave.end is None:
            continue
        seen[wave.direction] = seen.get(wave.direction, 0) + 1
        if seen["RiskOn"] >= min_waves and seen["RiskOff"] >= min_waves:
            return wave.end
    return None


def state_positions(
    closes: list[DailyClose],
    *,
    start: dt.date,
    short_riskoff: bool = False,
) -> dict[dt.date, int]:
    """Pozice pro každý den ≥ start: rozhodnutá stavem z PŘEDCHOZÍHO close.

    Point-in-time: stav dne d−1 se počítá jen z closes ≤ d−1 (adaptivní práh
    uvnitř `assess_state` používá jen vlny uzavřené před začátkem aktuální).
    """
    positions: dict[dt.date, int] = {}
    for index in range(1, len(closes)):
        day = closes[index].date
        if day < start:
            continue
        state = assess_state(closes[:index]).state
        if state == "RiskOn":
            positions[day] = 1
        elif state == "RiskOff":
            positions[day] = -1 if short_riskoff else 0
        else:
            positions[day] = 0
    return positions


def equity_curve(
    bars: list[SessionBar],
    positions: dict[dt.date, int] | None = None,
) -> list[EquityPoint]:
    """Equity z denních open/close; `positions=None` = buy & hold.

    Den se změnou pozice se skládá ze dvou úseků: stará pozice drží
    close→open, nová open→close — přesně „vstup na následující open".
    """
    equity = 1.0
    peak = 1.0
    points: list[EquityPoint] = []
    previous_close: float | None = None
    previous_position = 0
    for bar in bars:
        position = 1 if positions is None else positions.get(bar.date, previous_position)
        if previous_close is not None and previous_close > 0:
            equity *= 1 + previous_position * (bar.open / previous_close - 1)
        if bar.open > 0:
            equity *= 1 + position * (bar.close / bar.open - 1)
        peak = max(peak, equity)
        points.append(EquityPoint(date=bar.date, equity=equity, drawdown=equity / peak - 1))
        previous_close = bar.close
        previous_position = position
    return points


@dataclass(frozen=True)
class Trade:
    """Uzavřený obchod signálové strategie — propíše se v den výstupu."""

    exit_date: dt.date
    ret: float


def trades_to_curve(trades: list[Trade], sessions: list[dt.date]) -> list[EquityPoint]:
    """Denní equity ze seznamu obchodů; dny bez výstupu drží hodnotu."""
    by_day: dict[dt.date, list[float]] = {}
    for trade in trades:
        by_day.setdefault(trade.exit_date, []).append(trade.ret)
    equity = 1.0
    peak = 1.0
    points: list[EquityPoint] = []
    for day in sessions:
        for ret in by_day.get(day, []):
            equity *= 1 + ret
        peak = max(peak, equity)
        points.append(EquityPoint(date=day, equity=equity, drawdown=equity / peak - 1))
    return points


class TrackRecordJob:
    """Noční přepočet `track_record` — plný přepis (vstupy jsou immutable)."""

    def __init__(
        self,
        engine: Engine,
        bars: BarsRepository,
        *,
        symbol: str = "ES",
        short_riskoff: bool = False,
    ) -> None:
        self._engine = engine
        self._bars = bars
        self._symbol = symbol
        self._short_riskoff = short_riskoff

    def _daily_closes(self) -> list[DailyClose]:
        stmt = (
            select(sentiment_daily.c.date, sentiment_daily.c.close)
            .where(sentiment_daily.c.symbol == self._symbol)
            .order_by(sentiment_daily.c.date)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [DailyClose(date=row.date, close=float(row.close)) for row in rows]

    def _session_bars(self, start: dt.date, today: dt.date) -> list[SessionBar]:
        out: list[SessionBar] = []
        for day in self._bars.sessions(self._symbol):
            # Dnešek je rozjetá seance — nepatří do denní křivky
            if day < start or day >= today:
                continue
            bars = self._bars.load_day(self._symbol, day)
            if not bars:
                continue
            out.append(SessionBar(date=day, open=float(bars[0].open), close=float(bars[-1].close)))
        return out

    def _signal_trades(self, mode: str, start: dt.date, today: dt.date) -> list[Trade]:
        stmt = (
            select(signals.c.ts, signals.c.direction, signals.c.expiry_ts)
            .where(signals.c.symbol == self._symbol, signals.c.mode == mode)
            .order_by(signals.c.ts)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        trades: list[Trade] = []
        for row in rows:
            ts = _as_utc(row.ts)
            expiry = _as_utc(row.expiry_ts)
            if ts.date() < start or expiry >= dt.datetime.combine(today, dt.time(), dt.UTC):
                continue  # mimo vyhodnocovací období, nebo trade ještě běží
            window = self._bars.load_range(self._symbol, ts, expiry + dt.timedelta(minutes=30))
            entry = next((bar for bar in window if _as_utc(bar.ts) >= ts), None)
            exit_bar = None
            for bar in window:
                if _as_utc(bar.ts) >= expiry:
                    exit_bar = bar
                    break
                exit_bar = bar  # poslední dostupný před expirací (díra v datech)
            if entry is None or exit_bar is None or float(entry.open) <= 0:
                continue
            direction = 1 if row.direction == "long" else -1
            ret = direction * (float(exit_bar.close) / float(entry.open) - 1)
            trades.append(Trade(exit_date=_as_utc(exit_bar.ts).date(), ret=ret))
        return trades

    def run(self, now: dt.datetime) -> int:
        """Přepočítá všechny strategie; vrací počet zapsaných řádků."""
        closes = self._daily_closes()
        start = evaluation_start(closes)
        if start is None:
            logger.info("Track record: málo uzavřených vln — kalibrace ještě běží (ADR-0021)")
            return 0
        today = now.date()
        session_bars = self._session_bars(start, today)
        if not session_bars:
            logger.info("Track record: žádné denní bary od %s", start)
            return 0
        sessions = [bar.date for bar in session_bars]

        curves: dict[str, list[EquityPoint]] = {
            STRATEGY_BUY_HOLD: equity_curve(session_bars),
            STRATEGY_STATE: equity_curve(
                session_bars,
                state_positions(closes, start=start, short_riskoff=self._short_riskoff),
            ),
            STRATEGY_SIGNALS_NEWS: trades_to_curve(
                self._signal_trades("NEWS", start, today), sessions
            ),
            STRATEGY_SIGNALS_COMBINED: trades_to_curve(
                self._signal_trades("COMBINED", start, today), sessions
            ),
        }

        rows = [
            {
                "date": point.date,
                "strategy": strategy,
                "symbol": self._symbol,
                "equity": point.equity,
                "drawdown": point.drawdown,
            }
            for strategy, points in curves.items()
            for point in points
        ]
        with self._engine.begin() as conn:
            conn.execute(delete(track_record).where(track_record.c.symbol == self._symbol))
            if rows:
                conn.execute(insert(track_record), rows)
        logger.info(
            "Track record: %d řádků (od %s, %d seancí, 4 strategie)",
            len(rows),
            start,
            len(sessions),
        )
        return len(rows)


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
