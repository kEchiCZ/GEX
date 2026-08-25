"""Široký OI archiv z tasty (#828, varianta A)."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.tasty.symbols import ChainSymbols
from gexlens_engine.tasty.wideoi import wide_contracts, wide_records

DAY = dt.date(2026, 8, 25)
EXPIRY = "20260825"


def spec(strike: float, right: str) -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry=EXPIRY,
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E1A",
        multiplier="50",
    )


def chain() -> ChainSymbols:
    by_contract = {}
    for strike in (7400.0, 7500.0, 7600.0, 7700.0):
        for right in ("C", "P"):
            by_contract[(EXPIRY, strike, right)] = f".ES{int(strike)}{right}"
    # Jiná expirace do výběru nesmí spadnout
    by_contract[("20260826", 7500.0, "C")] = ".ESZ7500C"
    return ChainSymbols(product="ES", day=DAY, by_contract=by_contract)


def test_vybere_jen_striky_mimo_ibkr_obalku() -> None:
    """Dva zdroje se nikdy nesmí prát o týž řádek — proto disjunktní výběr."""
    covered = [spec(7500.0, "C"), spec(7500.0, "P"), spec(7600.0, "C")]

    out = wide_contracts(
        chain(), EXPIRY, covered, symbol="ES", exchange="CME", multiplier="50", trading_class="E1A"
    )

    keys = sorted((c.strike, c.right) for c, _ in out)
    assert keys == [(7400.0, "C"), (7400.0, "P"), (7600.0, "P"), (7700.0, "C"), (7700.0, "P")]
    assert all(c.expiry == EXPIRY for c, _ in out)  # cizí expirace vynechána


def test_kontrakt_bez_oi_se_nezapisuje_jako_nula() -> None:
    """Chybějící Summary je „nevíme", ne naměřená nula — jinak by to zkreslilo
    agregáty stejně jako dnešní useknutý řetěz."""
    contracts = [(spec(7400.0, "C"), ".ES7400C"), (spec(7400.0, "P"), ".ES7400P")]
    oi = {".ES7400C": 0.0}  # strike bez otevřených pozic = platná nula

    records = wide_records(contracts, lambda s: oi.get(s), DAY)

    assert len(records) == 1
    assert records[0].strike == 7400.0 and records[0].oi == 0.0


def test_tasty_zapis_neprepise_cas_ibkr_snimku(tmp_path: Path) -> None:
    """Jádro varianty A: `captured_ts` zůstává IBKR, tasty má vlastní sloupec.

    Na `captured_ts` stojí detektor finality (#463) i invalidace Max Pain
    cache (#826) — průběžný tasty zápis ho nesmí posouvat.
    """
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    repository.ensure_schema()
    ibkr_at = dt.datetime(2026, 8, 25, 8, 30, tzinfo=dt.UTC)
    repository.upsert_many(
        [OIRecord("ES", EXPIRY, 7500.0, "C", DAY, 100.0, trading_class="E1A")],
        captured_ts=ibkr_at,
    )

    repository.upsert_many(
        [OIRecord("ES", EXPIRY, 7400.0, "P", DAY, 250.0, trading_class="E1A")],
        tasty_captured_ts=dt.datetime(2026, 8, 25, 14, 0, tzinfo=dt.UTC),
    )

    # Čas ranního IBKR snímku se nehnul
    assert repository.captured_at("ES", DAY) == ibkr_at
    # Široký strike je v archivu a vstupuje do Σ čtení
    assert repository.get_oi("ES", DAY, 7400.0, "P", expiry=EXPIRY) == 250.0


def test_wide_streamers_bere_jen_striky_mimo_obalku() -> None:
    """#828: subskripce pro aktivní expiraci — extended plán ji neobsahuje."""
    from gexlens_engine.tasty.wideoi import wide_streamers

    covered = [spec(7500.0, "C"), spec(7500.0, "P")]

    out = wide_streamers(chain(), EXPIRY, covered, center=7500.0, band_pct=1.5)

    # ±1,5 % z 7500 = ±112 b → 7400 a 7600 uvnitř (100 b), 7700 mimo (200 b)
    assert ".ES7400C" in out and ".ES7600P" in out
    assert ".ES7500C" not in out  # pokrývá IBKR
    assert ".ES7700C" not in out  # mimo pásmo

    # Bez centra se nesubskribuje nic — slepé pokrytí by jen sežralo kapacitu
    assert wide_streamers(chain(), EXPIRY, covered, center=None, band_pct=1.5) == set()
