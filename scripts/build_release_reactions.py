"""Dataset reakcí ES a NQ na ohlášené USD releasy (#1296, fáze 1 — zpětný dopočet).

Výzkumný skript: nic nezapisuje do PG ani do `data/`. Produkční PG jen čte
(spojení s `default_transaction_read_only`), minutové bary
`data/derived/{ES,NQ}/bars/*.parquet` jen čte. Výstupy jdou do `--out-dir`.

Co se měří (jeden řádek = release × symbol, ES a NQ zvlášť):

* **Releasy**: `news_events` kind=scheduled, titulek `USD …`, FF impact
  (`raw.impactName` z backfillu, `raw.impact` z živého feedu) High/Medium,
  s `actual` i `forecast`. `importance` se nepoužívá — u scheduled ji přepisuje
  regexový klasifikátor (#1291, #1293).
* **Krátké horizonty 5/15/60 min** od floor minuty releasu: close-to-close
  (`compute_reactions`) a maximální výchylka nahoru/dolů proti close minuty před
  releasem (`measure_excursion`, #1291). Měřitelné jen s barem minuty před
  releasem a aspoň `h − 1` bary v okně.
* **Vícedenní horizonty 1/2/3/5/10 seancí** (`compute_daily_reactions`, ADR-0023):
  základ = poslední bar před releasem, konec = settle close N-té seance
  se settle po releasu (1d = nejbližší settle). Varianta `_cc` měří settle →
  settle od posledního settle PŘED releasem (včetně pohybu před releasem).
* **Roll kontraktů**: archiv není jeden kontrakt. Přechod na další kvartál se
  v datech hledá jako skok na denní pauze Globexu v okolí expirace, společný ES
  i NQ; ceny PŘED přechodem se zpětně násobí poměrem (back-adjust o spread).
  Okna přes přechod nesou příznak `roll_{N}d`. Řádky smíšeného kontraktu
  z doplňování děr (#1232) se vyřadí jako „plošina" posunutá o spread.
* **Seance**: seance bez settle (svátek — pauza 12:00 CT, mimořádné uzavření)
  se slučuje do následující (obchodní den CME); seance s dírou v datech zůstává
  v počtu seancí, ale bez close (okna končící v ní = NaN).
* **Baseline**: krátké horizonty — stejná minuta dne (ET) v ±60 seancích bez
  High/Medium USD události v okně; vícedenní — start ve stejnou minutu dne
  v ±60 seancích bez High USD události v seanci. Abnormální pohyb = ret −
  medián baseline, percentil = pozice ret v baseline (0–100).
* **Překvapení**: `actual − forecast`, `surprise_z` z DB (ffhistory, σ celé
  řady — obsahuje budoucnost), `surprise_z_pit` (σ jen z dřívějších releasů
  řady, ≥ `MIN_SERIES_SAMPLES`), polarita řady (+1 = vyšší číslo = silnější
  ekonomika / vyšší inflace / jestřábí; Unemployment Rate a Claims −1).
* **Režim**: realizovaná σ denních výnosů 20 předchozích seancí, tercily
  (celý vzorek i point-in-time proti 252 předchozím seancím, ADR-0028).
  GEX režim jen kde existují levels partice (od 20. 7. 2026).
* **Překryv**: další High USD releasy (s actual) a FOMC ve vícedenním okně.

Spuštění (z hostitele; URL ani heslo se nevypisují):
    python scripts/build_release_reactions.py --data-dir D:/…/GEX/data \\
        --env-file D:/…/GEX/.env --out-dir <scratchpad>/epic1296
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))
sys.path.insert(0, str(ROOT / "news-engine" / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import URL, Engine  # noqa: E402

from gexlens_engine.compute.settle import (  # noqa: E402
    ET_TZ,
    QUARTER_MONTHS,
    quarterly_expiry,
    session_bounds,
    settle_ts,
    trading_session_date,
)
from gexlens_engine.storage.sentiment import news_events  # noqa: E402
from gexlens_news.clusters import floor_minute  # noqa: E402
from gexlens_news.ffhistory import MIN_SERIES_SAMPLES  # noqa: E402
from gexlens_news.model import normalize_title  # noqa: E402
from gexlens_news.reaction_job import LevelsRegimeReader  # noqa: E402
from gexlens_news.reactions import (  # noqa: E402
    DEFERRED_GAP_MINUTES,
    MINUTES_PER_TRADING_DAY,
    Bar,
    SessionDaily,
    compute_daily_reactions,
    compute_reactions,
    measure_excursion,
)

SYMBOLS = ("ES", "NQ")
SHORT_WINDOWS = (5, 15, 60)
DAILY_WINDOWS = (1, 2, 3, 5, 10)
QUALIFYING_IMPACTS = ("high", "medium")
ARCHIVE_START = dt.datetime(2024, 7, 28, tzinfo=dt.UTC)

#: ±N seancí kolem releasu pro baseline
BASELINE_SESSIONS = 60
#: Pod tolik vzorků baseline se abnormální pohyb ani percentil neuvádí
BASELINE_MIN_SAMPLES = 20
#: Krátká baseline vyřadí start, když je High/Medium událost v [start − 5 min, start + h)
BASELINE_EVENT_GUARD_MIN = 5
#: Pohyb před releasem
PRE_WINDOW_MIN = 60

# ── Roll kontraktů ─────────────────────────────────────────────────
#: Přechod se hledá v [expirace − 12 d, expirace + 1 d]
ROLL_SEARCH_BEFORE_DAYS = 12
ROLL_SEARCH_AFTER_DAYS = 1
#: Denní pauza Globexu (61 min); víkend se za přechod nepovažuje
HALT_GAP_MIN = (30, 240)
#: Spread přechodu musí být nad tímto prahem u ES i NQ (carry ~ 80–140 bp)
ROLL_MIN_SPREAD_BP = 40.0
#: Plošina smíšeného kontraktu: vstupní i výstupní skok nad prahem, opačná znaménka
MIXED_MIN_JUMP_BP = 50.0
MIXED_MAX_RESIDUAL = 0.3
MIXED_NEIGHBOUR_MIN = 10
#: Zdroj měřené cesty (NULL = partice před zavedením sloupce `source`)
MEASURED_SOURCE = "ibkr"

# ── Úplnost seancí ─────────────────────────────────────────────────
SETTLE_LAG_MAX_MIN = 15
COVERAGE_MIN = 0.9
#: Zkrácená seance se settle (Black Friday, Štědrý den, 3. 7.): konec 12:15 CT
EARLY_CLOSE_ET = (dt.time(13, 10), dt.time(13, 20))
#: Svátek (pauza 12:00 CT) / mimořádné uzavření (8:15–8:30 CT) — bez settle
CLOSURE_ET = (dt.time(8, 0), dt.time(13, 10))

# ── Volatilitní režim ──────────────────────────────────────────────
RV_SESSIONS = 20
RV_MIN_RETURNS = 15
PIT_WINDOW = 252
PIT_MIN = 60

#: Řada → (skupina, polarita). Polarita +1: vyšší číslo = silnější ekonomika,
#: vyšší inflace, jestřábí Fed. −1: vyšší číslo = slabší (nezaměstnanost,
#: žádosti o podporu, zásoby ropy = slabší poptávka).
SERIES: dict[str, tuple[str, int]] = {
    "Federal Funds Rate": ("fed", 1),
    "Non-Farm Employment Change": ("labor", 1),
    "Core CPI m/m": ("inflation", 1),
    "CPI m/m": ("inflation", 1),
    "Core CPI y/y": ("inflation", 1),
    "CPI y/y": ("inflation", 1),
    "Core PCE Price Index m/m": ("inflation", 1),
    "Core PPI m/m": ("inflation", 1),
    "PPI m/m": ("inflation", 1),
    "Retail Sales m/m": ("growth", 1),
    "Core Retail Sales m/m": ("growth", 1),
    "Advance GDP q/q": ("growth", 1),
    "Prelim GDP q/q": ("growth", 1),
    "Final GDP q/q": ("growth", 1),
    "ISM Manufacturing PMI": ("growth", 1),
    "ISM Services PMI": ("growth", 1),
    "ADP Non-Farm Employment Change": ("labor", 1),
    "JOLTS Job Openings": ("labor", 1),
    "Unemployment Rate": ("labor", -1),
    "Average Hourly Earnings m/m": ("labor", 1),
    "Employment Cost Index q/q": ("labor", 1),
    "Unemployment Claims": ("labor", -1),
    "ISM Manufacturing Prices": ("inflation", 1),
    "Advance GDP Price Index q/q": ("inflation", 1),
    "Prelim GDP Price Index q/q": ("inflation", 1),
    "Final GDP Price Index q/q": ("inflation", 1),
    "Flash Manufacturing PMI": ("growth", 1),
    "Flash Services PMI": ("growth", 1),
    "Final Manufacturing PMI": ("growth", 1),
    "Final Services PMI": ("growth", 1),
    "Durable Goods Orders m/m": ("growth", 1),
    "Core Durable Goods Orders m/m": ("growth", 1),
    "Philly Fed Manufacturing Index": ("growth", 1),
    "Empire State Manufacturing Index": ("growth", 1),
    "Richmond Manufacturing Index": ("growth", 1),
    "Chicago PMI": ("growth", 1),
    "Prelim UoM Consumer Sentiment": ("sentiment", 1),
    "Revised UoM Consumer Sentiment": ("sentiment", 1),
    "CB Consumer Confidence": ("sentiment", 1),
    "Pending Home Sales m/m": ("housing", 1),
    "Existing Home Sales": ("housing", 1),
    "New Home Sales": ("housing", 1),
    "Building Permits": ("housing", 1),
    "S&P/CS Composite-20 HPI y/y": ("housing", 1),
    "Crude Oil Inventories": ("energy", -1),
}
#: Pořadí headline řad pro `primary_in_cluster` / `primary_in_group` — souběžné
#: releasy (CPI m/m + y/y + core…) nesou identickou reakci trhu; skupinová
#: statistika má brát jeden řádek na shluk a skupinu. Pořadí = pořadí v SERIES.
SERIES_RANK = {name: rank for rank, name in enumerate(SERIES)}
FOMC_DECISION_TITLES = ("USD Federal Funds Rate", "USD FOMC Statement")


def short_columns(h: int) -> list[str]:
    return [
        f"ret_{h}m_bp",
        f"up_{h}m_bp",
        f"down_{h}m_bp",
        f"exc_{h}m_bp",
        f"exc_dir_{h}m",
        f"exc_z_{h}m",
        f"contam_{h}m",
        f"ret_abn_{h}m_bp",
        f"ret_pct_{h}m",
        f"exc_pct_{h}m",
    ]


# ── Pomocné ────────────────────────────────────────────────────────


def impact_of(raw: Any) -> str | None:
    """FF impact z raw: backfill `impactName` (lowercase), živý feed `impact`."""
    if not isinstance(raw, dict):
        return None
    value = raw.get("impactName") or raw.get("impact")
    return str(value).lower() if value else None


def read_env_value(env_file: Path | None, key: str) -> str | None:
    """Hodnota klíče z prostředí, jinak z dotenv souboru. Nikdy se nevypisuje."""
    value = os.environ.get(key)
    if value:
        return value
    if env_file is None or not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        if name.strip() == key:
            return raw.strip().strip('"').strip("'") or None
    return None


def read_only_engine(env_file: Path | None) -> Engine:
    url = os.environ.get("GEXLENS_HOST_DATABASE_URL")
    target: str | URL
    if url:
        target = url
    else:
        password = read_env_value(env_file, "GEXLENS_PG_PASSWORD")
        if not password:
            raise SystemExit("Chybí GEXLENS_HOST_DATABASE_URL nebo GEXLENS_PG_PASSWORD")
        target = URL.create(
            "postgresql+psycopg",
            username="gexlens",
            password=password,
            host="127.0.0.1",
            port=55432,
            database="gexlens",
        )
    return create_engine(target, connect_args={"options": "-c default_transaction_read_only=on"})


def percentile_rank(value: float, sample: np.ndarray) -> float:
    """Pozice hodnoty v rozdělení 0–100 (shody se počítají napůl)."""
    below = float(np.count_nonzero(sample < value))
    equal = float(np.count_nonzero(sample == value))
    return 100.0 * (below + 0.5 * equal) / len(sample)


def sign(value: float) -> int:
    if abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else -1


def to_utc(ts: pd.Timestamp) -> dt.datetime:
    stamp = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    result: dt.datetime = stamp.to_pydatetime()
    return result


# ── Události ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    id: int
    ts: dt.datetime
    title: str
    impact: str | None
    forecast: float | None
    actual: float | None
    previous: float | None
    surprise_z_db: float | None

    @property
    def minute(self) -> dt.datetime:
        return floor_minute(self.ts)


def load_events(engine: Engine) -> list[Event]:
    stmt = (
        select(
            news_events.c.id,
            news_events.c.ts_event,
            news_events.c.title,
            news_events.c.forecast,
            news_events.c.actual,
            news_events.c.previous,
            news_events.c.surprise_z,
            news_events.c.raw,
        )
        .where(news_events.c.kind == "scheduled")
        .where(news_events.c.title.like("USD %"))
        .order_by(news_events.c.ts_event, news_events.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()

    def num(value: Any) -> float | None:
        return float(value) if value is not None else None

    return [
        Event(
            id=int(row.id),
            ts=row.ts_event.astimezone(dt.UTC),
            title=str(row.title),
            impact=impact_of(row.raw),
            forecast=num(row.forecast),
            actual=num(row.actual),
            previous=num(row.previous),
            surprise_z_db=num(row.surprise_z),
        )
        for row in rows
    ]


def series_surprises(events: Sequence[Event]) -> dict[str, list[tuple[Event, float]]]:
    """Překvapení podle řady — klíč normalize_title jako `ffhistory.recompute_surprise_z`."""
    by_series: dict[str, list[tuple[Event, float]]] = defaultdict(list)
    for event in sorted(events, key=lambda e: (e.ts, e.id)):
        if event.actual is not None and event.forecast is not None:
            by_series[normalize_title(event.title)].append((event, event.actual - event.forecast))
    return by_series


def surprise_z_pit(events: Sequence[Event]) -> dict[int, tuple[float | None, int]]:
    """σ překvapení jen z DŘÍVĚJŠÍCH releasů řady (bez pohledu do budoucnosti)."""
    result: dict[int, tuple[float | None, int]] = {}
    for series in series_surprises(events).values():
        history: list[float] = []
        for event, surprise in series:
            z: float | None = None
            if len(history) >= MIN_SERIES_SAMPLES:
                sigma = statistics.stdev(history)
                if sigma > 0:
                    z = surprise / sigma
            result[event.id] = (z, len(history))
            history.append(surprise)
    return result


def surprise_z_full(events: Sequence[Event]) -> dict[int, float]:
    """Kontrolní přepočet `ffhistory.recompute_surprise_z` (σ celé řady) bez zápisu."""
    result: dict[int, float] = {}
    for series in series_surprises(events).values():
        surprises = [surprise for _, surprise in series]
        if len(surprises) < MIN_SERIES_SAMPLES:
            continue
        sigma = statistics.stdev(surprises)
        if sigma <= 0:
            continue
        for event, surprise in series:
            result[event.id] = surprise / sigma
    return result


# ── Bary ───────────────────────────────────────────────────────────


def load_bars(data_dir: Path, symbol: str) -> pd.DataFrame:
    """Všechny partice symbolu; jedna minuta = jeden řádek (pravidlo #1002).

    `ts` = naivní UTC datetime64 (rychlé numpy operace), `source` = zdroj baru
    (None u partic před zavedením sloupce).
    """
    directory = data_dir / "derived" / symbol / "bars"
    frames = []
    for path in sorted(directory.glob("*.parquet")):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        table = pq.read_table(path)
        frame = table.select(["ts_min", "open", "high", "low", "close"]).to_pandas()
        frame["source"] = (
            table.column("source").to_pylist() if "source" in table.schema.names else None
        )
        ts = pd.to_datetime(frame["ts_min"])
        ts = ts.dt.tz_localize("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")
        frame["ts"] = ts.dt.tz_localize(None).astype("datetime64[ns]")
        frame["own"] = frame["ts"].dt.normalize() == pd.Timestamp(day)
        frame["partition"] = pd.Timestamp(day)
        frames.append(frame.drop(columns=["ts_min"]))
    if not frames:
        raise SystemExit(f"{symbol}: žádné partice barů v {directory}")
    bars = pd.concat(frames, ignore_index=True)
    # Vyhrává řádek z partice UTC dne minuty, jinak první nalezený (BarsRepository.load_range)
    bars = bars.sort_values(
        ["ts", "own", "partition"], ascending=[True, False, True], kind="stable"
    )
    bars = bars.drop_duplicates("ts", keep="first").reset_index(drop=True)
    bad = (bars["close"] <= 0) | (bars["open"] <= 0) | bars["close"].isna()
    if bad.any():
        print(f"{symbol}: vyřazeno {int(bad.sum())} barů s nekladnou/chybějící cenou")
    bars = bars[~bad].reset_index(drop=True)
    return bars[["ts", "open", "high", "low", "close", "source"]]


@dataclass
class MixedRun:
    symbol: str
    start: pd.Timestamp
    end: pd.Timestamp
    rows: int
    source: str
    entry_bp: float
    exit_bp: float


def drop_mixed_contract_runs(
    bars: pd.DataFrame, symbol: str
) -> tuple[pd.DataFrame, list[MixedRun]]:
    """Vyřadí souvislé běhy doplněných barů posunuté o spread jiného kontraktu (#1232).

    Běh = maximální posloupnost řádků se zdrojem mimo měřenou cestu. Vyřadí se,
    když skok do běhu i z běhu je nad `MIXED_MIN_JUMP_BP`, mají opačná znaménka
    a cena se vrátí na původní hladinu (plošina) — skutečná reakce na zprávu
    takhle nevypadá, posun o carry spread ano.
    """
    measured = bars["source"].isna() | (bars["source"] == MEASURED_SOURCE)
    flags = (~measured).to_numpy()
    ts = bars["ts"].to_numpy()
    opens = bars["open"].to_numpy()
    closes = bars["close"].to_numpy()
    sources = bars["source"].to_numpy()
    neighbour = np.timedelta64(MIXED_NEIGHBOUR_MIN, "m")
    drop = np.zeros(len(bars), dtype=bool)
    runs: list[MixedRun] = []
    index = 0
    count = len(bars)
    while index < count:
        if not flags[index]:
            index += 1
            continue
        end = index
        while end + 1 < count and flags[end + 1]:
            end += 1
        before = index - 1
        after = end + 1
        if (
            before >= 0
            and after < count
            and ts[index] - ts[before] <= neighbour
            and ts[after] - ts[end] <= neighbour
        ):
            entry = math.log(opens[index] / closes[before]) * 1e4
            exit_ = math.log(opens[after] / closes[end]) * 1e4
            plateau = abs(entry + exit_) <= MIXED_MAX_RESIDUAL * max(abs(entry), abs(exit_))
            if (
                abs(entry) >= MIXED_MIN_JUMP_BP
                and abs(exit_) >= MIXED_MIN_JUMP_BP
                and entry * exit_ < 0
                and plateau
            ):
                drop[index : end + 1] = True
                runs.append(
                    MixedRun(
                        symbol=symbol,
                        start=pd.Timestamp(ts[index]),
                        end=pd.Timestamp(ts[end]),
                        rows=end - index + 1,
                        source=str(sources[index]),
                        entry_bp=entry,
                        exit_bp=exit_,
                    )
                )
        index = end + 1
    return bars[~drop].reset_index(drop=True), runs


@dataclass
class RollSwitch:
    expiry: dt.date
    ts: pd.Timestamp  # první bar nového kontraktu (naivní UTC)
    spread_bp: dict[str, float]
    runner_up_bp: float | None


def halt_gaps(bars: pd.DataFrame) -> dict[pd.Timestamp, float]:
    """Log skok close → open přes denní pauzu, klíč = čas prvního baru po pauze."""
    ts = bars["ts"].to_numpy()
    gaps_min = np.diff(ts).astype("timedelta64[m]").astype(np.int64)
    mask = (gaps_min >= HALT_GAP_MIN[0]) & (gaps_min <= HALT_GAP_MIN[1])
    jumps = np.log(bars["open"].to_numpy()[1:] / bars["close"].to_numpy()[:-1])
    after = ts[1:]
    return {pd.Timestamp(after[i]): float(jumps[i]) for i in np.flatnonzero(mask)}


def detect_roll_switches(bars_by_symbol: dict[str, pd.DataFrame]) -> list[RollSwitch]:
    """Přechody na další kvartál: největší společný skok ES i NQ na denní pauze u expirace.

    ADR-0028 (dodatek) uvádí hranici na expiraci; data ukazují přechod 1–3
    seance před ní (první chunk nového kontraktu v `deepbars` přepsal překryv),
    živě 16. 9. 2026 podle roll date (ADR-0039). Proto rozhodují data.
    """
    gaps = {symbol: halt_gaps(bars) for symbol, bars in bars_by_symbol.items()}
    first = max(bars["ts"].iloc[0] for bars in bars_by_symbol.values()).date()
    last = min(bars["ts"].iloc[-1] for bars in bars_by_symbol.values()).date()
    switches: list[RollSwitch] = []
    for year in range(first.year, last.year + 1):
        for month in QUARTER_MONTHS:
            expiry = quarterly_expiry(year, month)
            lo = expiry - dt.timedelta(days=ROLL_SEARCH_BEFORE_DAYS)
            hi = expiry + dt.timedelta(days=ROLL_SEARCH_AFTER_DAYS)
            if hi < first or lo > last:
                continue
            common = set.intersection(
                *({ts for ts in g if lo <= ts.date() <= hi} for g in gaps.values())
            )
            scored = sorted(((min(gaps[s][ts] for s in gaps), ts) for ts in common), reverse=True)
            if not scored or scored[0][0] * 1e4 < ROLL_MIN_SPREAD_BP:
                if expiry <= last:
                    raise SystemExit(
                        f"Přechod kontraktu u expirace {expiry} nenalezen — zkontroluj bary"
                    )
                continue  # expirace ještě neproběhla a přechod v datech není
            best_ts = scored[0][1]
            switches.append(
                RollSwitch(
                    expiry=expiry,
                    ts=best_ts,
                    spread_bp={s: gaps[s][best_ts] * 1e4 for s in gaps},
                    runner_up_bp=scored[1][0] * 1e4 if len(scored) > 1 else None,
                )
            )
    return switches


# ── Minutová mřížka a seance ───────────────────────────────────────


@dataclass
class Grid:
    """Husté minutové pole (index = minuty od `t0`, naivní UTC); ceny back-adjusted o roll."""

    t0: pd.Timestamp
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    raw_close: np.ndarray

    def index(self, ts: dt.datetime) -> int:
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC").tz_localize(None)
        return int((stamp - self.t0) // pd.Timedelta(minutes=1))

    def ts(self, index: int) -> dt.datetime:
        return to_utc(self.t0 + pd.Timedelta(minutes=index))

    def bar(self, index: int) -> Bar | None:
        if index < 0 or index >= len(self.close) or math.isnan(self.close[index]):
            return None
        return Bar(
            ts=self.ts(index),
            open=float(self.open[index]),
            high=float(self.high[index]),
            low=float(self.low[index]),
            close=float(self.close[index]),
            volume=0.0,
        )

    def bars(self, start: int, end: int) -> list[Bar]:
        found = (self.bar(i) for i in range(max(start, 0), min(end, len(self.close))))
        return [bar for bar in found if bar is not None]


def build_grid(
    bars: pd.DataFrame,
    t0: pd.Timestamp,
    t1: pd.Timestamp,
    switches: Sequence[RollSwitch],
    symbol: str,
) -> Grid:
    size = int((t1 - t0) // pd.Timedelta(minutes=1)) + 1
    idx = ((bars["ts"] - t0) // pd.Timedelta(minutes=1)).to_numpy().astype(np.int64)
    # Back-adjust: ceny před přechodem × exp(spread) — referenční je nejnovější kontrakt
    ts = bars["ts"].to_numpy()
    log_factor = np.zeros(len(bars))
    for switch in switches:
        log_factor[ts < switch.ts.to_datetime64()] += switch.spread_bp[symbol] / 1e4
    factor = np.exp(log_factor)
    arrays = {}
    for column in ("open", "high", "low", "close"):
        values = np.full(size, np.nan)
        values[idx] = bars[column].to_numpy() * factor
        arrays[column] = values
    raw_close = np.full(size, np.nan)
    raw_close[idx] = bars["close"].to_numpy()
    return Grid(t0=t0, raw_close=raw_close, **arrays)


@dataclass
class Sessions:
    """Obchodní seance symbolu (ADR-0023) po sloučení seancí bez settle."""

    days: list[dt.date]
    settle_idx: np.ndarray  # minutový index settle v mřížce
    close: np.ndarray  # upravený settle close, NaN = seance s dírou v datech
    status: list[str]
    merged_closures: list[dt.date]
    daily: list[SessionDaily] = field(default_factory=list)


def build_sessions(bars: pd.DataFrame, grid: Grid, asof: dt.datetime) -> Sessions:
    """Seance z barů: settle close = poslední bar ≤ settle (vzor `ReactionJob._daily_sessions`)."""
    hours = bars["ts"].dt.floor("h")
    session_of_hour = {
        hour: trading_session_date(to_utc(pd.Timestamp(hour))) for hour in hours.unique()
    }
    frame = pd.DataFrame({"ts": bars["ts"], "session": hours.map(session_of_hour)})
    settle_map = {day: settle_ts(day) for day in frame["session"].unique()}
    settle_naive = {
        day: pd.Timestamp(value).tz_convert("UTC").tz_localize(None)
        for day, value in settle_map.items()
    }
    frame["settle"] = frame["session"].map(settle_naive)
    frame = frame[frame["ts"] <= frame["settle"]]  # po settle mimo denní agregát
    grouped = frame.groupby("session")["ts"].agg(["max", "count"]).sort_index()

    days: list[dt.date] = []
    status: list[str] = []
    last_bar: list[pd.Timestamp] = []
    closures: list[dt.date] = []
    for day, row in grouped.iterrows():
        settle = settle_map[day]
        if settle > asof:
            continue  # rozběhnutá seance — horizonty neuzavřené
        last = to_utc(row["max"])
        open_ts = session_bounds(day)[0]
        span = int((last - open_ts).total_seconds() // 60) + 1
        coverage = row["count"] / span if span > 0 else 0.0
        last_et = last.astimezone(ET_TZ)
        if (settle - last).total_seconds() / 60 <= SETTLE_LAG_MAX_MIN:
            # settle close existuje; díra uvnitř dne (9. 9. 2026 po vyřazení smíšených
            # řádků) vadí jen krátkým oknům a ta si pokrytí hlídají sama
            kind = "full"
        elif (
            last_et.date() == day
            and EARLY_CLOSE_ET[0] <= last_et.time() < EARLY_CLOSE_ET[1]
            and coverage >= COVERAGE_MIN
        ):
            kind = "early_close"
        elif (
            last_et.date() == day
            and CLOSURE_ET[0] <= last_et.time() < CLOSURE_ET[1]
            and coverage >= COVERAGE_MIN
        ):
            closures.append(day)  # svátek / mimořádné uzavření: bez settle, patří další seanci
            continue
        else:
            kind = "incomplete"
        days.append(day)
        status.append(kind)
        last_bar.append(row["max"])

    settle_idx = np.array([grid.index(settle_map[day]) for day in days], dtype=np.int64)
    close = np.full(len(days), np.nan)
    daily: list[SessionDaily] = []
    for position, day in enumerate(days):
        if status[position] != "incomplete":
            close[position] = grid.close[grid.index(to_utc(last_bar[position]))]
        daily.append(
            SessionDaily(
                day=day,
                settle_ts=settle_map[day],
                close=float(close[position]),
                high=math.nan,
                low=math.nan,
            )
        )
    return Sessions(
        days=days,
        settle_idx=settle_idx,
        close=close,
        status=status,
        merged_closures=closures,
        daily=daily,
    )


def realized_vol(sessions: Sessions) -> np.ndarray:
    """rv20[k] = σ denních log výnosů seancí k−20..k−1 (bp); NaN při < 15 výnosech."""
    closes = sessions.close
    returns = np.full(len(closes), np.nan)
    returns[1:] = np.log(closes[1:] / closes[:-1])
    rv = np.full(len(closes), np.nan)
    for k in range(len(closes)):
        window = returns[max(0, k - RV_SESSIONS) : k]
        window = window[~np.isnan(window)]
        if len(window) >= RV_MIN_RETURNS:
            rv[k] = float(np.std(window, ddof=1)) * 1e4
    return rv


def tercile(pct: float) -> str | None:
    if math.isnan(pct):
        return None
    if pct < 100 / 3:
        return "low"
    if pct > 200 / 3:
        return "high"
    return "mid"


# ── Měření ─────────────────────────────────────────────────────────


class Sampler:
    """Rychlé vzorky z mřížky pro baseline, s cache (baseline sdílí časy dne)."""

    def __init__(self, grid: Grid, sessions: Sessions) -> None:
        self.grid = grid
        self.sessions = sessions
        self._short: dict[tuple[int, int], tuple[float, float, float, float] | None] = {}
        self._daily: dict[tuple[int, int], tuple[float, int] | None] = {}

    def short(self, start: int, window: int) -> tuple[float, float, float, float] | None:
        """(ret, up, down, exc) v bp; None = chybí bar před startem nebo víc než 1 bar v okně."""
        key = (start, window)
        if key in self._short:
            return self._short[key]
        grid = self.grid
        result: tuple[float, float, float, float] | None = None
        if start >= 1 and start + window <= len(grid.close):
            base = grid.close[start - 1]
            closes = grid.close[start : start + window]
            valid = ~np.isnan(closes)
            if not math.isnan(base) and int(valid.sum()) >= window - 1:
                last = closes[valid][-1]
                up = (np.nanmax(grid.high[start : start + window]) - base) / base * 1e4
                down = (base - np.nanmin(grid.low[start : start + window])) / base * 1e4
                result = ((last - base) / base * 1e4, up, down, max(up, down))
        self._short[key] = result
        return result

    def daily(
        self, start: int, days: int, base_index: int | None = None
    ) -> tuple[float, int] | None:
        """Rychlá cesta `compute_daily_reactions`: (ret bp, index koncové seance).

        Konec = N-tá seance se settle > start (event_ts = minuta `start`).
        """
        key = (start, days)
        if base_index is None and key in self._daily:
            return self._daily[key]
        grid, sessions = self.grid, self.sessions
        base_at = start - 1 if base_index is None else base_index
        result: tuple[float, int] | None = None
        if base_at >= 0 and not math.isnan(grid.close[base_at]):
            ahead = int(np.searchsorted(sessions.settle_idx, start, side="right"))
            end = ahead + days - 1
            if end < len(sessions.days) and not math.isnan(sessions.close[end]):
                base = grid.close[base_at]
                result = ((sessions.close[end] - base) / base * 1e4, end)
        if base_index is None:
            self._daily[key] = result
        return result

    def settle_to_settle(self, session: int, days: int) -> float | None:
        """Close-to-close v bp: settle seance `session − 1` → settle seance `session + days − 1`."""
        closes = self.sessions.close
        end = session + days - 1
        if session < 1 or end >= len(closes):
            return None
        start_close, end_close = closes[session - 1], closes[end]
        if math.isnan(start_close) or math.isnan(end_close):
            return None
        return float((end_close - start_close) / start_close * 1e4)


def last_valid_before(grid: Grid, index: int, lookback: int = 7 * 1440) -> int | None:
    lo = max(0, index - lookback)
    valid = np.flatnonzero(~np.isnan(grid.close[lo:index]))
    return lo + int(valid[-1]) if len(valid) else None


def first_valid_at_or_after(grid: Grid, index: int, lookahead: int = 7 * 1440) -> int | None:
    valid = np.flatnonzero(~np.isnan(grid.close[index : index + lookahead]))
    return index + int(valid[0]) if len(valid) else None


def baseline_start(grid: Grid, event_et: dt.datetime, day: dt.date) -> int:
    """Stejná minuta dne (ET) v seanci `day`; večer po 18:00 ET patří seanci D+1."""
    local_day = day - dt.timedelta(days=1) if event_et.time() >= dt.time(18, 0) else day
    local = dt.datetime.combine(local_day, event_et.time().replace(second=0, microsecond=0), ET_TZ)
    return grid.index(local.astimezone(dt.UTC))


def has_event_in(minutes: np.ndarray, lo: int, hi: int) -> bool:
    """Je v seřazeném `minutes` hodnota z [lo, hi)?"""
    position = int(np.searchsorted(minutes, lo, side="left"))
    return position < len(minutes) and minutes[position] < hi


@dataclass
class Context:
    grid: Grid
    sessions: Sessions
    sampler: Sampler
    rv20: np.ndarray
    rv20_full_sorted: np.ndarray
    high_session_flags: np.ndarray  # seance s High USD událostí (vyřazeno z vícedenní baseline)


def build_contexts(
    args: argparse.Namespace, asof: dt.datetime, high_any: Sequence[Event], report: dict[str, Any]
) -> tuple[dict[str, Context], list[RollSwitch]]:
    raw_bars: dict[str, pd.DataFrame] = {}
    for symbol in SYMBOLS:
        loaded = load_bars(args.data_dir, symbol)
        cleaned, runs = drop_mixed_contract_runs(loaded, symbol)
        report["mixed_runs"].extend(runs)
        report["bars"][symbol] = (len(loaded), len(loaded) - len(cleaned))
        raw_bars[symbol] = cleaned
        dropped = len(loaded) - len(cleaned)
        print(f"{symbol}: barů {len(loaded)}, vyřazeno smíšených {dropped} v {len(runs)} bězích")
    switches = detect_roll_switches(raw_bars)
    for switch in switches:
        spreads = ", ".join(f"{s} {v:+.1f} bp" for s, v in switch.spread_bp.items())
        runner = f"{switch.runner_up_bp:+.1f} bp" if switch.runner_up_bp is not None else "—"
        print(
            f"Roll u expirace {switch.expiry}: {switch.ts} UTC ({spreads}; druhý kandidát {runner})"
        )

    t0 = min(bars["ts"].iloc[0] for bars in raw_bars.values()).floor("D")
    t1 = max(bars["ts"].iloc[-1] for bars in raw_bars.values()) + pd.Timedelta(days=8)
    contexts: dict[str, Context] = {}
    for symbol in SYMBOLS:
        grid = build_grid(raw_bars[symbol], t0, t1, switches, symbol)
        sessions = build_sessions(raw_bars[symbol], grid, asof)
        rv20 = realized_vol(sessions)
        flags = np.zeros(len(sessions.days), dtype=bool)
        for event in high_any:
            position = int(np.searchsorted(sessions.settle_idx, grid.index(event.ts), side="left"))
            if position < len(flags):
                flags[position] = True
        contexts[symbol] = Context(
            grid=grid,
            sessions=sessions,
            sampler=Sampler(grid, sessions),
            rv20=rv20,
            rv20_full_sorted=np.sort(rv20[~np.isnan(rv20)]),
            high_session_flags=flags,
        )
        counts = Counter(sessions.status)
        print(
            f"{symbol}: seancí {len(sessions.days)} {dict(counts)}, "
            f"sloučených bez settle {len(sessions.merged_closures)}"
        )
    return contexts, switches


def measure_symbol(
    event: Event,
    symbol: str,
    ctx: Context,
    *,
    hm_minutes: np.ndarray,
    hm_sorted: list[dt.datetime],
    overlap_sorted: list[Event],
    overlap_ts: list[dt.datetime],
    switches: Sequence[RollSwitch],
    levels: LevelsRegimeReader,
    first_levels_day: dt.date | None,
    mismatches: Counter[str],
) -> dict[str, Any]:
    grid, sessions, sampler = ctx.grid, ctx.sessions, ctx.sampler
    start_ts = event.minute
    event_et = start_ts.astimezone(ET_TZ)
    m = grid.index(start_ts)
    row: dict[str, Any] = {"symbol": symbol}
    base_index = last_valid_before(grid, m)
    first_index = first_valid_at_or_after(grid, m)
    base_age = (m - base_index) if base_index is not None else None
    short_ok = base_index == m - 1
    fresh = base_age is not None and base_age <= DEFERRED_GAP_MINUTES
    row["base_age_min"] = base_age
    row["market_closed_or_gap"] = not fresh
    row["base_close_raw"] = float(grid.raw_close[base_index]) if base_index is not None else None
    k = int(np.searchsorted(sessions.settle_idx, m, side="right"))  # první settle po releasu
    row["session_date"] = sessions.days[k] if k < len(sessions.days) else None

    # Pohyb před releasem
    row["pre_close_bp"] = row["pre_60m_bp"] = None
    if short_ok:
        base = grid.close[m - 1]
        if k >= 1 and not math.isnan(sessions.close[k - 1]):
            row["pre_close_bp"] = (base - sessions.close[k - 1]) / sessions.close[k - 1] * 1e4
        earlier = grid.close[m - 1 - PRE_WINDOW_MIN]
        if not math.isnan(earlier):
            row["pre_60m_bp"] = (base - earlier) / earlier * 1e4

    # Režim volatility (per symbol) a GEX
    rv = ctx.rv20[k] if k < len(ctx.rv20) else math.nan
    row["rv20_bp"] = row["rv20_pct_pit"] = row["vol_tercile"] = row["vol_tercile_pit"] = None
    if not math.isnan(rv):
        row["rv20_bp"] = rv
        row["vol_tercile"] = tercile(percentile_rank(rv, ctx.rv20_full_sorted))
        history = ctx.rv20[max(0, k - PIT_WINDOW) : k]
        history = history[~np.isnan(history)]
        if len(history) >= PIT_MIN:
            pct_pit = percentile_rank(rv, history)
            row["rv20_pct_pit"] = pct_pit
            row["vol_tercile_pit"] = tercile(pct_pit)
    available = first_levels_day is not None and start_ts.date() >= first_levels_day
    row["gex_regime_available"] = available
    row["gex_regime"] = (
        levels.regime_at(symbol, start_ts, row["base_close_raw"]) if available else None
    )

    # Baseline: stejná minuta dne v ±60 seancích (bez seance releasu)
    neighbours = [
        (j, baseline_start(grid, event_et, sessions.days[j]))
        for j in range(
            max(0, k - BASELINE_SESSIONS), min(len(sessions.days), k + BASELINE_SESSIONS + 1)
        )
        if j != k
    ]

    # Krátké horizonty — existující funkce na výřezu barů, baseline z mřížky
    longest = max(SHORT_WINDOWS)
    window_bars = grid.bars(m - PRE_WINDOW_MIN - 10, m + longest + 1)
    lo = bisect.bisect_right(hm_sorted, start_ts)
    hi = bisect.bisect_right(hm_sorted, start_ts + dt.timedelta(minutes=longest))
    reactions = (
        {
            r.window_min: r
            for r in compute_reactions(
                start_ts, window_bars, windows=SHORT_WINDOWS, other_event_ts=hm_sorted[lo:hi]
            )
        }
        if short_ok
        else {}
    )
    for h in SHORT_WINDOWS:
        for column in short_columns(h):
            row[column] = None
        row[f"base_n_{h}m"] = 0
        sample = sampler.short(m, h) if short_ok else None
        reaction = reactions.get(h)
        if sample is None or reaction is None:
            continue
        ret, up, down, exc = sample
        excursion = measure_excursion(window_bars, start_ts, h)
        if abs(reaction.ret_bp - ret) > 1e-6:
            mismatches[f"ret_{h}m vs compute_reactions"] += 1
        if excursion is not None and abs(excursion.bp - exc) > 1e-6:
            mismatches[f"exc_{h}m vs measure_excursion"] += 1
        row[f"ret_{h}m_bp"] = ret
        row[f"up_{h}m_bp"] = up
        row[f"down_{h}m_bp"] = down
        row[f"exc_{h}m_bp"] = exc
        row[f"exc_dir_{h}m"] = 1 if up >= down else -1
        row[f"exc_z_{h}m"] = excursion.z if excursion is not None else None
        row[f"contam_{h}m"] = reaction.contaminated
        rets: list[float] = []
        excs: list[float] = []
        for _, s in neighbours:
            if has_event_in(hm_minutes, s - BASELINE_EVENT_GUARD_MIN, s + h):
                continue
            other = sampler.short(s, h)
            if other is not None:
                rets.append(other[0])
                excs.append(other[3])
        row[f"base_n_{h}m"] = len(rets)
        if len(rets) >= BASELINE_MIN_SAMPLES:
            rets_arr = np.array(rets)
            row[f"ret_abn_{h}m_bp"] = ret - float(np.median(rets_arr))
            row[f"ret_pct_{h}m"] = percentile_rank(ret, rets_arr)
            row[f"exc_pct_{h}m"] = percentile_rank(exc, np.array(excs))

    # Vícedenní horizonty — compute_daily_reactions nad seancemi ADR-0023
    daily: dict[int, Any] = {}
    if fresh and base_index is not None and first_index is not None:
        pair = [b for b in (grid.bar(base_index), grid.bar(first_index)) if b is not None]
        daily = {
            r.window_min // MINUTES_PER_TRADING_DAY: r
            for r in compute_daily_reactions(
                start_ts, pair, sessions.daily, window_days=DAILY_WINDOWS
            )
        }
    for n in DAILY_WINDOWS:
        end = k + n - 1
        closed = end < len(sessions.days)
        reaction = daily.get(n)
        ret_d: float | None = None
        if reaction is not None and not math.isnan(reaction.ret_bp):
            ret_d = reaction.ret_bp
        end_ts = grid.ts(int(sessions.settle_idx[end])) if closed else None
        row[f"closed_{n}d"] = closed
        row[f"end_session_{n}d"] = sessions.days[end] if closed else None
        row[f"ret_{n}d_bp"] = ret_d
        if ret_d is not None and base_index is not None:
            fast = sampler.daily(m, n, base_index=base_index)
            if fast is None or abs(fast[0] - ret_d) > 1e-6:
                mismatches[f"ret_{n}d vs compute_daily_reactions"] += 1
        row[f"roll_{n}d"] = (
            any(start_ts < to_utc(sw.ts) <= end_ts for sw in switches)
            if end_ts is not None
            else None
        )
        row[f"overlap_n_{n}d"] = row[f"overlap_fomc_{n}d"] = None
        if end_ts is not None:
            lo = bisect.bisect_right(overlap_ts, start_ts)
            hi = bisect.bisect_right(overlap_ts, end_ts)
            inside = overlap_sorted[lo:hi]
            row[f"overlap_n_{n}d"] = len({e.minute for e in inside})
            row[f"overlap_fomc_{n}d"] = any(e.title in FOMC_DECISION_TITLES for e in inside)
            if n == max(DAILY_WINDOWS):
                row["overlap_list_10d"] = "; ".join(
                    f"{e.minute.astimezone(ET_TZ):%m-%d %H:%M} {e.title[4:]}" for e in inside
                )
        samples: list[float] = []
        for j, s in neighbours:
            if ctx.high_session_flags[j]:
                continue
            sample_d = sampler.daily(s, n)
            if sample_d is not None:
                samples.append(sample_d[0])
        row[f"base_n_{n}d"] = len(samples)
        row[f"ret_abn_{n}d_bp"] = row[f"ret_pct_{n}d"] = None
        if ret_d is not None and len(samples) >= BASELINE_MIN_SAMPLES:
            arr = np.array(samples)
            row[f"ret_abn_{n}d_bp"] = ret_d - float(np.median(arr))
            row[f"ret_pct_{n}d"] = percentile_rank(ret_d, arr)
        # Varianta settle → settle (od settle PŘED releasem, včetně pohybu před ním);
        # nepotřebuje intradenní bary, takže projde i releasem v díře dat
        cc = sampler.settle_to_settle(k, n)
        cc_samples = [
            value
            for j, _ in neighbours
            if not ctx.high_session_flags[j]
            and (value := sampler.settle_to_settle(j, n)) is not None
        ]
        row[f"ret_{n}d_cc_bp"] = cc
        row[f"ret_abn_{n}d_cc_bp"] = row[f"ret_pct_{n}d_cc"] = None
        if cc is not None and len(cc_samples) >= BASELINE_MIN_SAMPLES:
            arr = np.array(cc_samples)
            row[f"ret_abn_{n}d_cc_bp"] = cc - float(np.median(arr))
            row[f"ret_pct_{n}d_cc"] = percentile_rank(cc, arr)
    row.setdefault("overlap_list_10d", None)
    return row


# ── Hlavní běh ─────────────────────────────────────────────────────


def build(args: argparse.Namespace) -> pd.DataFrame:
    started = time.monotonic()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    asof: dt.datetime = args.asof or dt.datetime.now(dt.UTC)

    engine = read_only_engine(args.env_file)
    all_events = load_events(engine)
    engine.dispose()
    print(f"USD scheduled událostí v DB: {len(all_events)}")

    pit = surprise_z_pit(all_events)
    full_check = surprise_z_full(all_events)
    qualifying = [
        e
        for e in all_events
        if e.impact in QUALIFYING_IMPACTS
        and e.actual is not None
        and e.forecast is not None
        and ARCHIVE_START <= e.ts <= asof
    ]
    duplicates = [
        key for key, count in Counter((e.title, e.ts) for e in qualifying).items() if count > 1
    ]
    if duplicates:
        raise SystemExit(f"Duplicitní release (titulek, čas): {duplicates}")
    unknown = sorted({e.title[4:] for e in qualifying if e.title[4:] not in SERIES})
    if unknown:
        print(f"POZOR: řady bez skupiny/polarity (group=other): {unknown}")
    print(f"Kvalifikovaných releasů: {len(qualifying)}")

    # Kontaminace krátkých oken a vyřazení z baseline: všechny USD High/Medium události
    high_medium = [e for e in all_events if e.impact in QUALIFYING_IMPACTS]
    hm_sorted = sorted({e.minute for e in high_medium})
    # Překryv vícedenních oken: High releasy s číslem a FOMC (projevy ne)
    overlap_sorted = sorted(
        (
            e
            for e in all_events
            if e.impact == "high"
            and (e.actual is not None or "FOMC" in e.title or "Federal Funds" in e.title)
        ),
        key=lambda e: (e.ts, e.id),
    )
    overlap_ts = [e.minute for e in overlap_sorted]
    high_any = [e for e in all_events if e.impact == "high"]

    # Shluky souběžných releasů a headline řada shluku / skupiny
    clusters: dict[dt.datetime, list[Event]] = defaultdict(list)
    for event in qualifying:
        clusters[event.minute].append(event)

    def rank_key(event: Event) -> tuple[int, int, str]:
        series = event.title[4:]
        return (SERIES_RANK.get(series, len(SERIES)), 0 if event.impact == "high" else 1, series)

    primary_cluster: set[int] = set()
    primary_group: set[int] = set()
    for members in clusters.values():
        ordered = sorted(members, key=rank_key)
        primary_cluster.add(ordered[0].id)
        seen: set[str] = set()
        for event in ordered:
            group = SERIES.get(event.title[4:], ("other", 0))[0]
            if group not in seen:
                seen.add(group)
                primary_group.add(event.id)

    report: dict[str, Any] = {"mixed_runs": [], "bars": {}}
    contexts, switches = build_contexts(args, asof, high_any, report)
    grid_any = contexts[SYMBOLS[0]].grid
    hm_minutes = np.array(sorted({grid_any.index(ts) for ts in hm_sorted}), dtype=np.int64)
    # mřížky obou symbolů sdílí t0 → minutové indexy událostí platí pro oba
    assert all(ctx.grid.t0 == grid_any.t0 for ctx in contexts.values())

    levels = LevelsRegimeReader(args.data_dir)
    level_dirs = [
        dt.datetime.strptime(p.name, "%Y%m%d").date()
        for p in (args.data_dir / "derived" / "ES").iterdir()
        if p.is_dir() and p.name.isdigit() and len(p.name) == 8
    ]
    first_levels_day = min(level_dirs, default=None)

    mismatches: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for event in qualifying:
        series = event.title[4:]
        group, polarity = SERIES.get(series, ("other", 0))
        surprise = event.actual - event.forecast  # type: ignore[operator]
        z_pit, z_pit_n = pit.get(event.id, (None, 0))
        full = full_check.get(event.id)
        if (
            event.surprise_z_db is not None
            and full is not None
            and abs(event.surprise_z_db - full) > 1e-6
        ):
            mismatches["surprise_z DB vs přepočet ffhistory"] += 1
        members = clusters[event.minute]
        common: dict[str, Any] = {
            "event_id": event.id,
            "ts_event": event.ts,
            "ts_event_et": event.minute.astimezone(ET_TZ).strftime("%Y-%m-%d %H:%M"),
            "weekday": event.minute.astimezone(ET_TZ).strftime("%a"),
            "title": event.title,
            "series": series,
            "group": group,
            "polarity": polarity or None,
            "impact": event.impact,
            "cluster_ts": event.minute,
            "cluster_size": len(members),
            "cluster_members": "; ".join(sorted(e.title[4:] for e in members)),
            "primary_in_cluster": event.id in primary_cluster,
            "primary_in_group": event.id in primary_group,
            "actual": event.actual,
            "forecast": event.forecast,
            "previous": event.previous,
            "surprise": surprise,
            "surprise_sign": sign(surprise),
            "surprise_econ_sign": sign(surprise) * polarity if polarity else None,
            "surprise_z": event.surprise_z_db,
            "surprise_z_pit": z_pit,
            "surprise_z_pit_n": z_pit_n,
        }
        for symbol in SYMBOLS:
            measured = measure_symbol(
                event,
                symbol,
                contexts[symbol],
                hm_minutes=hm_minutes,
                hm_sorted=hm_sorted,
                overlap_sorted=overlap_sorted,
                overlap_ts=overlap_ts,
                switches=switches,
                levels=levels,
                first_levels_day=first_levels_day,
                mismatches=mismatches,
            )
            rows.append({**common, **measured})

    frame = pd.DataFrame(rows)
    frame.to_parquet(out_dir / "release_reactions.parquet", index=False)
    frame.to_csv(out_dir / "release_reactions.csv", index=False, encoding="utf-8")
    write_side_tables(out_dir, switches, report, contexts)
    write_coverage(out_dir, frame, switches, report, contexts, mismatches, asof)
    print(f"Kontroly shody s existujícím kódem (počty rozdílů): {dict(mismatches) or 'žádné'}")
    print(f"Řádků: {len(frame)} ({frame['event_id'].nunique()} releasů × {len(SYMBOLS)} symboly)")
    print(f"Hotovo za {time.monotonic() - started:.0f} s → {out_dir}")
    return frame


def write_side_tables(
    out_dir: Path,
    switches: Sequence[RollSwitch],
    report: dict[str, Any],
    contexts: dict[str, Context],
) -> None:
    pd.DataFrame(
        [
            {
                "expiry": s.expiry,
                "switch_ts_utc": s.ts,
                "switch_weekday": s.ts.strftime("%a"),
                **{f"spread_{sym.lower()}_bp": round(v, 2) for sym, v in s.spread_bp.items()},
                "runner_up_min_bp": round(s.runner_up_bp, 2)
                if s.runner_up_bp is not None
                else None,
            }
            for s in switches
        ]
    ).to_csv(out_dir / "roll_switches.csv", index=False)
    pd.DataFrame([vars(r) for r in report["mixed_runs"]]).to_csv(
        out_dir / "mixed_contract_runs.csv", index=False
    )
    rows = []
    for symbol, ctx in contexts.items():
        for day, kind in zip(ctx.sessions.days, ctx.sessions.status, strict=True):
            if kind != "full":
                rows.append({"symbol": symbol, "session": day, "status": kind})
        rows.extend(
            {"symbol": symbol, "session": day, "status": "merged_closure"}
            for day in ctx.sessions.merged_closures
        )
    pd.DataFrame(rows).sort_values(["session", "symbol"]).to_csv(
        out_dir / "session_exceptions.csv", index=False
    )


COLUMN_NOTES = (
    "## Sloupce (výběr)",
    "",
    "- Jeden řádek = release × symbol (`symbol` ES/NQ). Souběžné releasy mají stejný `cluster_ts` "
    "a identickou reakci — skupinové statistiky filtrovat `primary_in_group` (řada) / "
    "`primary_in_cluster` (celý shluk).",
    "- `surprise` = actual − forecast; `surprise_sign`; `polarity` (+1 vyšší = silnější/inflační/"
    "jestřábí, −1 Unemployment Rate, Claims, Crude Oil Inventories); `surprise_econ_sign` = "
    "znaménko × polarita; `surprise_z` (DB, σ celé řady — pohled do budoucnosti), "
    "`surprise_z_pit` (σ jen z dřívějších releasů, n = `surprise_z_pit_n`).",
    "- Krátké okno h ∈ {5, 15, 60}: `ret_{h}m_bp` close-to-close, `up/down/exc_{h}m_bp` výchylka "
    "proti close minuty před releasem, `exc_dir_{h}m`, `exc_z_{h}m` (normalizace σ poslední "
    "hodiny, #1291), `contam_{h}m` (jiná High/Medium událost v okně), `ret_abn_{h}m_bp` = "
    "ret − medián baseline, `ret_pct_{h}m` / `exc_pct_{h}m` percentil v baseline "
    "(n = `base_n_{h}m`).",
    "- Vícedenní N ∈ {1, 2, 3, 5, 10} seancí: `ret_{N}d_bp` od posledního baru před releasem, "
    "`ret_{N}d_cc_bp` settle → settle od settle před releasem, `ret_abn_*`, `ret_pct_*`, "
    "`base_n_{N}d`, `closed_{N}d`, `end_session_{N}d`, `roll_{N}d` (okno přes přechod kontraktu, "
    "ceny back-adjusted), `overlap_n_{N}d` (další High releasy/FOMC v okně, počet minut), "
    "`overlap_fomc_{N}d`, `overlap_list_10d`.",
    "- Režim: `rv20_bp` (σ denních výnosů 20 předchozích seancí), `vol_tercile` (tercil celého "
    "vzorku), `rv20_pct_pit` / `vol_tercile_pit` (proti ≤ 252 předchozím seancím, min. 60), "
    "`gex_regime` jen od 20. 7. 2026 (`gex_regime_available`). `pre_close_bp`, `pre_60m_bp` = "
    "pohyb před releasem.",
    "",
)


def markdown_table(frame: pd.DataFrame) -> str:
    header = "| " + " | ".join(str(c) for c in frame.columns) + " |"
    divider = "|" + "|".join("---" for _ in frame.columns) + "|"
    body = [
        "| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |"
        for row in frame.itertuples(index=False)
    ]
    return "\n".join([header, divider, *body])


def write_coverage(
    out_dir: Path,
    frame: pd.DataFrame,
    switches: Sequence[RollSwitch],
    report: dict[str, Any],
    contexts: dict[str, Context],
    mismatches: Counter[str],
    asof: dt.datetime,
) -> None:
    """Pokrytí datasetu (počty releasů per řada/skupina, měřitelnost horizontů)."""
    es = frame[frame["symbol"] == "ES"]
    nq = frame[frame["symbol"] == "NQ"]

    def measured(sub: pd.DataFrame, column: str) -> int:
        return int(sub[column].notna().sum())

    per_series = []
    for (group, series), sub_es in es.groupby(["group", "series"]):
        sub_nq = nq[nq["series"] == series]
        per_series.append(
            {
                "skupina": group,
                "řada": series,
                "releasů": len(sub_es),
                "High": int((sub_es["impact"] == "high").sum()),
                "od": sub_es["ts_event"].min().date(),
                "do": sub_es["ts_event"].max().date(),
                "headline shluku": int(sub_es["primary_in_cluster"].sum()),
                "z_pit": measured(sub_es, "surprise_z_pit"),
                "ES 5m": measured(sub_es, "ret_5m_bp"),
                "ES 60m": measured(sub_es, "ret_60m_bp"),
                "ES 1d": measured(sub_es, "ret_1d_bp"),
                "ES 10d": measured(sub_es, "ret_10d_bp"),
                "NQ 5m": measured(sub_nq, "ret_5m_bp"),
                "NQ 10d": measured(sub_nq, "ret_10d_bp"),
            }
        )
    series_table = pd.DataFrame(per_series).sort_values(
        ["skupina", "releasů"], ascending=[True, False]
    )

    per_group = []
    for group, sub in es.groupby("group"):
        primary = sub[sub["primary_in_group"]]
        per_group.append(
            {
                "skupina": group,
                "řad": sub["series"].nunique(),
                "releasů (řádky)": len(sub),
                "unikátních shluků (primary_in_group)": len(primary),
                "z toho ES 5m": measured(primary, "ret_5m_bp"),
                "ES 10d": measured(primary, "ret_10d_bp"),
                "NQ 5m": measured(nq[nq["event_id"].isin(primary["event_id"])], "ret_5m_bp"),
                "překvapení +/0/− (econ)": "{}/{}/{}".format(
                    int((primary["surprise_econ_sign"] == 1).sum()),
                    int((primary["surprise_econ_sign"] == 0).sum()),
                    int((primary["surprise_econ_sign"] == -1).sum()),
                ),
            }
        )
    group_table = pd.DataFrame(per_group).sort_values(
        "unikátních shluků (primary_in_group)", ascending=False
    )

    horizon_rows = []

    def count_true(sub: pd.DataFrame, column: str) -> int:
        return int((sub[column] == True).sum())  # noqa: E712 — sloupec s None

    for symbol, sub in (("ES", es), ("NQ", nq)):
        for h in SHORT_WINDOWS:
            horizon_rows.append(
                {
                    "symbol": symbol,
                    "horizont": f"{h} min",
                    "změřeno": measured(sub, f"ret_{h}m_bp"),
                    "s baseline": measured(sub, f"ret_pct_{h}m"),
                    "cc settle→settle": "",
                    "kontaminováno": count_true(sub, f"contam_{h}m"),
                    "přes roll": "",
                    "překryv >0": "",
                    "s FOMC": "",
                }
            )
        for n in DAILY_WINDOWS:
            horizon_rows.append(
                {
                    "symbol": symbol,
                    "horizont": f"{n} seancí",
                    "změřeno": measured(sub, f"ret_{n}d_bp"),
                    "s baseline": measured(sub, f"ret_pct_{n}d"),
                    "cc settle→settle": measured(sub, f"ret_{n}d_cc_bp"),
                    "kontaminováno": "",
                    "přes roll": count_true(sub, f"roll_{n}d"),
                    "překryv >0": int(
                        (pd.to_numeric(sub[f"overlap_n_{n}d"], errors="coerce") > 0).sum()
                    ),
                    "s FOMC": count_true(sub, f"overlap_fomc_{n}d"),
                }
            )
    horizon_table = pd.DataFrame(horizon_rows)

    missing = frame[frame["ret_5m_bp"].isna()][
        ["symbol", "ts_event_et", "series", "market_closed_or_gap", "base_age_min"]
    ]
    switch_table = pd.DataFrame(
        [
            {
                "expirace": s.expiry,
                "přechod (UTC)": f"{s.ts:%Y-%m-%d %H:%M} ({s.ts:%a})",
                "spread ES bp": f"{s.spread_bp['ES']:+.1f}",
                "spread NQ bp": f"{s.spread_bp['NQ']:+.1f}",
                "2. kandidát bp": f"{s.runner_up_bp:+.1f}" if s.runner_up_bp is not None else "—",
            }
            for s in switches
        ]
    )
    session_rows = []
    for symbol, ctx in contexts.items():
        counts = Counter(ctx.sessions.status)
        session_rows.append(
            {
                "symbol": symbol,
                "seancí": len(ctx.sessions.days),
                "full": counts.get("full", 0),
                "early_close": counts.get("early_close", 0),
                "incomplete (díra)": counts.get("incomplete", 0),
                "sloučeno bez settle": len(ctx.sessions.merged_closures),
                "díry": ", ".join(
                    str(d)
                    for d, s in zip(ctx.sessions.days, ctx.sessions.status, strict=True)
                    if s == "incomplete"
                ),
            }
        )
    lines = [
        "# Pokrytí datasetu reakcí na releasy (#1296, fáze 1)",
        "",
        f"Řez: {asof:%Y-%m-%d %H:%M} UTC. Řádků {len(frame)} = "
        f"{frame['event_id'].nunique()} releasů × ES/NQ.",
        "",
        "## Skupiny (headline řádek na shluk a skupinu)",
        "",
        markdown_table(group_table),
        "",
        "## Řady",
        "",
        markdown_table(series_table),
        "",
        "## Horizonty",
        "",
        markdown_table(horizon_table),
        "",
        "## Roll kontraktů (back-adjust o skok na denní pauze)",
        "",
        markdown_table(switch_table),
        "",
        "## Seance",
        "",
        markdown_table(pd.DataFrame(session_rows)),
        "",
        "## Vyřazené řádky smíšeného kontraktu",
        "",
        markdown_table(pd.DataFrame([vars(r) for r in report["mixed_runs"]]))
        if report["mixed_runs"]
        else "Žádné.",
        "",
        "## Releasy bez krátkého měření",
        "",
        markdown_table(missing) if len(missing) else "Žádné.",
        "",
        "## Kontroly shody s existujícím kódem",
        "",
        str(
            dict(mismatches)
            or "Bez rozdílů (compute_reactions, measure_excursion, "
            "compute_daily_reactions, surprise_z)."
        ),
        "",
        *COLUMN_NOTES,
    ]
    (out_dir / "coverage.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--asof",
        type=lambda value: dt.datetime.fromisoformat(value).astimezone(dt.UTC),
        default=None,
        help="Okamžik řezu (ISO, výchozí teď) — seance se settle po něm se neberou",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    build(parse_args())
