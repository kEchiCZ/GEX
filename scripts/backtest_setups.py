"""Offline přehrání historie přes PRODUKČNÍ detektor setupů (#434).

Nepřepisuje logiku detekce ani vyhodnocení: importuje `detect_all`,
`walk_setup_path`, `r_result` z `gexlens_engine.compute.setups` a jen kolem
nich staví orchestraci, kterou jinak dělá `SetupEngine` (anti-spam per šablona,
blokace směru po sérii stopů, cooldown v kontra-režimu).

Okno aktivního řetězu (#1464, ADR-0034 doplněk 22. 10. 2026): adresář expirace
nese i minuty, kdy ji psal sekundární runtime (kadence 3 min), ale živý
`SetupEngine` běží jen nad aktivním řetězem. Replay proto přehraje expiraci E
jen v [settle předchozí expirace, settle E) — pravidlo živého rollu od #1331.
Okna před nasazením #1331 (7. 10. 2026) začínají až aktivní kadencí levels
(`live_start`): živý řetěz tehdy roloval s novým dnem UTC nebo restartem.

Otevřené setupy jdou týmž krokem jako živý engine (#1320, #1345): po cestě
ceny od baru vstupu, bary od settle se nehodnotí, timeout za close baru
končícího v settle (dřív replay timeout neměl a setup skončil jako `active`,
#1369). Díru v minutách offline nikdo nedoplní — replay ji přejde jako engine
po `PATH_GAP_WAIT` a spočítá v `gaps` řádku.

Opční toky (`call_flow` / `put_flow` / `opt_vol`) se **rekonstruují ze snapshotů**
(`data/snapshots/{symbol}/{expiry}/*.parquet`), ne z předpočítané řady — engine
je nikam neukládá, počítá si je za běhu z cache kotací. Snapshot ale nese per
minutu a kontrakt `volume` i `delta`, což jsou přesně vstupy `SetupEngine._flows`:
přírůstek volume kontraktu, vážený |delta|, sečtený zvlášť za call a put strany
(`opt_vol` je nevážený součet přírůstků). Podle mechaniky replaye jako živý
engine (`option_flows`): v6 proti maximu seance s mezerou (#1602), v5 proti
předchozí minutě se zahozením záporných přírůstků.

CumΔ podle mechaniky replaye (`cum_delta_series`, #1590): v5 midpoint řada
`flow` jako živý engine v5; v6 tisky (ADR-0057 bod 7) — seance se zdrojem
`aggressor` z `flow` (živá 1A), seance od 21. 9. 2026 do nasazení ze stínu
`cumdelta_dx`, starší seance mezera. Mezera CumΔ jde do `MinuteInputs` jako
None a `cum_gap_before` jako v živém `SetupEngine`.

Sondy nezapnutých šablon (`--probes`, #577 fáze 1): tatáž čistá funkce
`detect_damping_ceiling` jako živý sběrač (`gexlens_engine.probes`) nad bary +
uloženými Dyn profily (`gexprofile/`), vyhodnocení týmž `walk_setup_path`,
timeout v settle expirace. Bez DB — nic se nezapisuje, výsledek je jen report.
Datový kořen: `--data` nebo `GEXLENS_DATA_DIR` (default `data`).
"""

import argparse
import bisect
import dataclasses
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict, deque

import pandas as pd
from sqlalchemy import create_engine

from gexlens_engine.compute.bandregime import band_metrics, band_zone
from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR, SOURCE_MIDPOINT, stored_sources
from gexlens_engine.compute.gexfield import GexProfile, gamma_edges
from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import (
    history_expiry_settle,
    is_trading_session,
    settle_ts,
    trading_session_date,
)
from gexlens_engine.compute.setups import (
    CUMDELTA_PRINTS,
    Direction,
    MinuteInputs,
    Outcome,
    PathState,
    ProbeMinute,
    ProbeOccurrence,
    ProbeParams,
    SetupParams,
    detect_all,
    detect_damping_ceiling,
    gex_regime,
    is_counter_regime,
    max_pain_strike,
    r_result,
    walk_setup_path,
)
from gexlens_engine.storage.oi_archive import OIEodRepository

ROOT = os.environ.get("GEXLENS_DATA_DIR", "data")
DATA = f"{ROOT}/derived"


def set_data_root(root: str) -> None:
    """Přepne datový kořen (worktree nemá `data/`, produkce ji má vedle repa)."""
    global ROOT, DATA
    ROOT = root.rstrip("/\\")
    DATA = f"{ROOT}/derived"


def load_series(pattern: str) -> pd.DataFrame | None:
    return load_files(sorted(glob.glob(pattern)))


def load_bars(symbol: str, decisions: pd.Series) -> pd.DataFrame | None:
    """Bary k rozhodovacím minutám `decisions` (bar N − 1 k minutě N).

    Partice barů = UTC den jejich minut, bez duplicit mezi dny (ověřeno
    22. 10. 2026 nad ES i NQ): stačí partice dnů těchto minut — celý archiv
    (~700 dní) pro každou expiraci stál ~0,2 GB špičky (#1464).
    """
    stamps = _utc_ns(decisions) - _MINUTE
    first, last = stamps.min().date().isoformat(), stamps.max().date().isoformat()
    return load_files(
        [
            path
            for path in sorted(glob.glob(f"{DATA}/{symbol}/bars/*.parquet"))
            if first <= os.path.basename(path)[:10] <= last
        ]
    )


def load_files(files: list[str]) -> pd.DataFrame | None:
    if not files:
        return None
    frame = pd.concat([pd.read_parquet(f) for f in files])
    return frame.sort_values("ts_min").drop_duplicates("ts_min", keep="last")


def option_flows(
    symbol: str,
    expiry: str,
    prints: bool = CUMDELTA_PRINTS,
    start: dt.datetime | None = None,
) -> pd.DataFrame | None:
    """Δ-vážený opční tok per minuta ze snapshotů — zrcadlo `SetupEngine._flows`.

    Snapshot nese per minutu a kontrakt `volume` i `delta`, tedy vstupy živého
    `_flows`. Partice se čtou po dnech (expirace má ~měsíc snapshotů, najednou
    přes 0,35 GB, #1464), do dalšího dne jde jen stav kontraktů.

    - **v6** (`prints`, #1602): přírůstek proti maximu seance
      (`compute/volume.py`). Cyklus = zapsaná minuta expirace: kontrakt bez
      objemu v předchozí zapsané minutě (NULL z tasty fallbacku, chybějící
      řádek) má mezeru a jen založí stav, stejně jako první výskyt v seanci.
      Živý engine čte expiraci až od rollu (`start`, okno #1464): první minuta
      okna jen založí stav a dřívější partice se nečtou.
    - **v5** (ADR-0057 bod 11 beze změny): přírůstek proti předchozímu řádku
      kontraktu (`groupby(strike, right).shift()`), ≤ 0 se zahazuje; kontrakt,
      který se ve sweepu poprvé objeví, přírůstek nemá (NaN → zahodit) —
      shodné s `previous is None: continue` v produkci. Do dalšího dne jde
      poslední řádek každého kontraktu.
    """
    files = sorted(glob.glob(f"{ROOT}/snapshots/{symbol}/{expiry}/*.parquet"))
    parts = _flows_session_max(files, start) if prints else _flows_last_value(files)
    if not parts:
        return None
    grouped = pd.concat(parts).groupby(level=["ts_min", "right"], sort=True).sum()
    wide = grouped.unstack("right")
    return pd.DataFrame(
        {
            "ts_min": wide.index,
            "call_flow": wide.get(("weighted", "C"), pd.Series(0.0, index=wide.index)).fillna(0.0),
            "put_flow": wide.get(("weighted", "P"), pd.Series(0.0, index=wide.index)).fillna(0.0),
            "opt_vol": wide["inc"].sum(axis=1).fillna(0.0),
        }
    ).reset_index(drop=True)


_SNAPSHOT_COLUMNS = ["ts_min", "strike", "right", "volume", "delta"]
_CONTRACT = ["strike", "right"]


def _flows_last_value(files: list[str]) -> list[pd.DataFrame]:
    """Přírůstky v5 po dnech: proti předchozímu řádku kontraktu (viz `option_flows`)."""
    carry: pd.DataFrame | None = None
    parts: list[pd.DataFrame] = []
    for path in files:
        day = pd.read_parquet(path, columns=_SNAPSHOT_COLUMNS)
        if day.empty:
            continue
        day = day.assign(carried=False)
        snap = day if carry is None else pd.concat([carry, day])
        snap = snap.sort_values([*_CONTRACT, "ts_min"])
        previous = snap.groupby(_CONTRACT, sort=False)["volume"].shift()
        increment = (snap["volume"] - previous).where(lambda s: s > 0)  # ≤ 0 se přeskakuje
        snap = snap.assign(inc=increment, weighted=increment * snap["delta"].abs())
        own = snap[~snap["carried"]]
        parts.append(own.groupby(["ts_min", "right"], sort=True)[["inc", "weighted"]].sum())
        carry = snap.groupby(_CONTRACT, sort=False).tail(1)[day.columns].assign(carried=True)
    return parts


def _flows_session_max(files: list[str], start: dt.datetime | None) -> list[pd.DataFrame]:
    """Přírůstky v6 po dnech: proti maximu seance s mezerou (viz `option_flows`, #1602).

    Stav kontraktu nese do dalšího dne řádek s minutou posledního objemu,
    seancí a maximem seance ve sloupci `volume`; mezeru určuje poslední
    zapsaná minuta expirace (`written`).
    """
    first_day = start.astimezone(dt.UTC).date().isoformat() if start is not None else ""
    state: pd.DataFrame | None = None
    written: pd.Timestamp | None = None
    parts: list[pd.DataFrame] = []
    for path in files:
        if os.path.basename(path)[:10] < first_day:
            continue
        day = pd.read_parquet(path, columns=_SNAPSHOT_COLUMNS)
        if start is not None:
            day = day[day["ts_min"] >= start]
        if day.empty:
            continue
        minutes = day["ts_min"].drop_duplicates().sort_values().reset_index(drop=True)
        previous = minutes.shift()
        if written is not None:
            previous.iloc[0] = written
        written = minutes.iloc[-1]
        cycles = pd.DataFrame(
            {"ts_min": minutes, "session": _session_of(minutes), "previous_written": previous}
        )
        fed = day[day["volume"].notna()].merge(cycles, on="ts_min").assign(carried=False)
        if state is not None:
            fed = pd.concat([state.assign(carried=True), fed], ignore_index=True)
        fed = fed.sort_values([*_CONTRACT, "session", "ts_min"], kind="stable")
        seance = [*_CONTRACT, "session"]
        fed = fed.assign(maximum=fed.groupby(seance, sort=False)["volume"].cummax())
        by_seance = fed.groupby(seance, sort=False)
        reference = by_seance["maximum"].shift()
        # Kontrakt bez objemu v předchozí zapsané minutě = mezera; první výskyt
        # v seanci referenci nemá — oba jen založí stav (`volume_increment`)
        measured = reference.notna() & by_seance["ts_min"].shift().eq(fed["previous_written"])
        increment = (fed["volume"] - reference).clip(lower=0.0).where(measured)
        fed = fed.assign(inc=increment, weighted=increment * fed["delta"].abs())
        own = fed[~fed["carried"]]
        parts.append(own.groupby(["ts_min", "right"], sort=True)[["inc", "weighted"]].sum())
        last = fed.groupby(_CONTRACT, sort=False).tail(1)
        state = last[["ts_min", *_CONTRACT, "session", "delta"]].assign(volume=last["maximum"])
    return parts


#: Stín `cumdelta_dx` zastupuje živou 1A v replayi v6 od této seance do nasazení
#: v6; starší seance jsou pro v6 mezera (ADR-0057 bod 7)
SHADOW_FROM = dt.date(2026, 9, 21)
_MINUTE = pd.Timedelta(minutes=1)


def _utc_ns(ts: pd.Series) -> pd.Series:
    """Minuty jako `datetime64[ns, UTC]` — partice nesou µs, testy ns; merge chce jednu jednotku."""
    return pd.to_datetime(ts, utc=True).astype("datetime64[ns, UTC]")


def _session_of(ts: pd.Series) -> pd.Series:
    """Obchodní den minut (17:00 CT) — `settle.trading_session_date`, bez kopie vzorce."""
    return ts.map(lambda value: trading_session_date(pd.Timestamp(value).to_pydatetime()))


def _open_market(ts: pd.Timestamp) -> bool:
    """Mezera se za zavřeného trhu nezapisuje (ADR-0057 bod 3)."""
    return not is_market_closed(ts.to_pydatetime())


def shadow_rows(shadow: pd.DataFrame, session: dt.date, gaps: set[pd.Timestamp]) -> pd.DataFrame:
    """CumΔ seance ze stínu `cumdelta_dx` pro rozhodnutí v minutě (ADR-0057 bod 7, #1586).

    - Štítek L nese stav k času L + φ, kde φ je 0–60 s (smyčka v5 není zarovnaná
      na minutu). Patří proto rozhodnutí v minutě L + 1: štítek L by pustil až
      minutu tisků, které živý engine v minutě L ještě neměl.
    - Jeden chybějící štítek (přeskok smyčky) = poslední kumulativ, tok nesou
      sousední řádky.
    - Díra ≥ 2 štítky za otevřeného trhu (restart, výpadek) je mezera včetně
      prvního řádku po ní, protože tisky z doby výpadku chybí.
    - Štítek, jehož obsah [L − 1, L + 1) zasáhla minuta bez trade větve (`gaps`,
      `_shadow_gap_minutes`), je mezera; štítek po přeskoku sahá i do L − 2.
      Stín za odpojeného tasty píše tok 0, živá 1A NULL — tady i v paritě platí
      sémantika 1A.
    - Od v6 stín štítky zarovnává (stav k N, #1586), posun o 1 min je tam celé
      zpoždění. Brána parity po bumpu proto měří horní mez zpoždění historie.

    Vrací `ts_min` (minuta rozhodnutí), `session` a `cum_delta` (NaN = mezera);
    řádek, který by po posunu patřil další seanci, se zahodí.
    """
    if shadow.empty:
        return pd.DataFrame({"ts_min": [], "session": [], "cum_delta": []})
    shadow = shadow.assign(ts_min=_utc_ns(shadow["ts_min"]))
    shadow = shadow.sort_values("ts_min").drop_duplicates("ts_min", keep="last")
    level = shadow["cum_ring"] + shadow.get("cum_hot", 0.0)
    rows: list[tuple[pd.Timestamp, float]] = []
    previous: tuple[pd.Timestamp, float] | None = None
    for label, value in zip(shadow["ts_min"], level, strict=True):
        hole = False
        missing = 0
        if previous is not None:
            missing = int((label - previous[0]) / _MINUTE) - 1
            if missing == 1:
                rows.append((previous[0] + _MINUTE, previous[1]))
            elif missing > 1:
                skipped = [previous[0] + k * _MINUTE for k in range(1, missing + 1)]
                opened = [ts for ts in skipped if _open_market(ts)]
                rows.extend((ts, float("nan")) for ts in opened)
                hole = bool(opened)
        reach = [label, label + _MINUTE] + ([label - _MINUTE] if missing == 1 else [])
        gap = hole or any(ts in gaps for ts in reach)
        rows.append((label, float("nan") if gap else float(value)))
        previous = (label, float(value))
    frame = pd.DataFrame(rows, columns=["ts_min", "cum_delta"])
    frame["ts_min"] = frame["ts_min"] + _MINUTE
    frame["session"] = _session_of(frame["ts_min"])
    return frame[frame["session"] == session].reset_index(drop=True)


def _shadow_gap_minutes(symbol: str, flow: pd.DataFrame | None) -> set[pd.Timestamp]:
    """Minuty otevřeného trhu bez trade větve od `SHADOW_FROM` (#1071, ADR-0057 bod 7).

    Sémantika mezery živé 1A podle enginu, který minutu zapsal:
    - řádek `flow` se zdrojem `aggressor` (v6): mezera = `cum_delta` NULL.
      `printvol.printed` NULL tu minutu neznačí — pod tisky ho nese až bar
      s přírůstkem, klidně o desítky minut později (`CumDeltaTracker.add_bar`);
    - jinak (v5): `flow.printed_volume` NULL nebo `printvol.printed` NULL.
    """
    candidates: list[pd.Timestamp] = []
    v6_minutes: set[pd.Timestamp] = set()
    if flow is not None:
        recent = flow["session"] >= SHADOW_FROM
        v6 = flow["source"] == SOURCE_AGGRESSOR
        v6_minutes = set(flow.loc[v6, "ts_min"])
        candidates.extend(flow.loc[recent & v6 & flow["cum_delta"].isna(), "ts_min"])
        candidates.extend(flow.loc[recent & ~v6 & flow["printed_volume"].isna(), "ts_min"])
    first_day = (SHADOW_FROM - dt.timedelta(days=1)).isoformat()
    for path in sorted(glob.glob(f"{DATA}/{symbol}/*/printvol/*.parquet")):
        if os.path.basename(path)[:10] < first_day:
            continue
        part = pd.read_parquet(path, columns=["ts_min", "printed"])
        nulls = _utc_ns(part.loc[part["printed"].isna(), "ts_min"])
        candidates.extend(ts for ts in nulls if ts not in v6_minutes)
    return {ts for ts in set(candidates) if _open_market(ts)}


def _flow_frame(symbol: str) -> pd.DataFrame | None:
    """Řada `flow` symbolu; sloupce, které starší partice nemají, jsou NULL."""
    flow = load_series(f"{DATA}/{symbol}/flow/*.parquet")
    if flow is None:
        return None
    flow = flow.reset_index(drop=True)
    for column in ("source", "printed_volume", "cum_delta"):
        if column not in flow:
            flow[column] = None
    flow["source"] = flow["source"].astype(object).where(flow["source"].notna(), None)
    flow["ts_min"] = _utc_ns(flow["ts_min"])
    flow["session"] = _session_of(flow["ts_min"])
    return flow


def build_cum_series(
    symbol: str, prints: bool, *, shadow_only: bool = False
) -> pd.DataFrame | None:
    """CumΔ symbolu pro rozhodnutí v minutě: `ts_min`, `session`, `cum_delta` (NaN = mezera).

    - **v5** (midpoint): řádky `flow` zdroje midpoint (`stored_sources`); řádek
      jiného zdroje je mezera.
    - **v6** (tisky, ADR-0057 bod 7): seance, která má za otevřeného trhu řádek
      `flow` se zdrojem `aggressor`, čte živou 1A (NULL = mezera, řádek jiného
      zdroje v ní taky); hranici nesou řádky, ne datum (bod 8). Řádky z denní
      pauze se nepočítají: nasazení v pauze 16:00–17:00 CT patří ještě seanci D,
      jejíž RTH je midpoint — D čte stín, 1A až D + 1. Ostatní seance od
      `SHADOW_FROM` čtou stín (`shadow_rows`), starší jsou mezera.
    - `shadow_only` (brána parity, `replay_parity.py`): stín i v seancích s 1A,
      touž transformací jako historie — srovnává se proti živé 1A.
    """
    flow = _flow_frame(symbol)
    parts: list[pd.DataFrame] = []
    if not prints:
        if flow is None:
            return None
        own = flow["source"].map(lambda source: source in stored_sources(SOURCE_MIDPOINT))
        parts.append(
            flow.assign(cum_delta=flow["cum_delta"].where(own))[["ts_min", "session", "cum_delta"]]
        )
    else:
        live_sessions: set[dt.date] = set()
        if flow is not None and not shadow_only:
            aggressor = flow[flow["source"] == SOURCE_AGGRESSOR]
            opened = aggressor["ts_min"].map(_open_market).astype(bool)
            live_sessions = set(aggressor.loc[opened, "session"])
            live = flow[flow["session"].isin(live_sessions)]
            measured = live["cum_delta"].where(live["source"] == SOURCE_AGGRESSOR)
            parts.append(live.assign(cum_delta=measured)[["ts_min", "session", "cum_delta"]])
        gaps = _shadow_gap_minutes(symbol, flow)
        for path in sorted(glob.glob(f"{DATA}/{symbol}/cumdelta_dx/*.parquet")):
            session = dt.date.fromisoformat(os.path.basename(path)[:10])
            if session < SHADOW_FROM or session in live_sessions:
                continue
            parts.append(shadow_rows(pd.read_parquet(path), session, gaps))
    parts = [part for part in parts if not part.empty]
    if not parts:
        return None
    series = pd.concat(parts, ignore_index=True)
    series["ts_min"] = _utc_ns(series["ts_min"])
    series["cum_delta"] = series["cum_delta"].astype(float)
    return (
        series.sort_values("ts_min").drop_duplicates("ts_min", keep="last").reset_index(drop=True)
    )


_CUM: dict[tuple[str, bool, bool], pd.DataFrame | None] = {}


def cum_delta_series(
    symbol: str, prints: bool, *, shadow_only: bool = False
) -> pd.DataFrame | None:
    """`build_cum_series` jednou za symbol a mechaniku — společná všem expiracím."""
    key = (symbol, prints, shadow_only)
    if key not in _CUM:
        _CUM[key] = build_cum_series(symbol, prints, shadow_only=shadow_only)
    return _CUM[key]


def attach_cum_delta(
    frame: pd.DataFrame, series: pd.DataFrame | None, prints: bool
) -> pd.DataFrame:
    """Přidá minutám replaye `cum_delta` (NaN = mezera) a `cum_gap_before`.

    Hodnota pod v6 = poslední řádek řady téže seance do minuty včetně (živý engine
    drží `runtime.last_flow`); pod v5 jen řádek téže minuty jako před #1590,
    chybějící je mezera. `cum_gap_before` jako živý `SetupEngine` (ADR-0057 bod 4):
    mezi minulým a tímto řádkem historie leží minuta s mezerou (minuta bez baru
    nebo levels do historie nejde), a pod v6 i hranice seance 17:00 CT (#1581).
    """
    frame = frame.assign(ts_min=_utc_ns(frame["ts_min"])).sort_values("ts_min")
    frame = frame.reset_index(drop=True)
    frame["session"] = _session_of(frame["ts_min"])
    if series is None or series.empty:
        frame["cum_delta"] = float("nan")
        gap_ts: list[pd.Timestamp] = []
    else:
        frame = pd.merge_asof(
            frame,
            series[["ts_min", "session", "cum_delta"]],
            on="ts_min",
            by="session",
            direction="backward",
            tolerance=None if prints else pd.Timedelta(0),
        )
        gap_ts = list(series.loc[series["cum_delta"].isna(), "ts_min"])
    flags = [False] * len(frame)
    times = list(frame["ts_min"])
    sessions = list(frame["session"])
    for index in range(1, len(frame)):
        lo = bisect.bisect_right(gap_ts, times[index - 1])
        hi = bisect.bisect_left(gap_ts, times[index])
        flags[index] = hi > lo or (prints and sessions[index] != sessions[index - 1])
    frame["cum_gap_before"] = flags
    return frame


def expiry_end(symbol: str, expiry: str) -> dt.datetime | None:
    """Settle expirace dne z partic (#1331). Partice trading class řetězu nenesou —
    kvartální datum se dovodí z tickeru jako v přepočtu setupů (#1366,
    `settle.history_expiry_settle`): kořenový ticker = týdenní série 16:00 ET."""
    return history_expiry_settle(expiry, symbol)[0]


def previous_expiry(expiry: str) -> str:
    """Expirace řetězu před `expiry` = předchozí obchodní den (ES/NQ mají denní
    expirace; pondělí → pátek, svátek se přeskočí přes `is_trading_session`)."""
    day = dt.datetime.strptime(expiry, "%Y%m%d").date() - dt.timedelta(days=1)
    while not is_trading_session(day):
        day -= dt.timedelta(days=1)
    return day.strftime("%Y%m%d")


def active_window(symbol: str, expiry: str) -> tuple[dt.datetime, dt.datetime]:
    """Okno aktivního řetězu expirace: [settle předchozí expirace, settle E) (#1464).

    Živá pipeline roluje v settle expirace (#1331), takže E je aktivní od settle
    předchozí expirace do vlastního settle; dřív ji psal jen sekundární runtime.
    Minuta settle E patří už další expiraci: replay v ní setup nezaloží
    (`born_after_settle`) ani nehodnotí (`walk_setup_path` bere minuty před
    settle) a otevřený setup uzavře timeout už v minutě před ní (`now` = settle).
    """
    start = expiry_end(symbol, previous_expiry(expiry))
    end = expiry_end(symbol, expiry)
    if start is None or end is None:
        raise ValueError(f"Nečitelná expirace {expiry!r}")
    return start, end


def settled(symbol: str, expiry: str, now: dt.datetime) -> bool:
    """Nastal settle expirace proti okamžiku běhu `now` (UTC)? Neuzavřená seance
    nese jen minuty do teď a setupy bez výsledku — do denní ΣR nepatří (#1464)."""
    end = expiry_end(symbol, expiry)
    if end is None:
        raise ValueError(f"Nečitelná expirace {expiry!r}")
    return now >= end


#: Nasazení #1331: od 7. 10. 2026 živá pipeline roluje v settle expirace
ROLL_AT_SETTLE_FROM = dt.datetime(2026, 10, 7, tzinfo=dt.UTC)
#: Aktivní kadence = tolik minut levels po sobě; sekundár píše jednou za 3 min
ACTIVE_RUN = 3


def live_start(levels: pd.Series, start: dt.datetime) -> dt.datetime | None:
    """Odkdy byla E živě aktivní v okně od `start` (#1464).

    Do nasazení #1331 roloval živý řetěz až s novým dnem UTC nebo restartem:
    večer po settle předchozí expirace psal E jen sekundár (kadence 3 min)
    a živý engine nad ní nehledal. Začátek = první minuta levels od `start`,
    po které jdou další bez mezery; None = aktivní kadence chybí. Od #1331
    je E aktivní hned od `start`.
    """
    if start >= ROLL_AT_SETTLE_FROM:
        return start
    stamps = _utc_ns(levels)
    present = set(stamps[stamps >= start])
    for ts in sorted(present):
        if all(ts + k * _MINUTE in present for k in range(1, ACTIVE_RUN)):
            return ts.to_pydatetime()
    return None


@dataclasses.dataclass
class WindowSkip:
    """Co replay vynechal kvůli oknu aktivního řetězu (#1464) — hlavička walk-forwardu."""

    #: minut (bar ∩ levels) mimo [settle předchozí expirace, settle E),
    #: před #1331 i večer před aktivní kadencí (`live_start`)
    minutes: int = 0
    #: expirace, jejichž settle proti času běhu ještě nenastal
    unsettled: list[str] = dataclasses.field(default_factory=list)


def max_pain_for(repo: OIEodRepository, symbol: str, expiry: str, day: dt.date) -> float | None:
    try:
        records = repo.values_for(symbol, expiry, day)
    except Exception:
        return None
    oi_map = {(r.strike, r.right): r.oi for r in records}
    return max_pain_strike(oi_map) if oi_map else None


def build_minutes(
    symbol: str,
    expiry: str,
    repo: OIEodRepository,
    prints: bool = CUMDELTA_PRINTS,
    *,
    shadow_only: bool = False,
    skip: WindowSkip | None = None,
) -> list[MinuteInputs]:
    """MinuteInputs jednoho obchodního dne (expirace = adresář derived).

    Jen minuty v okně aktivního řetězu (`active_window`, před #1331 od
    `live_start`, #1464); kolik jich padlo mimo, přičte do `skip`. `prints` =
    mechanika replaye: CumΔ z tisků (v6, ADR-0057 bod 7), jinak midpoint (v5);
    výchozí je mechanika kódu.
    `shadow_only` čte pod v6 stín i v seancích s 1A (brána parity,
    `cum_delta_series`).
    """
    levels = load_series(f"{DATA}/{symbol}/{expiry}/levels/*.parquet")
    dom = load_series(f"{DATA}/{symbol}/{expiry}/walldom/*.parquet")
    bars = None if levels is None else load_bars(symbol, levels["ts_min"])
    if bars is None or levels is None:
        return []
    # Decision-time konvence (#794 f. 1, změřeno 24. 8.): živý engine v minutě N
    # rozhoduje nad DOKONČENÝM barem N−1 + stavem (levels/flow/profil) N.
    # Původní párování bar N + stav N leakovalo minutu budoucí ceny do řádku —
    # parita s živým feature logem to doložila (bar blok seděl až s posunem +1).
    # POZOR: mění výsledky backtestů proti kalibracím #394/#434 (běžely s leakem).
    bars = bars.copy()
    bars["ts_min"] = bars["ts_min"] + pd.Timedelta(minutes=1)
    frame = bars.merge(levels, on="ts_min", how="inner")
    # Sekundární minuty před rollem a minuty od settle živý engine nad E
    # nehledá (#1464) — filtr jen minut, zdroje CumΔ ani toků se nemění
    start, settle = active_window(symbol, expiry)
    start = live_start(levels["ts_min"], start) or settle
    stamps = _utc_ns(frame["ts_min"])
    inside = (stamps >= start) & (stamps < settle)
    if skip is not None:
        skip.minutes += int((~inside).sum())
    frame = frame[inside]
    if dom is not None:
        frame = frame.merge(dom, on="ts_min", how="left")
    flows = option_flows(symbol, expiry, prints, start)
    if flows is not None:
        frame = frame.merge(flows, on="ts_min", how="left")
    if frame.empty:
        return []
    # CumΔ podle mechaniky + mezera mezi řádky historie (#1590); minuta bez řádku
    # bere poslední hodnotu seance jako živý engine (dřív left merge → NaN)
    series = cum_delta_series(symbol, prints, shadow_only=shadow_only)
    frame = attach_cum_delta(frame, series, prints)
    # Hranice gamma masy (#600) z uložených Dyn GEX profilů — dřív harness
    # tohle pole nechával None, takže se live vs. replay lišily (#796)
    edges_by_ts: dict[object, tuple[float | None, float | None]] = {}
    profiles = load_series(f"{DATA}/{symbol}/{expiry}/gexprofile/*.parquet")
    if profiles is not None:
        for prof in profiles.itertuples():
            gp = GexProfile(
                ts_min=prof.ts_min,
                grid_start=float(prof.grid_start),
                grid_step=float(prof.grid_step),
                values=tuple(float(v) for v in prof.values),
            )
            edges = gamma_edges(gp)
            edges_by_ts[prof.ts_min] = (edges.up, edges.dn)
    day = pd.Timestamp(frame.ts_min.iloc[-1]).date()
    pain = max_pain_for(repo, symbol, expiry, day)
    # Settle vlastní expirace = konec života setupu, táž hranice jako živý
    # SetupEngine (`expiry_end`, ADR-0039 bod 2, DST #511, #1366; dřív pevně
    # 20:00 UTC) a konec okna: otevřený setup uzavře timeout v settle — viz `replay`

    minutes: list[MinuteInputs] = []
    for row in frame.itertuples():
        ts = pd.Timestamp(row.ts_min).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        left = (settle - ts).total_seconds() / 60.0
        minutes.append(
            MinuteInputs(
                ts=ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                flip=none_if_nan(getattr(row, "flip", None)),
                call_wall=none_if_nan(getattr(row, "call_wall", None)),
                put_wall=none_if_nan(getattr(row, "put_wall", None)),
                max_pain=pain,
                # Mezera CumΔ = None, ne 0,0 (ADR-0057 bod 3): nula by byla
                # vymyšlená hodnota, kterou by T4 brala jako potvrzení
                cum_delta=none_if_nan(row.cum_delta),
                call_flow=float(getattr(row, "call_flow", 0.0) or 0.0),
                put_flow=float(getattr(row, "put_flow", 0.0) or 0.0),
                opt_vol=float(getattr(row, "opt_vol", 0.0) or 0.0),
                minutes_to_expiry=left if left > 0 else None,
                call_wall_dom=none_if_nan(getattr(row, "call_wall_dom", None)),
                put_wall_dom=none_if_nan(getattr(row, "put_wall_dom", None)),
                gex_regime=gex_regime(
                    float(row.close),
                    none_if_nan(getattr(row, "flip", None)),
                    float(getattr(row, "total_gex", 0.0) or 0.0),
                ),
                gamma_edge_up=edges_by_ts.get(row.ts_min, (None, None))[0],
                gamma_edge_dn=edges_by_ts.get(row.ts_min, (None, None))[1],
                cum_gap_before=bool(row.cum_gap_before),
            )
        )
    return minutes


def none_if_nan(value):
    if value is None:
        return None
    try:
        return None if pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


@dataclasses.dataclass
class OpenSetup:
    template: str
    direction: Direction
    entry: float
    target: float
    stop: float
    created: dt.datetime
    counter: bool
    #: Cesta ceny od baru vstupu (#1345) — vyhodnocuje se `walk_setup_path`
    path: PathState


def replay(
    minutes: list[MinuteInputs], params: SetupParams, expiry: str, symbol: str
) -> list[dict]:
    """Přehraje den expirace `expiry`; vrací uzavřené i otevřené setupy s výsledkem v R.

    Invariant vzniku (#1324) jako živý `SetupEngine._detect_new`: v minutě po
    settle vlastní expirace (`expiry_end`, #1366) žádný kandidát nevznikne —
    živě je to jediný cyklus minuty settle, po kterém pipeline roluje (#1331).

    Vyhodnocení týmž `walk_setup_path` jako živý engine (#1345, #1369): bary
    v pořadí od baru vstupu, bary od settle se nehodnotí a setup otevřený
    v settle skončí timeoutem za close baru končícího v settle (`closed` =
    settle). Díru v minutách (trh běžel, bar chybí) offline nikdo nedoplní —
    replay ji přejde jako engine po `PATH_GAP_WAIT` a počet děr vrátí v `gaps`.
    """
    settle = expiry_end(symbol, expiry)
    history: list[MinuteInputs] = []
    open_setups: list[OpenSetup] = []
    done: list[dict] = []
    last_created: dict[str, dt.datetime] = {}
    last_counter_stop: dict[str, dt.datetime] = {}
    dir_stops: dict[str, int] = defaultdict(int)
    dir_blocked: dict[str, dt.datetime] = {}

    gaps: dict[int, int] = defaultdict(int)
    for now in minutes:
        history.append(now)
        # 1) Vyhodnocení otevřených po cestě ceny (stop-first uvnitř svíčky,
        # timeout v settle) — týž krok jako SetupEngine; offline se díra přejde
        still: list[OpenSetup] = []
        for item in open_setups:
            step = walk_setup_path(
                item.direction,
                item.entry,
                item.target,
                item.stop,
                settle,
                [now],
                item.path,
                now=now.ts + dt.timedelta(minutes=1),
                force_until=now.ts,
            )
            gaps[id(item)] += len(step.gaps)
            item.path = step.state
            outcome = step.outcome
            if outcome is None:
                still.append(item)
                continue
            exit_price = step.exit_price if step.exit_price is not None else item.entry
            result = r_result(item.direction, item.entry, item.stop, exit_price)
            done.append(
                {
                    "template": item.template,
                    "direction": item.direction.value,
                    "created": item.created,
                    "closed": step.closed_ts,
                    "outcome": outcome.value,
                    "r": result,
                    "gaps": gaps.pop(id(item), 0),
                }
            )
            side = item.direction.value
            closed_ts = step.closed_ts or now.ts
            if outcome is Outcome.STOP:
                if item.counter:
                    last_counter_stop[item.template] = closed_ts
                dir_stops[side] += 1
                if dir_stops[side] >= params.max_stops_per_direction:
                    dir_blocked[side] = closed_ts + dt.timedelta(
                        minutes=params.direction_block_minutes
                    )
            elif outcome is Outcome.TARGET:
                dir_stops[side] = 0
                dir_blocked.pop(side, None)
        open_setups = still

        # 2) Nové kandidáty přes produkční detect_all — ne po settle expirace (#1324)
        if settle is not None and now.ts >= settle:  # `born_after_settle` (#1366)
            continue
        open_templates = {item.template for item in open_setups}
        for candidate in detect_all(history, params):
            template = candidate.template.value
            if template in open_templates:
                continue
            last = last_created.get(template)
            if last is not None and (now.ts - last).total_seconds() < params.cooldown_minutes * 60:
                continue
            blocked = dir_blocked.get(candidate.direction.value)
            if blocked is not None and now.ts < blocked:
                continue
            counter = is_counter_regime(candidate.direction, candidate.context.get("gex_regime"))
            if counter:
                stop_ts = last_counter_stop.get(template)
                if (
                    stop_ts is not None
                    and (now.ts - stop_ts).total_seconds()
                    < params.counter_stop_cooldown_minutes * 60
                ):
                    continue
            last_created[template] = now.ts
            open_setups.append(
                OpenSetup(
                    template=template,
                    direction=candidate.direction,
                    entry=candidate.entry,
                    target=candidate.target,
                    stop=candidate.stop,
                    created=now.ts,
                    counter=counter,
                    # Bar vstupu = minuta vzniku (entry je její close)
                    path=PathState(last_ts=now.ts, last_close=now.close),
                )
            )
            open_templates.add(template)

    for item in open_setups:  # data skončila před settle = bez výsledku
        done.append(
            {
                "template": item.template,
                "direction": item.direction.value,
                "created": item.created,
                "closed": None,
                "outcome": "active",
                "r": None,
                "gaps": gaps.get(id(item), 0),
            }
        )
    return done


def load_profiles(symbol: str, expiry: str) -> dict[object, GexProfile]:
    """Dyn GEX profily expirace per ts_min (partice `gexprofile/{den}.parquet`)."""
    profiles = load_series(f"{DATA}/{symbol}/{expiry}/gexprofile/*.parquet")
    if profiles is None:
        return {}
    return {
        prof.ts_min: GexProfile(
            ts_min=prof.ts_min,
            grid_start=float(prof.grid_start),
            grid_step=float(prof.grid_step),
            values=tuple(float(v) for v in prof.values),
        )
        for prof in profiles.itertuples()
    }


_BARS: dict[str, pd.DataFrame | None] = {}


def cached_bars(symbol: str) -> pd.DataFrame | None:
    """Bary podkladu jsou společné všem expiracím — načíst jednou, ne 50×."""
    if symbol not in _BARS:
        _BARS[symbol] = load_series(f"{DATA}/{symbol}/bars/*.parquet")
    return _BARS[symbol]


def build_probe_minutes(symbol: str, expiry: str) -> list[ProbeMinute]:
    """Minuty seance expirace pro sondy: dokončený bar N−1 + profil N (decision-time).

    Seance = (settle předchozího dne, settle expirace): adresář expirace nese
    i profily z dob, kdy byla sekundárním řetězem (#442), a minuty po settle
    expirace (`expiry_end`, #1366) už pipeline neběží (roll #1331).
    Minuta bez profilu v seanci zůstává se zónou None (#1345) — jako živý
    sběrač, kterému chybí `last_profile`: přechod se v ní nepočítá, ale její
    bar patří cestě ceny otevřených sond (dřív se vynechala a stop v ní
    se ztratil).
    """
    bars = cached_bars(symbol)
    profiles = load_profiles(symbol, expiry)
    if bars is None or not profiles:
        return []
    expiry_day = dt.datetime.strptime(expiry, "%Y%m%d").date()
    session_end = expiry_end(symbol, expiry)
    if session_end is None:
        return []
    session_start = settle_ts(expiry_day - dt.timedelta(days=1))
    bars = bars.copy()
    bars["ts_min"] = bars["ts_min"] + pd.Timedelta(minutes=1)  # bar N−1 → rozhodnutí v N
    minutes: list[ProbeMinute] = []
    for row in bars.itertuples():
        ts = pd.Timestamp(row.ts_min).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        if not (session_start < ts <= session_end):
            continue
        profile = profiles.get(row.ts_min)
        close = float(row.close)
        metrics = band_metrics(profile, close) if profile is not None else None
        minutes.append(
            ProbeMinute(
                ts=ts,
                high=float(row.high),
                low=float(row.low),
                close=close,
                zone=band_zone(profile, close) if profile is not None else None,
                band_depth=metrics.depth if metrics is not None else None,
            )
        )
    return minutes


@dataclasses.dataclass(frozen=True)
class _PathBar:
    """Bar cesty ceny sondy: `ProbeMinute.ts` je minuta rozhodnutí, bar je o minutu dřív."""

    ts: dt.datetime
    high: float
    low: float
    close: float


@dataclasses.dataclass
class OpenProbe:
    occurrence: ProbeOccurrence
    path: PathState


def replay_probes(
    minutes: list[ProbeMinute], params: ProbeParams, settle: dt.datetime | None = None
) -> list[dict]:
    """Přehraje seanci přes `detect_damping_ceiling`; výsledek týmž `walk_setup_path`.

    Zrcadlo `T9ProbeCollector` (#1345): okno 2 × akceptace minut, otevřené sondy
    jdou po cestě ceny od baru vstupu (stop-first uvnitř svíčky, bary od settle
    se nehodnotí, timeout za close baru končícího v settle, `closed` = settle),
    MFE/MAE v R. Díru v minutách offline nikdo nedoplní — přejde se a spočítá
    v `gaps`. Bez `settle` (testy) uzavře konec dat timeoutem za poslední close.
    """
    history: deque[ProbeMinute] = deque(maxlen=2 * params.acceptance_minutes)
    open_probes: list[OpenProbe] = []
    done: list[dict] = []
    gaps: dict[int, int] = defaultdict(int)

    def bar_of(minute: ProbeMinute) -> _PathBar:
        return _PathBar(minute.ts - dt.timedelta(minutes=1), minute.high, minute.low, minute.close)

    def close_row(
        probe: OpenProbe, outcome: Outcome, closed: dt.datetime, exit_price: float
    ) -> dict:
        item = probe.occurrence
        risk = abs(item.entry - item.stop)
        return {
            "template": item.template,
            "direction": item.direction.value,
            "created": item.ts,
            "closed": closed,
            "outcome": outcome.value,
            "r": r_result(item.direction, item.entry, item.stop, exit_price),
            "mfe_r": probe.path.mfe / risk if risk > 0 else None,
            "mae_r": probe.path.mae / risk if risk > 0 else None,
            "gaps": gaps.pop(id(probe), 0),
            "context": item.context,
        }

    def step(probe: OpenProbe, bars: list[_PathBar], now: dt.datetime) -> bool:
        """Krok cesty; True = sonda uzavřena (řádek zapsán)."""
        item = probe.occurrence
        result = walk_setup_path(
            item.direction,
            item.entry,
            item.target,
            item.stop,
            settle,
            bars,
            probe.path,
            now=now,
            force_until=now,
        )
        gaps[id(probe)] += len(result.gaps)
        probe.path = result.state
        if result.outcome is None:
            return False
        exit_price = result.exit_price if result.exit_price is not None else item.entry
        closed = result.closed_ts if result.closed_ts is not None else now
        done.append(close_row(probe, result.outcome, closed, exit_price))
        return True

    for now in minutes:
        bar = bar_of(now)
        open_probes = [probe for probe in open_probes if not step(probe, [bar], now.ts)]
        if settle is not None and now.ts >= settle:
            continue  # minuta settle a po ní — sonda nevzniká (`born_after_settle`, #1331)
        history.append(now)
        occurrence = detect_damping_ceiling(list(history), params)
        if occurrence is not None:
            open_probes.append(
                OpenProbe(
                    occurrence=occurrence, path=PathState(last_ts=bar.ts, last_close=bar.close)
                )
            )

    if settle is not None:
        open_probes = [probe for probe in open_probes if not step(probe, [], settle)]
    if minutes:
        last = minutes[-1]
        for probe in open_probes:  # data skončila dřív než settle
            done.append(close_row(probe, Outcome.TIMEOUT, last.ts, last.close))
    return done


def summarize_probes(rows: list[dict]) -> dict:
    """Souhrn sond: n, výsledky, MFE/MAE v R — surovina fáze 2 (#577)."""
    base = summarize(rows)
    mfe = [r["mfe_r"] for r in rows if r.get("mfe_r") is not None]
    mae = [r["mae_r"] for r in rows if r.get("mae_r") is not None]
    outcomes = defaultdict(int)
    for row in rows:
        outcomes[row["outcome"]] += 1
    return {
        **base,
        "Ø MFE R": round(sum(mfe) / len(mfe), 2) if mfe else None,
        "Ø MAE R": round(sum(mae) / len(mae), 2) if mae else None,
        "výsledky": dict(sorted(outcomes.items())),
    }


def probe_report(symbols: list[str], params: ProbeParams) -> dict:
    """Sondy T9 nad všemi expiracemi v `derived/` — per instrument, šablona, směr."""
    report: dict = {}
    for symbol in symbols:
        expiries = sorted(
            os.path.basename(p)
            for p in glob.glob(f"{DATA}/{symbol}/*")
            if os.path.basename(p).isdigit()
        )
        rows: list[dict] = []
        per_day: dict[str, int] = {}
        days = 0
        for expiry in expiries:
            minutes = build_probe_minutes(symbol, expiry)
            if len(minutes) < 60:
                continue
            days += 1
            day_rows = replay_probes(minutes, params, expiry_end(symbol, expiry))
            rows.extend(day_rows)
            if day_rows:
                per_day[expiry] = len(day_rows)
        per_kind: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            per_kind[f"{row['template']} {row['direction']}"].append(row)
        report[symbol] = {
            "dnů s daty": days,
            "výskytů": len(rows),
            "per šablona a směr": {
                kind: summarize_probes(krows) for kind, krows in sorted(per_kind.items())
            },
            "výskytů po dnech": per_day,
        }
    return report


def summarize(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["r"] is not None]
    wins = [r for r in closed if r["outcome"] == Outcome.TARGET.value]
    total_r = sum(r["r"] for r in closed)
    return {
        "setupů": len(rows),
        "uzavřených": len(closed),
        "úspěšnost %": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "Ø R": round(total_r / len(closed), 2) if closed else 0.0,
        "Σ R": round(total_r, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--db", default=os.environ.get("GEXLENS_DATABASE_URL", ""))
    parser.add_argument("--data", default=ROOT, help="kořen dat (derived/, snapshots/)")
    parser.add_argument(
        "--probes",
        action="store_true",
        help="jen sondy T9 (#577) nad bary + profily; bez DB, nic se nezapisuje",
    )
    args = parser.parse_args()
    set_data_root(args.data)

    if args.probes:
        emit(probe_report(args.symbols.split(","), ProbeParams()))
        return

    repo = OIEodRepository(create_engine(args.db)) if args.db else None
    now = dt.datetime.now(dt.UTC)

    # Produkční default = absolutní prahy (násobky ATR jsou 0, viz #434)
    configs = {
        "baseline (produkční prahy)": SetupParams(),
        "ATR škálované (#434)": SetupParams(wall_zone_atr=1.9, rejection_min_atr=0.6),
    }

    report: dict = {}
    for symbol in args.symbols.split(","):
        expiries = sorted(
            os.path.basename(p)
            for p in glob.glob(f"{DATA}/{symbol}/*")
            if os.path.basename(p).isdigit()
        )
        per_config: dict[str, list[dict]] = {name: [] for name in configs}
        per_template: dict[str, dict[str, list[dict]]] = {
            name: defaultdict(list) for name in configs
        }
        per_day: dict[str, dict[str, float]] = defaultdict(dict)
        for expiry in expiries:
            if not settled(symbol, expiry, now):
                continue  # neuzavřená seance (#1464)
            minutes = build_minutes(symbol, expiry, repo) if repo else []
            if len(minutes) < 60:
                continue
            for name, params in configs.items():
                rows = replay(minutes, params, expiry, symbol)
                per_config[name].extend(rows)
                for row in rows:
                    per_template[name][row["template"]].append(row)
                closed = [r["r"] for r in rows if r["r"] is not None]
                per_day[expiry][name] = round(sum(closed), 2)
        report[symbol] = {
            "dnů": len(expiries),
            "Σ R po dnech": dict(sorted(per_day.items())),
            "konfigurace": {
                name: {
                    "celkem": summarize(rows),
                    "per šablona": {
                        tmpl: summarize(trows) for tmpl, trows in sorted(per_template[name].items())
                    },
                }
                for name, rows in per_config.items()
            },
        }
    emit(report)


def emit(report: dict) -> None:
    out = os.environ.get("BACKTEST_OUT")
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"zapsáno: {out}".encode("ascii", "replace").decode())
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
