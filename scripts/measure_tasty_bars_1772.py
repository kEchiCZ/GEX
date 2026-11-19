"""Měření minutových barů podkladu z tastytrade proti IBKR (#1772) — jen čtení.

Otázka: dá se při výpadku barů IBKR dát detektoru (a grafu) bar minuty M−1 už
v cyklu M (M:00 + `CYCLE_PHASE_S`) z druhého feedu, a jak přesně sedí na bar IBKR?
Sonda otevře vlastní DXLink spojení (nic nezapisuje, produkční engine nechává
být) a na front futures sbírá naráz:

* `Candle{=1m}` (živá subskripce s `fromTime`): poslední update svíčky M−1
  zachycený v M:00 + fáze („stav v čase cyklu“) a stav na konci měření („finál“);
* `TimeAndSale`: vlastní agregace OHLC podle času obchodu, zvlášť všechny tisky
  a jen `validTick` bez `spreadLeg` a s typem NEW.

Výstup je JSONL (jedna minuta × symbol × varianta). Srovnání s měřenými bary
IBKR (`derived/{sym}/bars`, source `ibkr`) dělá `--compare` nad týmž souborem.

Spuštění (host, proměnné prostředí GEXLENS_TASTY_CLIENT_SECRET a
GEXLENS_TASTY_REFRESH_TOKEN, nikdy ne výpis .env):
    python scripts/measure_tasty_bars_1772.py --minutes 30 --out bars.jsonl \
        --symbols /ESZ26:XCME,/NQZ26:XCME
    python scripts/measure_tasty_bars_1772.py --compare bars.jsonl --data data
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

import websockets  # noqa: E402

from gexlens_engine.instruments import CYCLE_PHASE_S  # noqa: E402
from gexlens_engine.tasty.dxlink import handshake, send_json  # noqa: E402
from gexlens_engine.tasty.session import TastyCredentials, TastySession  # noqa: E402

TRADE_FIELDS = [
    "eventSymbol",
    "time",
    "price",
    "size",
    "spreadLeg",
    "validTick",
    "type",
    "eventFlags",
]
CANDLE_FIELDS = ["eventSymbol", "eventFlags", "time", "open", "high", "low", "close", "volume"]
UTC = dt.UTC


def _minute(ms: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=UTC).replace(second=0, microsecond=0)


class _Bars:
    """OHLC per (symbol, minuta) — sdílené pro tisky i svíčky."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, dt.datetime], dict[str, float]] = {}

    def trade(self, symbol: str, minute: dt.datetime, price: float, size: float) -> None:
        row = self.rows.get((symbol, minute))
        if row is None:
            self.rows[(symbol, minute)] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": size,
                "n": 1,
            }
            return
        row["high"] = max(row["high"], price)
        row["low"] = min(row["low"], price)
        row["close"] = price
        row["volume"] += size
        row["n"] += 1


async def measure(args: argparse.Namespace) -> None:
    secret = os.environ.get("GEXLENS_TASTY_CLIENT_SECRET", "")
    refresh = os.environ.get("GEXLENS_TASTY_REFRESH_TOKEN", "")
    if not secret or not refresh:
        raise SystemExit("Chybí GEXLENS_TASTY_CLIENT_SECRET / GEXLENS_TASTY_REFRESH_TOKEN")
    symbols = args.symbols.split(",")
    session = TastySession(TastyCredentials(client_secret=secret, refresh_token=refresh))
    url, token = await session.quote_token()
    started = dt.datetime.now(UTC)
    deadline = time.monotonic() + args.minutes * 60
    # Tisky: všechny a jen validní (validTick, NEW, ne spreadLeg)
    all_trades, valid_trades = _Bars(), _Bars()
    candles: dict[tuple[str, dt.datetime], dict[str, float]] = {}
    at_cycle: dict[tuple[str, dt.datetime], dict[str, float] | None] = {}
    flags_seen: dict[str, int] = {}
    trades_seen = {"total": 0, "spread_leg": 0, "invalid": 0, "not_new": 0}
    late: dict[str, int] = {"trades_after_cycle": 0}
    connected_at = dt.datetime.now(UTC)

    def dump() -> None:
        """Celý stav do souboru (temp + rename) — přežije i zabití procesu."""
        meta = {
            "kind": "meta",
            "started": started.isoformat(),
            "connected_at": connected_at.isoformat(),
            "trades": trades_seen,
            "late": late,
            "candle_flags": flags_seen,
        }
        temp = f"{args.out}.tmp"
        with open(temp, "w", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(meta) + "\n")
            for label, bars in (
                ("trades_all", all_trades.rows),
                ("trades_valid", valid_trades.rows),
            ):
                for (sym, minute), row in sorted(bars.items()):
                    record = {"kind": label, "sym": sym, "ts": minute.isoformat(), **row}
                    out.write(json.dumps(record) + "\n")
            for (sym, minute), row in sorted(candles.items()):
                record = {"kind": "candle_final", "sym": sym, "ts": minute.isoformat(), **row}
                out.write(json.dumps(record) + "\n")
            for (sym, minute), cycle_row in sorted(at_cycle.items()):
                record = {"kind": "candle_at_cycle", "sym": sym, "ts": minute.isoformat()}
                if cycle_row is not None:
                    record.update(cycle_row)
                out.write(json.dumps(record) + "\n")
        os.replace(temp, args.out)

    async with websockets.connect(url, max_size=2**24) as ws:
        await handshake(ws, token, {"TimeAndSale": TRADE_FIELDS, "Candle": CANDLE_FIELDS})
        since_ms = int((started - dt.timedelta(minutes=3)).timestamp() * 1000)
        add: list[dict[str, object]] = [{"type": "TimeAndSale", "symbol": s} for s in symbols]
        add += [{"type": "Candle", "symbol": f"{s}{{=1m}}", "fromTime": since_ms} for s in symbols]
        await send_json(ws, {"type": "FEED_SUBSCRIPTION", "channel": 1, "add": add})
        last_keepalive = time.monotonic()
        next_cycle = (connected_at.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)) + (
            dt.timedelta(seconds=CYCLE_PHASE_S)
        )
        while time.monotonic() < deadline:
            now = dt.datetime.now(UTC)
            if now >= next_cycle:
                # Stav svíčky M−1 v okamžiku, kdy by ji četl cyklus M
                prev = next_cycle.replace(second=0, microsecond=0) - dt.timedelta(minutes=1)
                for s in symbols:
                    row = candles.get((s, prev))
                    at_cycle[(s, prev)] = dict(row) if row is not None else None
                next_cycle += dt.timedelta(minutes=1)
                dump()
            if time.monotonic() - last_keepalive > 30:
                await send_json(ws, {"type": "KEEPALIVE", "channel": 0})
                last_keepalive = time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            message = json.loads(raw)
            if message.get("type") != "FEED_DATA":
                if message.get("type") == "ERROR":
                    print("ERROR", message, file=sys.stderr)
                continue
            data = message.get("data", [])
            for index in range(0, len(data) - 1, 2):
                kind, values = data[index], data[index + 1]
                if kind == "TimeAndSale":
                    width = len(TRADE_FIELDS)
                    for off in range(0, len(values) - width + 1, width):
                        sym, ts, price, size, leg, valid, typ, flags = values[off : off + width]
                        try:
                            p, q, t = float(price), float(size), float(ts)
                        except (TypeError, ValueError):
                            continue
                        if p != p or q != q or t <= 0:
                            continue
                        minute = _minute(t)
                        trades_seen["total"] += 1
                        if (
                            minute
                            < dt.datetime.now(UTC).replace(second=0, microsecond=0)
                            - dt.timedelta(minutes=1)
                        ) or (
                            minute
                            == dt.datetime.now(UTC).replace(second=0, microsecond=0)
                            - dt.timedelta(minutes=1)
                            and dt.datetime.now(UTC).second >= CYCLE_PHASE_S
                        ):
                            late["trades_after_cycle"] += 1
                        all_trades.trade(str(sym), minute, p, q)
                        bad = False
                        if leg is True or str(leg).lower() == "true":
                            trades_seen["spread_leg"] += 1
                            bad = True
                        if valid is False or str(valid).lower() == "false":
                            trades_seen["invalid"] += 1
                            bad = True
                        if typ not in (None, "NEW"):
                            trades_seen["not_new"] += 1
                            bad = True
                        if not bad:
                            valid_trades.trade(str(sym), minute, p, q)
                elif kind == "Candle":
                    width = len(CANDLE_FIELDS)
                    for off in range(0, len(values) - width + 1, width):
                        sym, flags, ts, o, h, low, c, v = values[off : off + width]
                        flags_seen[str(flags)] = flags_seen.get(str(flags), 0) + 1
                        try:
                            row = {
                                "open": float(o),
                                "high": float(h),
                                "low": float(low),
                                "close": float(c),
                                "volume": float(v) if v is not None else 0.0,
                            }
                            minute = _minute(float(ts))
                        except (TypeError, ValueError):
                            continue
                        if any(x != x for x in row.values()):
                            continue
                        base = str(sym).split("{")[0]
                        candles[(base, minute)] = row
    await session.close()
    dump()
    print(f"Zapsáno {args.out}: tisky {trades_seen}, pozdní {late}, příznaky {flags_seen}")


def compare(args: argparse.Namespace) -> None:
    import pyarrow.parquet as pq

    with open(args.compare, encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    meta = rows[0]
    started = dt.datetime.fromisoformat(meta["connected_at"])
    # Úplné minuty: začínají po připojení sondy
    first_full = started.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    by_kind: dict[str, dict[tuple[str, str], dict[str, float]]] = {}
    for row in rows[1:]:
        by_kind.setdefault(row["kind"], {})[(row["sym"], row["ts"])] = row
    ibkr: dict[tuple[str, str], dict[str, float]] = {}
    for sym_root, streamer in (("ES", "/ESZ26:XCME"), ("NQ", "/NQZ26:XCME")):
        for path in sorted(Path(args.data, "derived", sym_root, "bars").glob("*.parquet"))[-3:]:
            for bar in pq.read_table(path).to_pylist():
                ts = bar["ts_min"]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts < first_full or bar.get("source") not in (None, "ibkr"):
                    continue
                ibkr[(streamer, ts.isoformat())] = bar
    tick = {"/ESZ26:XCME": 0.25, "/NQZ26:XCME": 0.25}
    print(f"Měření od {started.isoformat()}, tisky {meta['trades']}, pozdní {meta['late']}")
    print(f"Příznaky svíček: {meta['candle_flags']}")
    for kind in ("candle_at_cycle", "candle_final", "trades_valid", "trades_all"):
        bars = by_kind.get(kind, {})
        for streamer in ("/ESZ26:XCME", "/NQZ26:XCME"):
            keys = sorted(
                key
                for key in ibkr
                if key[0] == streamer and dt.datetime.fromisoformat(key[1]) >= first_full
            )
            # Poslední minuta měření může být neúplná
            keys = keys[:-1] if keys else keys
            missing = [key for key in keys if key not in bars or "close" not in bars[key]]
            stats = {"open": 0, "high": 0, "low": 0, "close": 0}
            worst = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}
            n = 0
            for key in keys:
                bar = bars.get(key)
                if bar is None or "close" not in bar:
                    continue
                n += 1
                for field in stats:
                    diff = abs(float(bar[field]) - float(ibkr[key][field]))
                    if diff < 1e-9:
                        stats[field] += 1
                    worst[field] = max(worst[field], diff / tick[streamer])
            print(
                f"{kind:16s} {streamer}: minut IBKR {len(keys)}, bez baru {len(missing)}, "
                f"shoda {n and {f: f'{stats[f]}/{n}' for f in stats}}, "
                f"max rozdíl v ticích {worst}"
            )
    # Svíčka v čase cyklu × finál: je M−1 v M:10 už konečná?
    cycle = by_kind.get("candle_at_cycle", {})
    final = by_kind.get("candle_final", {})
    changed = [
        key
        for key, row in cycle.items()
        if "close" in row
        and key in final
        and any(abs(row[f] - final[key][f]) > 1e-9 for f in ("open", "high", "low", "close"))
    ]
    print(
        f"Svíčka M-1 v M:{CYCLE_PHASE_S:.0f} s jiná než finál: "
        f"{len(changed)} z {len(cycle)} {changed[:5]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--out", default="tasty_bars_1772.jsonl")
    parser.add_argument("--symbols", default="/ESZ26:XCME,/NQZ26:XCME")
    parser.add_argument("--compare", help="JSONL z měření → srovnání s bary IBKR")
    parser.add_argument("--data", default="data")
    args = parser.parse_args()
    if args.compare:
        compare(args)
    else:
        asyncio.run(measure(args))


if __name__ == "__main__":
    main()
