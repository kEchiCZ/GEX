"""Oprava rozložení barů do partic (#1002) — čistá plánovací logika.

Pravidlo: bar patří do partice UTC dne svého `ts_min` (`bar_partition_day`).
Do 3. 9. 2026 ho engine porušoval dvěma cestami: půlnoční cyklus zapsal
finální bar 23:59 pod dnem cyklu (D+1) vedle provizorní kopie v D, a
rekonstrukce #617 zapsala blok 22:00–23:59 dne D−1 pod datem seance (D)
vedle měřených barů v D−1. Tady se z existujících partic spočítá, co kam
patří a který duplikát vyhrává; zápis dělá `scripts/fix_bar_partitions.py`.

Kdo vyhrává, když tutéž minutu nese víc řádků:
1. měřený bar (`source` NULL/`ibkr`) před rekonstruovaným (`tasty_candle`) —
   doplněná minuta není totéž co změřená (#617),
2. větší objem — finální bar má ≥ objem než provizorní (ADR-0005),
3. řádek z partice, kam minuta patří.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from gexlens_engine.storage.parquet_store import BAR_SOURCE_RECONSTRUCTED, bar_partition_day

BarRow = Mapping[str, object]


@dataclass(frozen=True)
class RepartitionPlan:
    """Výsledek plánování: nový obsah změněných partic + co se s duplikáty stalo."""

    rows_by_day: dict[dt.date, list[dict[str, object]]] = field(default_factory=dict)
    moved: int = 0  # řádek z cizí partice vyhrál a přesunul se do své
    dropped: int = 0  # řádek z cizí partice prohrál a zmizel
    replaced: int = 0  # řádek ve své partici prohrál s přesunutým

    @property
    def changed_days(self) -> list[dt.date]:
        return sorted(self.rows_by_day)


def _ts(row: BarRow) -> dt.datetime:
    ts = row["ts_min"]
    assert isinstance(ts, dt.datetime)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)


def bar_rank(row: BarRow, file_day: dt.date) -> tuple[int, float, int]:
    """Řadicí klíč duplikátů — vyšší vyhrává (viz docstring modulu)."""
    measured = 0 if row.get("source") == BAR_SOURCE_RECONSTRUCTED else 1
    volume = float(row.get("volume") or 0.0)  # type: ignore[arg-type]
    at_home = 1 if bar_partition_day(_ts(row)) == file_day else 0
    return (measured, volume, at_home)


def plan_repartition(rows_by_file: Mapping[dt.date, Sequence[BarRow]]) -> RepartitionPlan:
    """Z obsahu partic (den souboru → řádky) spočítá opravené partice.

    Vrací jen dny, jejichž obsah se mění; ostatní soubory zůstávají netknuté.
    Řádky nemění — jen je přesouvá nebo zahazuje, žádná hodnota se nepřepočítává.
    """
    # kandidáti per cílový den a minuta: (řádek, den souboru)
    candidates: dict[dt.date, dict[dt.datetime, list[tuple[BarRow, dt.date]]]] = {}
    touched: set[dt.date] = set()
    for file_day, rows in rows_by_file.items():
        for row in rows:
            target = bar_partition_day(_ts(row))
            candidates.setdefault(target, {}).setdefault(_ts(row), []).append((row, file_day))
            if target != file_day:
                touched.add(file_day)
                touched.add(target)

    moved = dropped = replaced = 0
    result: dict[dt.date, list[dict[str, object]]] = {}
    for day in sorted(touched):
        kept: list[dict[str, object]] = []
        for ts in sorted(candidates.get(day, {})):
            options = candidates[day][ts]
            winner_row, winner_file = max(options, key=lambda item: bar_rank(item[0], item[1]))
            if winner_file != day:
                moved += 1
            for row, file_day in options:
                if row is winner_row:
                    continue
                if file_day == day:
                    replaced += 1
                else:
                    dropped += 1
            kept.append(dict(winner_row))
        result[day] = kept
    return RepartitionPlan(rows_by_day=result, moved=moved, dropped=dropped, replaced=replaced)
