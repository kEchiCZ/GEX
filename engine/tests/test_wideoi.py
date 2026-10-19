"""Široký OI archiv z tasty (#828, varianta A)."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, select

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord, oi_eod_table
from gexlens_engine.tasty.symbols import ChainSymbols
from gexlens_engine.tasty.wideoi import wide_contracts, wide_oi_pass, wide_records

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
    """IBKR hodnotu tasty nepřepisuje — striky s IBKR řádkem se vynechají."""
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


def _repository(tmp_path: Path) -> OIEodRepository:
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    repository.ensure_schema()
    return repository


def _stored(repository: OIEodRepository) -> dict[tuple[float, str], tuple[float, float | None]]:
    """(strike, strana) → (OI, iv) přímo z tabulky."""
    with repository._engine.connect() as conn:
        rows = conn.execute(
            select(
                oi_eod_table.c.strike, oi_eod_table.c["right"], oi_eod_table.c.oi, oi_eod_table.c.iv
            )
        ).all()
    return {(float(r.strike), str(r.right)): (float(r.oi), r.iv) for r in rows}


def _tasty_stamps(repository: OIEodRepository) -> dict[tuple[float, str], dt.datetime | None]:
    """Čas posledního tasty zápisu per strana (sqlite vrací naivní UTC)."""
    with repository._engine.connect() as conn:
        rows = conn.execute(
            select(oi_eod_table.c.strike, oi_eod_table.c["right"], oi_eod_table.c.tasty_captured_ts)
        ).all()
    return {
        (float(r.strike), str(r.right)): (
            None if r.tasty_captured_ts is None else r.tasty_captured_ts.replace(tzinfo=dt.UTC)
        )
        for r in rows
    }


async def _pass(repository: OIEodRepository, oi: dict[str, float], now: dt.datetime) -> int:
    return await wide_oi_pass(
        repository,
        chain(),
        symbol="ES",
        expiry=EXPIRY,
        sample=spec(7500.0, "C"),
        oi_lookup=oi.get,
        now=now,
    )


async def test_druhy_beh_obnovi_jen_zmeneny_tasty_radek(tmp_path: Path) -> None:
    """#1605: do opravy byl po prvním zápisu dne každý strike „pokrytý" vlastním
    tasty řádkem a publikace CME (~12:00 UTC) se do archivu nedostala. Teď druhý
    běh změněnou hodnotu přepíše, nezměněnou nezapíše a IBKR řádek nechá."""
    repository = _repository(tmp_path)
    ibkr_at = dt.datetime(2026, 8, 25, 13, 1, tzinfo=dt.UTC)
    repository.upsert_many(
        [OIRecord("ES", EXPIRY, 7500.0, "C", DAY, 100.0, iv=0.2, trading_class="E1A")],
        captured_ts=ibkr_at,
    )
    oi = dict.fromkeys(chain().by_contract.values(), 10.0)
    midnight = dt.datetime(2026, 8, 25, 0, 0, tzinfo=dt.UTC)

    assert await _pass(repository, oi, midnight) == 7  # celý řetěz mimo IBKR stranu
    assert await _pass(repository, oi, midnight) == 0  # beze změny se nezapisuje nic

    oi[".ES7400P"] = 55.0  # publikace CME
    oi[".ES7500C"] = 999.0  # IBKR strana — nesmí se přepsat
    after_pub = dt.datetime(2026, 8, 25, 12, 30, tzinfo=dt.UTC)
    assert await _pass(repository, oi, after_pub) == 1

    stored = _stored(repository)
    assert stored[(7400.0, "P")] == (55.0, None)
    assert stored[(7500.0, "C")] == (100.0, 0.2)  # IBKR hodnota i greeks zůstaly
    assert repository.captured_at("ES", DAY) == ibkr_at
    stamps = _tasty_stamps(repository)
    assert stamps[(7400.0, "P")] == after_pub
    assert stamps[(7400.0, "C")] == midnight  # nezměněná hodnota se nepřepsala
    assert stamps[(7500.0, "C")] is None


async def test_radek_tasty_prepsany_ibkr_je_pokryty(tmp_path: Path) -> None:
    """Rozšíření obálky (#828): IBKR přepíše tasty řádek a ten pak nese oba časy.
    Původ je IBKR (vyplněný `captured_ts`), tasty ho od té chvíle nemění."""
    repository = _repository(tmp_path)
    oi = dict.fromkeys(chain().by_contract.values(), 10.0)
    midnight = dt.datetime(2026, 8, 25, 0, 0, tzinfo=dt.UTC)
    assert await _pass(repository, oi, midnight) == 8
    repository.upsert_many(
        [OIRecord("ES", EXPIRY, 7400.0, "P", DAY, 40.0, iv=0.3, trading_class="E1A")],
        captured_ts=dt.datetime(2026, 8, 25, 13, 31, tzinfo=dt.UTC),
    )

    oi[".ES7400P"] = 77.0
    assert await _pass(repository, oi, dt.datetime(2026, 8, 25, 14, 0, tzinfo=dt.UTC)) == 0

    assert _stored(repository)[(7400.0, "P")] == (40.0, 0.3)
    assert _tasty_stamps(repository)[(7400.0, "P")] == midnight  # oba časy, původ IBKR
