"""Settle kvartálního data podle trading class (#1366, ADR-0039 bod 2).

Na 3. pátek kvartálního měsíce sbírá pipeline po rollu front kontraktu
(ADR-0039 bod 1) týdenní sérii NOVÉHO kontraktu (ES: EW3, NQ: QN3), která se
vypořádá odpoledne. SOQ 9:30 ET patří jen standardní kvartální třídě (kořen
produktu) — opcím na expirující kontrakt (pinovaný ESU6/ESZ6, #1191).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.compute.settle import (
    expiry_settle,
    history_expiry_settle,
    settles_at_soq,
)
from gexlens_engine.compute.setups import born_after_settle, counts_in_stats
from gexlens_engine.discovery_cache import CachedDiscovery, FrontFuture
from gexlens_engine.ibkr.discovery import ExpiryInfo
from gexlens_engine.instruments import expiry_expired, greeks_watch_applies
from gexlens_engine.storage.setups_store import SetupsRepository

# 3. pátek prosince 2026 — zimní čas: SOQ 9:30 EST = 14:30 UTC, 16:00 EST = 21:00 UTC
DEC = "20261218"
DEC_SOQ = dt.datetime(2026, 12, 18, 14, 30, tzinfo=dt.UTC)
DEC_PM = dt.datetime(2026, 12, 18, 21, 0, tzinfo=dt.UTC)
# 3. pátek září 2026 — letní čas: SOQ 13:30 UTC, 16:00 EDT = 20:00 UTC
SEP = "20260918"
SEP_SOQ = dt.datetime(2026, 9, 18, 13, 30, tzinfo=dt.UTC)
SEP_PM = dt.datetime(2026, 9, 18, 20, 0, tzinfo=dt.UTC)


def test_settle_kvartalniho_data_podle_tridy() -> None:
    # Týdenní série nového kontraktu odpoledne (ES i NQ, zima i léto)
    assert expiry_settle(DEC, "EW3", "ES") == DEC_PM
    assert expiry_settle(SEP, "EW3", "ES") == SEP_PM
    assert expiry_settle(SEP, "QN3", "NQ") == SEP_PM
    assert expiry_settle(DEC, "QN3", "MNQ") == DEC_PM
    # Standardní kvartální třída = kořen produktu → SOQ; i u pinovaného tickeru
    assert expiry_settle(DEC, "ES", "ES") == DEC_SOQ
    assert expiry_settle(DEC, "ES", "ESZ6") == DEC_SOQ
    assert expiry_settle(SEP, "NQ", "NQU6") == SEP_SOQ
    assert expiry_settle(DEC, "MES", "MES") == DEC_SOQ
    # Bez třídy nebo symbolu (historie před #1366) rozhoduje datum jako dřív
    assert expiry_settle(DEC) == DEC_SOQ
    assert expiry_settle(DEC, "EW3") == DEC_SOQ
    assert expiry_settle(DEC, None, "ES") == DEC_SOQ
    # Mimo kvartální datum třída nic nemění: 16:00 ET
    assert not settles_at_soq(dt.date(2026, 12, 11), "ES", "ES")
    assert expiry_settle("20261211", "ES", "ES") == dt.datetime(2026, 12, 11, 21, 0, tzinfo=dt.UTC)
    assert expiry_settle("nesmysl", "EW3", "ES") is None


def test_roll_a_hlidka_greeks_kvartalniho_patku_podle_tridy() -> None:
    late_morning = dt.datetime(2026, 12, 18, 16, 0, tzinfo=dt.UTC)  # 11:00 EST
    # Týdenní série žije do 16:00 ET — pipeline nad ní neroluje v SOQ
    assert not expiry_expired(DEC, late_morning, "EW3", "ES")
    assert greeks_watch_applies(DEC, late_morning, "EW3", "ES")
    assert not expiry_expired(DEC, DEC_PM - dt.timedelta(seconds=1), "EW3", "ES")
    assert expiry_expired(DEC, DEC_PM, "EW3", "ES")
    # Standardní třída (pinovaný expirující kontrakt) roluje v SOQ
    assert expiry_expired(DEC, DEC_SOQ, "ES", "ESZ6")
    assert not greeks_watch_applies(DEC, late_morning, "ES", "ESZ6")


def test_cache_discovery_kvartalni_patek_drzi_tydenni_serii() -> None:
    cached = CachedDiscovery(
        symbol="ES",
        stored_at=dt.datetime(2026, 12, 18, 6, 0, tzinfo=dt.UTC),
        front=FrontFuture(
            symbol="ES",
            con_id=1,
            exchange="CME",
            multiplier="50",
            last_trade_date="20270319",
            local_symbol="ESH7",
            trading_class="ES",
        ),
        expiries=(
            ExpiryInfo("EW3", DEC, "CME", "50", (6000.0,)),
            ExpiryInfo("E1A", "20261221", "CME", "50", (6000.0,)),
        ),
    )
    late_morning = dt.datetime(2026, 12, 18, 16, 0, tzinfo=dt.UTC)
    assert [info.expiry for info in cached.unexpired(late_morning)] == [DEC, "20261221"]
    assert [info.expiry for info in cached.unexpired(DEC_PM)] == ["20261221"]


def test_vznik_a_statistiky_setupu_ctou_tridu_z_kontextu() -> None:
    late_morning = dt.datetime(2026, 12, 18, 16, 0, tzinfo=dt.UTC)
    assert not born_after_settle(DEC, late_morning, "EW3", "ES")
    assert born_after_settle(DEC, late_morning, "ES", "ES")
    # Řádek před #1366 bez třídy se čte podle data (SOQ)
    assert not counts_in_stats(DEC, late_morning, {}, "ES")
    assert counts_in_stats(DEC, late_morning, {"trading_class": "EW3"}, "ES")
    assert not counts_in_stats(DEC, late_morning, {"trading_class": "ES"}, "ES")


def test_repository_kvartalni_patek_tydenni_serie_ve_statistikach(tmp_path: Path) -> None:
    """Setup týdenní série vzniklý po SOQ se v tabulce neoznačí „po settle“,
    počítá se do statistik a po restartu nese settle 16:00 ET."""
    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.sqlite'}"))
    repository.ensure_schema()
    created = dt.datetime(2026, 12, 18, 16, 0, tzinfo=dt.UTC)

    def setup(context: dict[str, object]) -> int:
        return repository.create(
            symbol="ES",
            expiry=DEC,
            template="failed_break",
            direction="long",
            created_ts=created,
            entry=6000.0,
            target=6010.0,
            stop=5995.0,
            confidence=50,
            reason="test",
            context=context,
        )

    weekly = setup({"trading_class": "EW3"})
    legacy = setup({})
    rows = {row["id"]: row for row in repository.list_for("ES")}
    assert rows[weekly]["after_settle"] is False
    assert rows[legacy]["after_settle"] is True  # bez třídy podle data
    active = {item.id: item for item in repository.active_for("ES")}
    assert active[weekly].trading_class == "EW3"
    assert active[weekly].settle() == DEC_PM
    assert active[legacy].trading_class is None
    assert active[legacy].settle() == DEC_SOQ
    facts = {fact.id: fact for fact in repository.summary_facts(["ES"])}
    assert facts[weekly].trading_class == "EW3"
    assert not facts[weekly].after_settle and facts[weekly].in_stats
    assert facts[legacy].after_settle


def test_historie_bez_tridy_dovodi_settle_z_tickeru() -> None:
    """Offline nástroje (přepočet, backtest): bez třídy na kvartální datum
    kořenový ticker = týdenní série po rollu, pinovaný expirující = SOQ."""
    assert history_expiry_settle(DEC, "ES") == (DEC_PM, True)
    assert history_expiry_settle(DEC, "ESZ6") == (DEC_SOQ, True)
    assert history_expiry_settle(DEC, "ESH7") == (DEC_PM, True)  # další kontrakt
    assert history_expiry_settle(SEP, "NQU6") == (SEP_SOQ, True)
    # Se třídou nic nedovozuje; mimo kvartální datum také ne
    assert history_expiry_settle(DEC, "ES", "ES") == (DEC_SOQ, False)
    assert history_expiry_settle("20261211", "ES") == (
        dt.datetime(2026, 12, 11, 21, 0, tzinfo=dt.UTC),
        False,
    )
    assert history_expiry_settle("nesmysl", "ES") == (None, False)
