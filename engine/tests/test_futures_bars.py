"""Bary podkladu z tisků tasty — záložní bar minuty za výpadku IBKR (#1772)."""

import datetime as dt

from gexlens_engine.compute.futures_bars import (
    BarAgreement,
    FuturesBarAggregator,
    FuturesMinuteBar,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import BAR_SOURCE_TASTY_TRADES, bar_source_rank

ES = "/ESZ26:XCME"
MINUTE = dt.datetime(2026, 11, 23, 16, 5, tzinfo=dt.UTC)
SUBSCRIBED = MINUTE - dt.timedelta(minutes=10)


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _print(
    agg: FuturesBarAggregator,
    at: dt.datetime,
    price: float,
    size: float = 1.0,
    *,
    streamer: str = ES,
    spread_leg: object = False,
) -> None:
    ms = at.timestamp() * 1000.0
    agg.on_event("TimeAndSale", [streamer, ms, price, size, "BUY", spread_leg, False])


def _aggregator(first_print: dt.datetime = SUBSCRIBED) -> tuple[FuturesBarAggregator, Clock]:
    clock = Clock(first_print)
    agg = FuturesBarAggregator(clock)
    agg.register("ES", ES)
    return agg, clock


def _full_minute(agg: FuturesBarAggregator, clock: Clock) -> None:
    """Tisk před minutou, čtyři v minutě, jeden po ní (tok doložený za konec)."""
    _print(agg, MINUTE - dt.timedelta(seconds=5), 7270.0)
    clock.now = MINUTE + dt.timedelta(seconds=1)
    for second, price, size in (
        (0.2, 7272.75, 3),
        (15, 7278.75, 2),
        (40, 7270.5, 4),
        (59.9, 7276.25, 1),
    ):
        _print(agg, MINUTE + dt.timedelta(seconds=second), price, size)
    _print(agg, MINUTE + dt.timedelta(minutes=1, seconds=0.5), 7276.5)


def test_bar_minuty_z_tisku_podle_casu_tisku() -> None:
    agg, clock = _aggregator()
    _full_minute(agg, clock)

    got = agg.take("ES", MINUTE, SUBSCRIBED)

    assert got is not None
    assert got.streamer == ES
    assert got.bar == Bar(
        ts=MINUTE,
        open=7272.75,
        high=7278.75,
        low=7270.5,
        close=7276.25,
        volume=10.0,
        source=BAR_SOURCE_TASTY_TRADES,
    )
    # Tisk minuty M (16:06) zůstává pro další cyklus; pozdní tisk do uzavřené minuty ne
    _print(agg, MINUTE + dt.timedelta(seconds=30), 7000.0)
    assert agg.late_prints == 1
    _print(agg, MINUTE + dt.timedelta(minutes=2, seconds=1), 7277.0)
    nxt = agg.take("ES", MINUTE + dt.timedelta(minutes=1), SUBSCRIBED)
    assert nxt is not None
    assert (nxt.bar.open, nxt.bar.close, nxt.bar.volume) == (7276.5, 7276.5, 1.0)


def test_spojeni_az_behem_minuty_je_mezera() -> None:
    """23. 11.: připojení v 16:13:27 dalo poloviční objem a open o 24 tiků jinde."""
    agg, clock = _aggregator()
    _full_minute(agg, clock)
    assert agg.take("ES", MINUTE, MINUTE + dt.timedelta(seconds=27)) is None


def test_bez_subskripce_je_mezera() -> None:
    agg, clock = _aggregator()
    _full_minute(agg, clock)
    assert agg.take("ES", MINUTE, None) is None


def test_prvni_tisk_streameru_az_v_minute_je_mezera() -> None:
    """Symbol přidaný do běžící subskripce (roll): od kdy tiskne, nevíme přesněji."""
    agg, clock = _aggregator(first_print=MINUTE + dt.timedelta(seconds=20))
    clock.now = MINUTE + dt.timedelta(seconds=20)
    _print(agg, MINUTE + dt.timedelta(seconds=20), 7272.0)
    _print(agg, MINUTE + dt.timedelta(minutes=1, seconds=1), 7273.0)
    assert agg.take("ES", MINUTE, SUBSCRIBED) is None


def test_tok_nedolozeny_za_konec_minuty_je_mezera() -> None:
    """Stream utichl uprostřed minuty bez reconnectu — bar by byl neúplný."""
    agg, clock = _aggregator()
    _print(agg, MINUTE - dt.timedelta(seconds=5), 7270.0)
    _print(agg, MINUTE + dt.timedelta(seconds=10), 7272.0)
    assert agg.take("ES", MINUTE, SUBSCRIBED) is None


def test_minuta_bez_tisku_je_mezera_a_neregistrovany_symbol_nic() -> None:
    agg, clock = _aggregator()
    _print(agg, MINUTE - dt.timedelta(seconds=5), 7270.0)
    _print(agg, MINUTE + dt.timedelta(minutes=1, seconds=1), 7271.0)
    assert agg.take("ES", MINUTE, SUBSCRIBED) is None
    assert agg.take("NQ", MINUTE, SUBSCRIBED) is None


def test_noha_spreadu_a_opcni_tisk_se_nepocitaji() -> None:
    agg, clock = _aggregator()
    _full_minute(agg, clock)
    _print(agg, MINUTE + dt.timedelta(seconds=30), 15.0, spread_leg=True)
    _print(agg, MINUTE + dt.timedelta(seconds=30), 1.0, streamer="./ESZ26C7300:XCME")
    got = agg.take("ES", MINUTE, SUBSCRIBED)
    assert got is not None
    assert got.bar.low == 7270.5
    assert agg.spread_legs == 1


def test_roll_streameru_zahodi_stav_stareho() -> None:
    agg, clock = _aggregator()
    _full_minute(agg, clock)
    agg.register("ES", "/ESH27:XCME")
    # Nový streamer zatím netiskl — minuta nejde doložit, starý tok se nepoužije
    assert agg.take("ES", MINUTE, SUBSCRIBED) is None


def _pair(
    close_ibkr: float, close_tasty: float, minute: int, streamer: str = ES
) -> tuple[Bar, FuturesMinuteBar]:
    ts = MINUTE + dt.timedelta(minutes=minute)
    measured = Bar(
        ts=ts, open=close_ibkr, high=close_ibkr, low=close_ibkr, close=close_ibkr, volume=10.0
    )
    tasty = Bar(
        ts=ts,
        open=close_tasty,
        high=close_tasty,
        low=close_tasty,
        close=close_tasty,
        volume=10.0,
        source=BAR_SOURCE_TASTY_TRADES,
    )
    return measured, FuturesMinuteBar(bar=tasty, streamer=streamer)


def test_straz_kontraktu_a_hodinova_shoda() -> None:
    agreement = BarAgreement()
    # Málo párů = nelze rozhodnout
    agreement.add(*_pair(7270.0, 7270.0, 0))
    assert agreement.contract_deviation(ES) is None
    for minute in range(1, 6):
        agreement.add(*_pair(7270.0, 7270.25, minute))
    deviation = agreement.contract_deviation(ES)
    assert deviation is not None and deviation < 0.001
    summary = agreement.hourly_summary()
    assert summary is not None and "přesná shoda OHLC 1/6" in summary
    assert agreement.hourly_summary() is None  # vynulováno

    # Jiný kontrakt (roll Z6 × H7 ~1 %) — stráž ho pozná
    rolled = BarAgreement()
    for minute in range(6):
        rolled.add(*_pair(7270.0, 7345.0, minute))
    deviation = rolled.contract_deviation(ES)
    assert deviation is not None and deviation > 0.003
    # Páry jiného streameru neplatí
    assert rolled.contract_deviation("/ESH27:XCME") is None


def test_poradi_puvodu_baru() -> None:
    """Živý IBKR > IBKR historical > bar z tisků tasty > rekonstrukce ze svíček."""
    assert (
        bar_source_rank(None)
        > bar_source_rank("ibkr_hist")
        > bar_source_rank(BAR_SOURCE_TASTY_TRADES)
        > bar_source_rank("tasty_candle")
    )
    assert bar_source_rank("ibkr") == bar_source_rank(None)
