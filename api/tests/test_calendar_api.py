"""Kalendář expirací (#1189): fáze kvartálního týdne, SOQ, značky."""

import datetime as dt

from fastapi.testclient import TestClient

from gexlens_api.main import create_app
from gexlens_engine.config import Settings


def test_calendar_expiry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        data_dir=tmp_path, database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}"
    )
    client = TestClient(create_app(settings))
    payload = client.get("/calendar/expiry", params={"date": "2026-09-16"}).json()
    assert payload["phase"] == "opex_week" and payload["quarterly_expiry"] == "2026-09-18"
    assert payload["roll_date"] == "2026-09-10" and payload["soq_ts"] == "2026-09-18T13:30:00+00:00"
    assert payload["days_to_expiry"] == 2 and payload["is_opex_week"] is True
    kinds = {(m["kind"], m["date"]) for m in payload["markers"]}
    assert ("quarterly_expiry", "2026-09-18") in kinds and ("roll", "2026-09-10") in kinds
    # Bez data = dnešní seance
    today = client.get("/calendar/expiry").json()
    assert dt.date.fromisoformat(today["today"]) >= dt.date(2026, 9, 1)
