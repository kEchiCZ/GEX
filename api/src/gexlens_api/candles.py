"""Svíčky vyšších timeframů z 1min partic barů (#1089, karta Trend v Briefingu).

Bary podkladu drží engine ~2 roky (`derived/{sym}/bars`, ADR-0028), takže
denní i týdenní svíčky jdou složit ve čtecí vrstvě bez zásahu do enginu —
jediný zdroj dat zůstává IBKR (R6). Osa dne je Globex seance (ADR-0023, #512):
denní svíčka = [17:00 CT D−1, 17:00 CT D), intradenní koše se zarovnávají na
otevření seance (4h svíčky 17:00, 21:00, 01:00 … CT), týdenní svíčka vzniká
z denních podle ISO týdne.

Čisté funkce nad DataFrame — testovatelné bez souborů.
"""

import datetime as dt
import math
from typing import Any

import pandas as pd

from gexlens_engine.compute.settle import CME_TZ, GLOBEX_OPEN_LOCAL, session_bounds

#: Intradenní timeframy v minutách; klíč = hodnota query parametru `tf`.
INTRADAY_MINUTES: dict[str, int] = {"15": 15, "60": 60, "240": 240}
#: Všechny podporované timeframy (pořadí od nejvyššího).
TIMEFRAMES: tuple[str, ...] = ("W", "D", "240", "60", "15")
#: Obchodní minuty seance (17:00 → 16:00 CT, hodina pauzy) — odhad, kolik partic číst.
SESSION_MINUTES = 23 * 60


def calendar_days_needed(tf: str, limit: int) -> int:
    """Kolik kalendářních dnů partic přečíst, aby vzniklo `limit` svíček.

    Rezerva tří dnů kryje víkend a sešití seance přes půlnoc UTC; u týdnů
    se počítá 7 kalendářních dnů na týden (svíčky vznikají z obchodních dnů).
    """
    if tf == "W":
        return limit * 7 + 3
    if tf == "D":
        return limit + 3
    minutes = INTRADAY_MINUTES[tf] * limit
    return math.ceil(minutes / SESSION_MINUTES) + 3


def session_dates(ts: pd.Series) -> pd.Series:
    """Obchodní den každé minuty (protějšek `trading_session_date`, vektorizovaně).

    Minuta od 17:00 America/Chicago patří seanci NÁSLEDUJÍCÍHO kalendářního dne.
    """
    local = ts.dt.tz_convert(CME_TZ)
    after_open = (local.dt.hour > GLOBEX_OPEN_LOCAL.hour) | (
        (local.dt.hour == GLOBEX_OPEN_LOCAL.hour) & (local.dt.minute >= GLOBEX_OPEN_LOCAL.minute)
    )
    days = local.dt.tz_localize(None).dt.normalize()
    days = days.where(~after_open, days + pd.Timedelta(days=1))
    return days.dt.date


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    """Sjednotí vstup: UTC časová osa, jedna minuta jednou, vzestupně.

    Vyhrává první výskyt minuty (#1002): partice se čtou vzestupně, takže
    večerní minuty drží partice D−1, kam podle UTC patří.
    """
    if frame.empty:
        return frame
    prepared = frame.copy()
    prepared["ts_min"] = pd.to_datetime(prepared["ts_min"], utc=True)
    prepared = prepared.sort_values("ts_min", kind="stable")
    prepared = prepared.drop_duplicates(subset="ts_min", keep="first")
    prepared["session"] = session_dates(prepared["ts_min"])
    return prepared.reset_index(drop=True)


def _aggregate(groups: Any) -> pd.DataFrame:
    return groups.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        last_ts=("ts_min", "max"),
    )


PARTIAL_COLUMNS = ["session", "open", "high", "low", "close", "volume", "first_ts", "last_ts"]


def partials_from_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Denní agregát jedné partice barů: ≤ 2 řádky (část seance D a D+1).

    Nese `first_ts`/`last_ts`, aby šlo víc částí téže seance sloučit se
    správným open (nejdřívější minuta) a close (nejpozdější). Tohle je tvar,
    který si repository drží v cache místo celé partice (#1089).
    """
    if frame.empty:
        return pd.DataFrame(columns=PARTIAL_COLUMNS)
    prepared = _prepare(frame)
    grouped = prepared.groupby("session", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        first_ts=("ts_min", "min"),
        last_ts=("ts_min", "max"),
    )
    return grouped.reset_index()[PARTIAL_COLUMNS]


def daily_from_partials(partials: pd.DataFrame) -> pd.DataFrame:
    """Denní svíčky sloučené z částí seance napříč particemi; index = obchodní den.

    Stejná minuta nemůže být ve dvou částech dvakrát jinak než chybou zápisu
    (#1002); části se řadí podle `first_ts`, takže open/close sedí i když
    seance leží ve dvou souborech.
    """
    if partials.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "last_ts"])
    ordered = partials.sort_values(["session", "first_ts"], kind="stable")
    return ordered.groupby("session", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        last_ts=("last_ts", "max"),
    )


def daily_candles(prepared: pd.DataFrame) -> pd.DataFrame:
    """Denní svíčky per Globex seance z připravených barů; index = obchodní den."""
    if prepared.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "last_ts"])
    return _aggregate(prepared.groupby("session", sort=True))


def weekly_candles(daily: pd.DataFrame) -> pd.DataFrame:
    """Týdenní svíčky z denních podle ISO týdne; index = pondělí týdne."""
    if daily.empty:
        return daily
    dates = pd.to_datetime(pd.Series(daily.index, index=daily.index))
    iso = dates.dt.isocalendar()
    week_start = dates - pd.to_timedelta(dates.dt.weekday, unit="D")
    frame = daily.assign(_year=iso["year"].to_numpy(), _week=iso["week"].to_numpy())
    frame = frame.assign(_start=week_start.dt.date.to_numpy())
    grouped = frame.groupby(["_year", "_week"], sort=True)
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        last_ts=("last_ts", "max"),
        start=("_start", "first"),
    )
    return result.set_index("start")


def intraday_candles(prepared: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Koše `minutes` minut zarovnané na otevření seance; index = UTC začátek koše."""
    if prepared.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "last_ts"])
    opens = {day: session_bounds(day)[0] for day in prepared["session"].unique()}
    session_open = pd.to_datetime(prepared["session"].map(opens), utc=True)
    offset_min = (prepared["ts_min"] - session_open).dt.total_seconds() // 60
    bucket = (offset_min // minutes).astype(int)
    start = session_open + pd.to_timedelta(bucket * minutes, unit="m")
    frame = prepared.assign(_start=start)
    return _aggregate(frame.groupby("_start", sort=True))


def build_candles(frame: pd.DataFrame, tf: str, limit: int) -> list[dict[str, object]]:
    """Posledních `limit` svíček timeframe `tf` z barů jako JSON řádky.

    `partial` označuje rozdělanou svíčku (poslední bar končí před koncem koše
    a koš ještě neuplynul) — trend se z ní čte jen jako průběžný stav.
    """
    prepared = _prepare(frame)
    if tf == "D":
        candles = daily_candles(prepared)
    elif tf == "W":
        candles = weekly_candles(daily_candles(prepared))
    else:
        candles = intraday_candles(prepared, INTRADAY_MINUTES[tf])
    return _rows(candles, tf, limit)


def build_daily_candles(partials: pd.DataFrame, tf: str, limit: int) -> list[dict[str, object]]:
    """Svíčky D/W z cache denních agregátů (rychlá cesta Briefingu)."""
    daily = daily_from_partials(partials)
    candles = weekly_candles(daily) if tf == "W" else daily
    return _rows(candles, tf, limit)


def _rows(candles: pd.DataFrame, tf: str, limit: int) -> list[dict[str, object]]:
    if candles.empty:
        return []
    candles = candles.tail(limit)
    now = dt.datetime.now(dt.UTC)
    rows: list[dict[str, object]] = []
    for key, row in candles.iterrows():
        start, end = _candle_span(tf, key)
        last_ts = row["last_ts"]
        partial = bool(now < end and last_ts + pd.Timedelta(minutes=1) < end)
        rows.append(
            {
                "ts": start.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "partial": partial,
            }
        )
    return rows


def _candle_span(tf: str, key: object) -> tuple[dt.datetime, dt.datetime]:
    """UTC začátek a konec svíčky podle jejího klíče (obchodní den / pondělí / koš)."""
    if tf == "D":
        day = key if isinstance(key, dt.date) else pd.Timestamp(str(key)).date()
        return session_bounds(day)
    if tf == "W":
        monday = key if isinstance(key, dt.date) else pd.Timestamp(str(key)).date()
        # Týden Globex: otevření nedělní seance (= seance pondělí) až pátek settle;
        # konec = otevření seance následujícího pondělí
        start = session_bounds(monday)[0]
        end = session_bounds(monday + dt.timedelta(days=7))[0]
        return start, end
    start_ts = pd.Timestamp(key)
    start = start_ts.to_pydatetime()
    return start, start + dt.timedelta(minutes=INTRADAY_MINUTES[tf])
