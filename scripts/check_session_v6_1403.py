"""Kontrola seance pod mechanikou v6 jen čtením partic (#1403, #1602, #1338 D-b).

Ověření v6 v RTH (#1403, podmínka 3 „připraveno“ v #1385: seance bez díry v minutách
a s pokrytím CumΔ) se opakuje denně; skript dává per symbol:

- **levels** `derived/{sym}/{expiry}/levels`: minuty v okně a chybějící minuty;
- **flow** `derived/{sym}/flow`: minuty, NULL `cum_delta` (mezera = výpadek trade větve
  nebo reconnect DXLink, ADR-0057 bod 2; jinak nález), zdroj řádků a max |`flow_delta`|
  s minutou (skok v pauze, AC #1602);
- **snapshots** `snapshots/{sym}/{expiry}`: minuty s aspoň jedním čerstvým řádkem
  s `volume` (IBKR; extended expirace z tasty `volume` nenesou, ADR-0058);
- **AC #1602**: Σ `printvol.volume_delta` v okně × Σ přírůstků proti maximu seance
  ze snapshotů. Přírůstky počítá `compute/volume.SessionVolumes` — tatáž čistá funkce
  jako engine, cyklus = minuta snapshotu, stav od otevření seance. Kotace starší než
  `quote_max_age_s` engine do trackeru nepouští (mezera), skript ji bere jako řádek
  bez objemu. Restart enginu v seanci nebo cyklus, který spadl po začátku, dá rozdíl
  v minutě po něm (engine po něm jen založí stav).

Okno je polouzavřené [od, do) v UTC. Výchozí = RTH seance [`us_open_ts`, `settle_ts`)
(14:30–21:00 UTC v zimě, 13:30–20:00 v létě, zkrácená seance do 13:00 ET).
`--from`/`--to` HH:MM UTC se mapují do 24 h od otevření seance (`session_bounds`):
celá seance v zimě `--from 23:00 --to 22:00`, denní pauza `--from 22:00 --to 23:00`.
Očekávané minuty jsou jen minuty otevřeného trhu (`marketclock.is_market_closed`),
v pauze se tak kontroluje jen skok toku. `--session` musí být obchodní den
(`settle.is_trading_session`). Okno se ořízne na teď.

Spouštěj po settle: čtení z hostitele drží partici otevřenou a souběh se zápisem enginu
(`os.replace`) přes bind mount ověřený není. Za seance jen výjimečně.

Spuštění (z kořene repa, `--data` = kořen dat; nic nezapisuje):
    python -I -X utf8 scripts/check_session_v6_1403.py --data data --session 2026-11-02
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "engine" / "src"))

import pyarrow as pa  # noqa: E402
import pyarrow.compute as pc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from gexlens_engine.compute.marketclock import is_market_closed  # noqa: E402
from gexlens_engine.compute.settle import (  # noqa: E402
    is_trading_session,
    session_bounds,
    settle_ts,
    us_open_ts,
)
from gexlens_engine.compute.volume import SessionVolumes  # noqa: E402
from gexlens_engine.config import Settings  # noqa: E402
from gexlens_engine.storage.parquet_store import (  # noqa: E402
    FLOW_SCHEMA,
    LEVELS_SCHEMA,
    PRINTVOL_SCHEMA,
    SNAPSHOT_SCHEMA,
)

#: Kolik položek seznamu (chybějící minuty, rozdíly) se vypíše
SHOW = 20


def _hm(value: str) -> dt.time:
    hour, minute = map(int, value.split(":"))
    return dt.time(hour, minute)


def window(
    session: dt.date, start: dt.time | None, end: dt.time | None
) -> tuple[dt.datetime, dt.datetime]:
    """Okno [od, do) v UTC; bez `start`/`end` RTH seance, jinak HH:MM v 24 h od otevření.

    `start` padne do [open, open + 24 h), `end` do (open, open + 24 h] — `--from 23:00`
    je tak otevření seance a `--to 23:00` její konec za 24 h. Den bez seance je chyba.
    """
    if not is_trading_session(session):
        raise ValueError(f"{session} není obchodní den (víkend nebo svátek)")
    open_ts, _ = session_bounds(session)
    lo = us_open_ts(session) if start is None else _in_session(open_ts, start, end=False)
    hi = settle_ts(session) if end is None else _in_session(open_ts, end, end=True)
    if hi <= lo:
        raise ValueError(f"prázdné okno {lo:%d. %m. %H:%M}–{hi:%d. %m. %H:%M} UTC")
    return lo, hi


def _in_session(open_ts: dt.datetime, at: dt.time, *, end: bool) -> dt.datetime:
    ts = dt.datetime.combine(open_ts.date(), at, tzinfo=dt.UTC)
    if ts < open_ts or (end and ts == open_ts):
        ts += dt.timedelta(days=1)
    return ts


def minutes(lo: dt.datetime, hi: dt.datetime) -> list[dt.datetime]:
    return [lo + dt.timedelta(minutes=i) for i in range(int((hi - lo).total_seconds() // 60))]


def open_minutes(lo: dt.datetime, hi: dt.datetime) -> list[dt.datetime]:
    """Minuty okna za otevřeného trhu — jen v nich se data očekávají."""
    return [m for m in minutes(lo, hi) if not is_market_closed(m)]


def fresh_volume(row: dict[str, Any], max_age: float) -> float | None:
    """Objem řádku, jak ho vidí tracker enginu: zmrzlá kotace (stáří > `max_age`) = None."""
    age = row["stale_age"]
    if age is not None and age > max_age:
        return None
    volume: float | None = row["volume"]
    return volume


def increments_by_minute(
    rows: Iterable[dict[str, Any]], max_age: float
) -> tuple[dict[dt.datetime, float], int]:
    """Přírůstky objemu proti maximu seance per minuta a počet poklesů feedu.

    Řádky jedné seance (`ts_min`, `strike`, `right`, `volume`, `stale_age`); cyklus = minuta.
    """
    by_min: defaultdict[dt.datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_min[row["ts_min"]].append(row)
    volumes = SessionVolumes()
    out: defaultdict[dt.datetime, float] = defaultdict(float)
    declines = 0
    for minute in sorted(by_min):
        for row in by_min[minute]:
            volume = fresh_volume(row, max_age)
            if volume is None:
                continue
            step = volumes.add((row["strike"], row["right"]), volume)
            declines += step.declined
            if step.increment:
                out[minute] += step.increment
        volumes.end_cycle()
    return dict(out), declines


def _rows(
    paths: Iterable[Path], schema: pa.Schema, lo: dt.datetime, hi: dt.datetime
) -> list[dict[str, Any]]:
    # Se schématem: sloupec, který starší partice nemá, je NULL (lessons-learned #1390)
    out: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        table = pq.read_table(path, columns=schema.names, schema=schema)
        ts = table.column("ts_min")
        out.extend(table.filter(pc.and_(pc.greater_equal(ts, lo), pc.less(ts, hi))).to_pylist())
    return out


def _schema(full: pa.Schema, names: list[str]) -> pa.Schema:
    return pa.schema([full.field(name) for name in names])


def _fmt(items: list[dt.datetime]) -> str:
    return f"{len(items)}" + (f" {[m.strftime('%H:%M') for m in items[:SHOW]]}" if items else "")


def check_symbol(
    data: Path,
    symbol: str,
    expiry: str,
    session: dt.date,
    lo: dt.datetime,
    hi: dt.datetime,
    max_age: float,
) -> list[str]:
    """Řádky výstupu pro jeden symbol (okno už oříznuté na teď)."""
    open_ts, _ = session_bounds(session)
    days = sorted({open_ts.date(), lo.date(), (hi - dt.timedelta(minutes=1)).date()})
    expected = open_minutes(lo, hi)
    derived = data / "derived" / symbol
    lines: list[str] = []

    levels = _rows(
        [derived / expiry / "levels" / f"{d}.parquet" for d in days],
        _schema(LEVELS_SCHEMA, ["ts_min"]),
        lo,
        hi,
    )
    have = {r["ts_min"] for r in levels}
    missing = [m for m in expected if m not in have]
    lines.append(f"{symbol} levels: {len(have)}/{len(expected)} minut, chybí {_fmt(missing)}")

    flow = _rows(
        [derived / "flow" / f"{d}.parquet" for d in days],
        _schema(FLOW_SCHEMA, ["ts_min", "flow_delta", "cum_delta", "source"]),
        lo,
        hi,
    )
    flow_min = {r["ts_min"] for r in flow}
    nulls = sorted(r["ts_min"] for r in flow if r["cum_delta"] is None)
    sources = Counter(r["source"] for r in flow)
    deltas = [r for r in flow if r["flow_delta"] is not None]
    jump = max(deltas, key=lambda r: abs(r["flow_delta"]), default=None)
    jump_text = "—" if jump is None else f"{jump['flow_delta']:,.0f} v {jump['ts_min']:%H:%M}"
    lines.append(
        f"{symbol} flow: {len(flow_min)}/{len(expected)} minut, NULL cum_delta {_fmt(nulls)}, "
        f"zdroj {dict(sources)}, max |flow_delta| {jump_text}, "
        f"chybí {_fmt([m for m in expected if m not in flow_min])}"
    )

    snaps = _rows(
        [data / "snapshots" / symbol / expiry / f"{d}.parquet" for d in days],
        _schema(SNAPSHOT_SCHEMA, ["ts_min", "strike", "right", "volume", "stale_age"]),
        open_ts,
        hi,
    )
    with_volume = {
        r["ts_min"] for r in snaps if r["ts_min"] >= lo and fresh_volume(r, max_age) is not None
    }
    lines.append(
        f"{symbol} snapshots {expiry}: minut s volume {len(with_volume)}/{len(expected)}, "
        f"chybí {_fmt([m for m in expected if m not in with_volume])}"
    )

    increments, declines = increments_by_minute(snaps, max_age)
    increments = {m: v for m, v in increments.items() if m >= lo}
    printvol: defaultdict[dt.datetime, float] = defaultdict(float)
    for row in _rows(
        [derived / expiry / "printvol" / f"{d}.parquet" for d in days],
        _schema(PRINTVOL_SCHEMA, ["ts_min", "volume_delta"]),
        lo,
        hi,
    ):
        printvol[row["ts_min"]] += row["volume_delta"] or 0.0
    differ = [
        (m.strftime("%H:%M"), printvol.get(m, 0.0), increments.get(m, 0.0))
        for m in sorted(set(printvol) | set(increments))
        if abs(printvol.get(m, 0.0) - increments.get(m, 0.0)) > 1e-6
    ]
    lines.append(
        f"{symbol} #1602: Σ printvol {sum(printvol.values()):,.0f} × Σ přírůstků od maxima "
        f"{sum(increments.values()):,.0f} (poklesů feedu v seanci {declines}); "
        f"minut s rozdílem {len(differ)} {differ[:8]}"
    )
    return lines


def main() -> int:
    # Přesměrovaný výstup má na Windows cp1250: diakritika by se rozsypala (roura v Git Bash)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Kontrola seance pod v6 (#1403, #1602)")
    parser.add_argument("--data", type=Path, default=Path("data"), help="kořen dat")
    parser.add_argument("--session", type=dt.date.fromisoformat, required=True, help="obchodní den")
    parser.add_argument("--expiry", default=None, help="YYYYMMDD, výchozí = den seance (0DTE)")
    parser.add_argument("--from", dest="start", type=_hm, default=None, help="HH:MM UTC")
    parser.add_argument("--to", dest="end", type=_hm, default=None, help="HH:MM UTC")
    parser.add_argument("--symbols", nargs="+", default=["ES", "NQ"])
    parser.add_argument(
        "--quote-max-age",
        type=float,
        default=Settings.model_fields["quote_max_age_s"].default,
        help="stáří kotace v s, nad kterým ji engine do trackeru nepustí (výchozí z konfigurace)",
    )
    args = parser.parse_args()
    try:
        lo, hi = window(args.session, args.start, args.end)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
    hi_now = min(hi, now)
    if hi_now <= lo:
        print(f"Okno začíná {lo:%d. %m. %H:%M} UTC, zatím není co kontrolovat", file=sys.stderr)
        return 2
    expiry = args.expiry or args.session.strftime("%Y%m%d")
    print(
        f"Seance {args.session}, expirace {expiry}, okno {lo:%d. %m. %H:%M}–{hi:%d. %m. %H:%M} UTC"
        f" (do {hi_now:%H:%M}, {len(open_minutes(lo, hi_now))} min otevřeného trhu)"
    )
    for symbol in args.symbols:
        print()
        lines = check_symbol(
            args.data, symbol, expiry, args.session, lo, hi_now, args.quote_max_age
        )
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
