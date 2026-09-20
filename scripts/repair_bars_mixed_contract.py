"""Oprava partic barů kontaminovaných jiným kontraktem (#1232, roll týden září 2026).

Partice `derived/{sym}/bars/{den}.parquet` 8.–18. 9. 2026 mají vedle měřených
barů (NQU6, `source=ibkr`) doplněné řádky `ibkr_hist` z NQZ6 (+230–460 b).
Skript pro každý den:

1. nechá měřené minuty (živá cesta / NULL zdroj) beze změny,
2. z dxFeed Candle stáhne 1min svíčky KANDIDÁTNÍCH kontraktů a vybere ten,
   který sedí na měřené minuty (medián odchylky < tolerance) — kalendář front
   kontraktu se s #1189 měnil, rozhodují data,
3. všechny doplněné řádky (`ibkr_hist`, `tasty_candle`) nahradí svíčkami
   vybraného kontraktu a doplní i minuty, které chybí úplně,
4. před zápisem zálohuje původní partici do `--backup-dir`.

Bez `--apply` jen vypíše, co by udělal. Spouští se v kontejneru enginu
(tasty přihlášení z prostředí, data pod /app/data):

    docker exec -i gex-engine-1 python - --symbol NQ --from 2026-09-08 \
        --to 2026-09-18 --contracts /NQU26:XCME,/NQZ26:XCME [--apply] \
        < scripts/repair_bars_mixed_contract.py

Engine musí po nasazení stráže (#1232 část 1) běžet z čerstvého startu — jeho
zapisovač drží partice v paměti jen pro aktuální den, starší nepřepíše.
"""

import argparse
import asyncio
import datetime as dt
import logging
import shutil
import statistics
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import BACKFILL_CONTRACT_TOLERANCE, contract_mismatch
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_RECONSTRUCTED,
    bar_source_rank,
)
from gexlens_engine.tasty.candles import CandleBar, CandleFetcher, CandleRange
from gexlens_engine.tasty.session import TastyCredentials, TastySession

logger = logging.getLogger("repair_bars")


def _partition(settings: Settings, symbol: str, day: dt.date) -> Path:
    return settings.derived_dir / symbol / "bars" / f"{day.isoformat()}.parquet"


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=dt.UTC) if ts.tzinfo is None else ts.astimezone(dt.UTC)


def _deviates(
    measured: dict[dt.datetime, float], ts: dt.datetime, close: float, window: int = 3
) -> bool:
    """Liší se doplněný bar od nejbližší měřené minuty (±window) víc než tolerance?"""
    for offset in range(window + 1):
        for sign in (1, -1) if offset else (1,):
            ref = measured.get(ts + dt.timedelta(minutes=offset * sign))
            if ref:
                return abs(close - ref) / ref > BACKFILL_CONTRACT_TOLERANCE
    return False


def _write(
    path: Path,
    schema: pa.Schema,
    rows: list[dict[str, object]],
    backup_dir: Path,
    symbol: str,
    day: dt.date,
) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{symbol}-{day.isoformat()}.parquet"
    if not backup.exists():
        shutil.copy2(path, backup)
    rows.sort(key=lambda r: r["ts_min"])  # type: ignore[arg-type,return-value]
    new_table = pa.Table.from_pylist(rows, schema=schema)
    tmp = path.with_suffix(".tmp.parquet")
    pq.write_table(new_table, tmp)
    tmp.replace(path)
    print(f"{day}: zapsáno {new_table.num_rows} řádků (záloha {backup})")


async def _candles(
    fetcher: CandleFetcher, contract: str, day: dt.date
) -> dict[dt.datetime, CandleBar]:
    since = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
    until = since + dt.timedelta(days=1)
    bars = await fetcher.fetch(CandleRange(streamer_symbol=contract, since=since, until=until))
    return {_aware(bar.ts).replace(second=0, microsecond=0): bar for bar in bars}


async def repair_day(
    settings: Settings,
    fetcher: CandleFetcher,
    symbol: str,
    day: dt.date,
    contracts: list[str],
    *,
    apply: bool,
    backup_dir: Path,
) -> None:
    path = _partition(settings, symbol, day)
    if not path.exists():
        print(f"{day}: partice neexistuje — přeskočeno")
        return
    table = pq.read_table(path)
    rows = table.to_pylist()
    measured: dict[dt.datetime, dict[str, object]] = {}
    filled: dict[str, int] = {}
    for row in rows:
        ts = row.get("ts_min")
        if ts is None:
            continue
        ts = _aware(ts)
        row["ts_min"] = ts
        if bar_source_rank(row.get("source")) >= bar_source_rank(None):
            measured[ts] = row
        else:
            filled[str(row.get("source"))] = filled.get(str(row.get("source")), 0) + 1
    measured_closes = {ts: float(row["close"]) for ts, row in measured.items() if row.get("close")}

    chosen: str | None = None
    chosen_bars: dict[dt.datetime, CandleBar] = {}
    verdicts: list[str] = []
    for contract in contracts:
        candles = await _candles(fetcher, contract, day)
        if not candles:
            verdicts.append(f"{contract}: bez svíček")
            continue
        deviation = contract_mismatch(measured_closes, list(candles.values()))
        verdicts.append(
            f"{contract}: {len(candles)} svíček, odchylka "
            f"{'—' if deviation is None else f'{deviation * 100:.2f} %'}"
        )
        if deviation is not None and deviation <= BACKFILL_CONTRACT_TOLERANCE and chosen is None:
            chosen, chosen_bars = contract, candles

    if not measured:
        print(
            f"{day}: žádné měřené minuty, {sum(filled.values())} doplněných — nelze rozhodnout; "
            + "; ".join(verdicts)
        )
        return
    if chosen is None:
        # Bez svíček (dxFeed historie expirovaného kontraktu sahá ~10 dní zpět):
        # cizí doplněné řádky se aspoň smažou — díra je lepší než skok o 400 b
        foreign_rows = [
            row
            for row in rows
            if row.get("ts_min") is not None
            and _aware(row["ts_min"]) not in measured
            and _deviates(measured_closes, _aware(row["ts_min"]), float(row["close"]))
        ]
        print(
            f"{day}: měřených {len(measured)}, doplněných {filled} — žádný kandidát nesedí; "
            f"smazat {len(foreign_rows)} cizích řádků (odchylka od měřených > tolerance); "
            + "; ".join(verdicts)
        )
        if apply and foreign_rows:
            drop = {_aware(r["ts_min"]) for r in foreign_rows}
            kept = [r for r in rows if r.get("ts_min") is None or _aware(r["ts_min"]) not in drop]
            _write(path, table.schema, kept, backup_dir, symbol, day)
        return

    replacement = {ts: bar for ts, bar in chosen_bars.items() if ts not in measured}
    foreign = [
        row
        for row in rows
        if row.get("ts_min") is not None and _aware(row["ts_min"]) not in measured
    ]
    foreign_dev = [
        abs(float(r["close"]) - chosen_bars[_aware(r["ts_min"])].close)
        for r in foreign
        if _aware(r["ts_min"]) in chosen_bars and r.get("close")
    ]
    print(
        f"{day}: měřených {len(measured)}, doplněných {filled} → nahradit {len(replacement)} "
        f"svíčkami {chosen} (medián |Δ| doplněné vs. svíčka "
        f"{statistics.median(foreign_dev):.0f} b, n={len(foreign_dev)}); " + "; ".join(verdicts)
        if foreign_dev
        else f"{day}: měřených {len(measured)}, doplněných {filled} → nahradit "
        f"{len(replacement)} svíčkami {chosen}; " + "; ".join(verdicts)
    )
    if not apply:
        return

    new_rows: list[dict[str, object]] = list(measured.values())
    for ts, bar in replacement.items():
        new_rows.append(
            {
                "ts_min": ts,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "source": BAR_SOURCE_RECONSTRUCTED,
            }
        )
    _write(path, table.schema, new_rows, backup_dir, symbol, day)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from", dest="since", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--to", dest="until", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--contracts", required=True, help="dxFeed symboly kandidátů, čárkou")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    if not settings.tasty_client_secret or not settings.tasty_refresh_token:
        raise SystemExit("chybí tasty přihlášení v prostředí")
    session = TastySession(
        TastyCredentials(
            client_secret=settings.tasty_client_secret,
            refresh_token=settings.tasty_refresh_token,
        )
    )
    fetcher = CandleFetcher(session.quote_token)
    backup_dir = args.backup_dir or (settings.data_dir / "backup" / "bars-1232")
    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]
    day = args.since
    while day <= args.until:
        await repair_day(
            settings, fetcher, args.symbol, day, contracts, apply=args.apply, backup_dir=backup_dir
        )
        day += dt.timedelta(days=1)


if __name__ == "__main__":
    asyncio.run(main())
