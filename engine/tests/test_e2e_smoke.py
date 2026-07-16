"""E2E smoke (issue #31): engine(mock) → storage → API, golden hodnoty všech výpočtů.

Referenční den je deterministický (generátor + golden hodnoty verzované v repu
— dataset se regeneruje při testu, LFS není potřeba). Běží v CI bez live API.
"""

import datetime as dt
import io
import json
from pathlib import Path

import pandas as pd
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from gexlens_api.main import create_app
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import QuoteSnapshot, SubscriptionScheduler
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, NullPublisher
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import SnapshotWriter

GOLDEN = json.loads(
    (Path(__file__).parent / "golden" / "e2e_reference_day.json").read_text(encoding="utf-8")
)
DAY_START = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
CALL_VOLUME_BASE = 20.0
PUT_VOLUME_BASE = 10.0


class DeterministicStreamer:
    """Kotace jako čistá funkce (kontrakt, minuta) — referenční den je reprodukovatelný."""

    def __init__(self) -> None:
        self.minute = 0

    async def fetch_quote(self, spec: OptionContractSpec, timeout_s: float) -> QuoteSnapshot:
        base = CALL_VOLUME_BASE if spec.right == "C" else PUT_VOLUME_BASE
        return QuoteSnapshot(
            bid=10.0,
            ask=10.5,
            last=10.4,  # nad midem → midpoint sign +1 (CumΔ bar větev)
            volume=(self.minute + 1) * base,
            iv=0.15,
            delta=0.5 if spec.right == "C" else -0.5,
            gamma=0.01,
            theta=-0.5,
            vega=1.2,
        )


def reference_contracts() -> list[OptionContractSpec]:
    return [
        OptionContractSpec("ES", "FOP", "20260716", strike, right, "CME", "E3D", "50")
        for strike in GOLDEN["strikes"]
        for right in ("C", "P")
    ]


@pytest.fixture
def reference_day(tmp_path: Path) -> Settings:
    """Vygeneruje referenční den přes celou engine pipeline (mock → storage)."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.db'}",
    )
    specs = reference_contracts()
    repository = OIEodRepository(create_engine(settings.database_url))
    repository.ensure_schema()
    day = DAY_START.date()
    records = [
        OIRecord("ES", "20260716", strike, "C", day, GOLDEN["oi_call"][str(strike)])
        for strike in GOLDEN["strikes"]
    ] + [
        OIRecord("ES", "20260716", strike, "P", day, GOLDEN["oi_put"][str(strike)])
        for strike in GOLDEN["strikes"]
    ]
    repository.upsert_many(records)
    streamer = DeterministicStreamer()
    runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(streamer, settings),
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=NullPublisher(),
        symbol="ES",
        expiry="20260716",
        multiplier=50.0,
        contracts=specs,
    )
    import asyncio

    async def generate() -> None:
        for minute in range(int(GOLDEN["minutes"])):
            streamer.minute = minute
            ts = DAY_START + dt.timedelta(minutes=minute)
            bar = Bar(
                ts=ts, open=7599.0, high=7601.0, low=7598.0, close=GOLDEN["spot"], volume=500.0
            )
            await runtime.run_cycle(ts, GOLDEN["spot"], [bar])

    asyncio.run(generate())
    return settings


def test_e2e_engine_storage_api_golden(reference_day: Settings) -> None:
    """AC: mock replay celé pipeline; API vrací golden hodnoty všech výpočtů."""
    client = TestClient(create_app(reference_day))
    expected = GOLDEN["expected"]
    day = DAY_START.date().isoformat()

    # Instruments + expirace z uložených dat
    assert client.get("/instruments").json() == {"instruments": ["ES"]}
    assert client.get("/instruments/ES/expiries").json() == {"expiries": ["20260716"]}

    # Snapshoty (raw Arrow): kompletní matice dne
    raw = client.get("/snapshots/ES/20260716", params={"date": day, "raw": "true"})
    with pyarrow.ipc.open_stream(io.BytesIO(raw.content)) as reader:
        frame = reader.read_all().to_pandas()
    assert len(frame) == expected["snapshot_rows"]
    assert frame["oi"].max() == max(GOLDEN["oi_put"].values())

    # Levels: golden flip/walls/total v každé minutě (profil je konstantní)
    levels = client.get("/levels/ES/20260716", params={"date": day}).json()["levels"]
    assert len(levels) == GOLDEN["minutes"]
    for row in levels:
        assert row["flip"] == pytest.approx(expected["flip"])
        assert row["call_wall"] == expected["call_wall"]
        assert row["put_wall"] == expected["put_wall"]
        assert row["total_gex"] == pytest.approx(expected["total_gex"])

    # Flow: CumΔ dle ručního výpočtu (první minuta bez přírůstku)
    flow = client.get("/flow/ES", params={"date": day}).json()
    cum = [row["cum_delta"] for row in flow["flow"]]
    assert cum[0] == pytest.approx(0.0)
    assert cum[1] == pytest.approx(expected["flow_delta_per_minute"])
    assert cum[-1] == pytest.approx(expected["cum_delta_final"])

    # Replay balík je kompletní a dekódovatelný (vstup pro frontend playback)
    bundle = client.get(f"/replay/ES/20260716/{day}").json()
    assert len(bundle["levels"]) == GOLDEN["minutes"]
    assert len(bundle["bars"]) == GOLDEN["minutes"]
    assert len(bundle["snapshots_arrow_base64"]) > 0

    # Heatmap matice v OI módu se počítá bez chyby (Arrow stream)
    matrix = client.get("/snapshots/ES/20260716", params={"date": day, "mode": "oi"})
    assert matrix.status_code == 200


def test_reference_day_deterministic(reference_day: Settings, tmp_path: Path) -> None:
    """Dataset je reprodukovatelný — verzování řeší generátor + golden JSON (bez LFS)."""
    day = DAY_START.date().isoformat()
    first = pd.read_parquet(reference_day.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    # Stejné vstupy → stejné hodnoty (žádný náhodný stav v pipeline)
    volumes = first[(first["strike"] == 7600.0) & (first["right"] == "C")]["volume"].tolist()
    assert volumes == [20.0, 40.0, 60.0, 80.0, 100.0]
