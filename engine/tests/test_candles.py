"""Backfill svíček (#617): detekce děr, parsování a záruka nepřepisování."""

import asyncio
import datetime as dt
import json
import logging
import time

import pytest

from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_LIVE,
    BAR_SOURCE_RECONSTRUCTED,
    SnapshotWriter,
)
from gexlens_engine.tasty.candles import (
    CANDLE_FIELDS,
    CandleBar,
    CandleFetcher,
    CandleRange,
    _chunks,
    _Progress,
    _row_to_bar,
    missing_minutes,
)

M = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.UTC)


def minutes(*offsets: int) -> set[dt.datetime]:
    return {M + dt.timedelta(minutes=o) for o in offsets}


# ── Detekce chybějících minut ──────────────────────────────────────


def test_dira_uprostred_se_najde() -> None:
    have = minutes(0, 1, 4)
    chybi = missing_minutes(have, M, M + dt.timedelta(minutes=5))
    assert chybi == [M + dt.timedelta(minutes=2), M + dt.timedelta(minutes=3)]


def test_uplny_usek_nema_co_doplnit() -> None:
    have = minutes(0, 1, 2)
    assert missing_minutes(have, M, M + dt.timedelta(minutes=3)) == []


def test_okno_je_polootevrene() -> None:
    """`until` je rozdělaná minuta — tu se doplňovat nesmí."""
    chybi = missing_minutes(set(), M, M + dt.timedelta(minutes=2))
    assert chybi == [M, M + dt.timedelta(minutes=1)]
    assert M + dt.timedelta(minutes=2) not in chybi


def test_prazdny_den_chce_cele_okno() -> None:
    assert len(missing_minutes(set(), M, M + dt.timedelta(minutes=60))) == 60


# ── Parsování COMPACT řádků ────────────────────────────────────────


def _row(ts: dt.datetime, close: float = 7600.0, volume: float = 12.0) -> list[object]:
    ms = ts.timestamp() * 1000
    return ["/ESU26:XCME{=1m}", ms, 7599.0, 7601.0, 7598.0, close, volume]


def test_radek_se_prevede_na_bar_se_znackou_rekonstrukce() -> None:
    bar = _row_to_bar(_row(M))
    assert bar is not None
    assert bar.ts == M
    assert bar.close == 7600.0
    # Bez tohohle by doplněná minuta v UI splynula s měřenou (#617)
    assert bar.source == BAR_SOURCE_RECONSTRUCTED


def test_neuplna_nebo_vadna_svicka_se_zahodi() -> None:
    assert _row_to_bar(["/ESU26:XCME", 1.0]) is None  # krátký řádek
    assert _row_to_bar(["/ESU26:XCME", "x", 1.0, 1.0, 1.0, 1.0, 1.0]) is None  # nečíselný čas
    assert _row_to_bar(_row(M, close=float("nan"))) is None  # NaN cena


def test_sekundy_se_zarovnaji_na_minutu() -> None:
    bar = _row_to_bar(_row(M + dt.timedelta(seconds=37)))
    assert bar is not None and bar.ts == M


def test_chunks_rozseka_davku_na_zaznamy() -> None:
    data: list[object] = ["Candle", _row(M) + _row(M + dt.timedelta(minutes=1))]
    rows = _chunks(data)
    assert len(rows) == 2
    assert all(len(r) == len(CANDLE_FIELDS) for r in rows)


# ── Zápis: rekonstrukce nesmí přepsat měřená data ──────────────────


class ZivyBar:
    """Bar z živé cesty — nemá `source`, zapisovač doplní `ibkr`."""

    def __init__(self, ts: dt.datetime, close: float) -> None:
        self.ts = ts
        self.open = self.high = self.low = close
        self.close = close
        self.volume = 1.0


def test_zapis_odlisi_puvod_a_backfill_neprepise_merena_data(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Jádro #617: doplňují se JEN chybějící minuty, měřené zůstanou."""
    from gexlens_engine.config import Settings

    store = SnapshotWriter(Settings(data_dir=tmp_path))
    den = M.date()

    # Živě naměřeno: minuty 0 a 3
    store.write_bars("ES", den, [ZivyBar(M, 100.0), ZivyBar(M + dt.timedelta(minutes=3), 103.0)])

    # Backfill smí doplnit jen to, co chybí
    have = {M, M + dt.timedelta(minutes=3)}
    chybi = missing_minutes(have, M, M + dt.timedelta(minutes=4))
    assert chybi == [M + dt.timedelta(minutes=1), M + dt.timedelta(minutes=2)]
    store.write_bars(
        "ES",
        den,
        [CandleBar(ts=t, open=1.0, high=1.0, low=1.0, close=999.0, volume=5.0) for t in chybi],
    )

    import pyarrow.parquet as pq

    rows = pq.read_table(
        tmp_path / "derived" / "ES" / "bars" / f"{den.isoformat()}.parquet"
    ).to_pylist()
    podle_minuty = {r["ts_min"]: r for r in rows}

    assert len(rows) == 4
    # Měřené minuty si drží svou hodnotu i původ
    assert podle_minuty[M]["close"] == 100.0
    assert podle_minuty[M]["source"] == BAR_SOURCE_LIVE
    assert podle_minuty[M + dt.timedelta(minutes=3)]["close"] == 103.0
    assert podle_minuty[M + dt.timedelta(minutes=3)]["source"] == BAR_SOURCE_LIVE
    # Doplněné jsou označené jako rekonstrukce
    for offset in (1, 2):
        row = podle_minuty[M + dt.timedelta(minutes=offset)]
        assert row["close"] == 999.0
        assert row["source"] == BAR_SOURCE_RECONSTRUCTED


# ── Sběr s živou subskripcí (#1253) ───────────────────────────────


def _feed(rows: list[list[object]]) -> str:
    return json.dumps({"type": "FEED_DATA", "channel": 1, "data": ["Candle", sum(rows, [])]})


class _LiveWs:
    """Dvojník DXLink: dávka historie a pak nekonečné updaty rozdělané minuty."""

    def __init__(self, history: list[list[object]], forming: list[object]) -> None:
        self._queue = [_feed(history)]
        self._forming = forming
        self.sent = 0

    async def send(self, payload: str) -> None:
        pass

    async def recv(self) -> str:
        if self._queue:
            return self._queue.pop(0)
        await asyncio.sleep(0.02)  # živý update každých 20 ms, ticho nikdy
        self.sent += 1
        return _feed([self._forming])


async def test_sber_skonci_i_kdyz_zive_updaty_rozdelane_minuty_nikdy_neutichnou() -> None:
    """#1253: SPY v RTH — server po historii posílá updaty tvořící se minuty
    každou chvíli; ticho se měří nad NOVÝMI minutami, jinak sběr vyčerpal strop."""
    now = M + dt.timedelta(minutes=3, seconds=23)
    history = [_row(M + dt.timedelta(minutes=i)) for i in range(3)]
    forming = _row(M + dt.timedelta(minutes=3), close=7777.0)
    ws = _LiveWs(history, forming)
    fetcher = CandleFetcher(_no_token, quiet_timeout_s=0.1, total_timeout_s=5.0)
    progress = _Progress()
    started = time.monotonic()
    await fetcher._collect(ws, CandleRange("SPY", since=M, until=now), progress)
    assert time.monotonic() - started < 1.0
    assert [bar.ts for bar in progress.bars()] == [M + dt.timedelta(minutes=i) for i in range(3)]
    assert ws.sent > 0  # updaty opravdu chodily a sběr je přežil


async def test_rozdelana_minuta_se_nesbira_ani_nehlasi_jako_dira() -> None:
    now = M + dt.timedelta(minutes=2, seconds=23)
    assert missing_minutes(set(), M, now) == [M, M + dt.timedelta(minutes=1)]
    ws = _LiveWs([_row(M), _row(M + dt.timedelta(minutes=2))], _row(M + dt.timedelta(minutes=2)))
    progress = _Progress()
    fetcher = CandleFetcher(_no_token, quiet_timeout_s=0.1, first_data_timeout_s=0.5)
    await fetcher._collect(ws, CandleRange("SPY", since=M, until=now), progress)
    assert [bar.ts for bar in progress.bars()] == [M]


async def test_pri_stropu_se_vrati_dotazene_minuty_a_log_nese_fazi(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Hanging(CandleFetcher):
        async def _fetch(self, request: CandleRange, progress: _Progress) -> None:
            progress.phase = "collect"
            progress.by_minute[M] = CandleBar(M, 1.0, 1.0, 1.0, 1.0, 1.0)
            await asyncio.sleep(10)

    fetcher = _Hanging(_no_token, total_timeout_s=0.1)
    with caplog.at_level(logging.WARNING, logger="gexlens_engine.tasty.candles"):
        bars = await fetcher.fetch(CandleRange("SPY", since=M, until=M + dt.timedelta(minutes=5)))
    assert [bar.ts for bar in bars] == [M]
    assert "ve fázi collect" in caplog.text and "zapíše se 1" in caplog.text


async def _no_token() -> tuple[str, str]:
    return ("wss://x", "t")
