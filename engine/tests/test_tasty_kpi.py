"""KPI stability tasty streamu (#1214): delty počítadel, RTH mlčení, uzávěrka seance, verdikty."""

import datetime as dt
import json
from pathlib import Path

from gexlens_engine.tasty.kpi import StreamKpi

RTH = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)  # 10:00 ET, uvnitř RTH
NIGHT = dt.datetime(2026, 9, 17, 3, 0, tzinfo=dt.UTC)  # Globex noc, mimo RTH


def sample(kpi: StreamKpi, now: dt.datetime, **overrides: object) -> None:
    values: dict[str, object] = {
        "connected": True,
        "reconnects": 0,
        "rate_limited": 0,
        "heals": 0,
        "errors": 0,
        "last_event_at": now - dt.timedelta(seconds=5),
        "rate_limit_active": False,
        "tasty_dead_share": 0.0,
        "greeks_share": 0.98,
    }
    values.update(overrides)
    kpi.observe(now, **values)  # type: ignore[arg-type]


def test_rozpracovana_seance_prezije_restart(tmp_path: Path) -> None:
    """#1214: restart uprostřed seance (18. 9. 23:05) nesmí zahodit RTH část dne."""
    state = tmp_path / "tasty-kpi-current.json"
    report = tmp_path / "kpi.jsonl"
    kpi = StreamKpi(report_path=report, state_path=state)
    sample(kpi, RTH, reconnects=3)
    sample(kpi, RTH + dt.timedelta(minutes=1), reconnects=4)
    sample(kpi, RTH + dt.timedelta(minutes=2), last_event_at=RTH - dt.timedelta(minutes=5))
    assert state.exists()

    # „Restart“: nová instance načte stav, počítadla streamu začínají od nuly
    restarted = StreamKpi(report_path=report, state_path=state)
    assert restarted.current is not None
    assert restarted.current.minutes == 3
    assert restarted.current.rth_minutes == 3
    assert restarted.current.rth_silent == 1
    assert restarted.current.drops == 1
    # Táž minuta jako před restartem = no-op (nepočítá se dvakrát)
    sample(restarted, RTH + dt.timedelta(minutes=2), reconnects=0)
    assert restarted.current.minutes == 3
    # První vzorek po restartu jen primuje: reconnects 0 → 1 se počítá až od druhého
    sample(restarted, RTH + dt.timedelta(minutes=3), reconnects=0)
    sample(restarted, RTH + dt.timedelta(minutes=4), reconnects=1)
    assert restarted.current.minutes == 5
    assert restarted.current.drops == 2

    # Nová seance uzavře navázanou do reportu s plným počtem minut
    sample(restarted, RTH + dt.timedelta(days=1))
    rows = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["session"] == "2026-09-17" and rows[-1]["minutes"] == 5


def test_poskozeny_stav_zacina_od_nuly(tmp_path: Path) -> None:
    state = tmp_path / "tasty-kpi-current.json"
    state.write_text("{nesmysl", encoding="utf-8")
    kpi = StreamKpi(state_path=state)
    assert kpi.current is None
    sample(kpi, RTH)
    assert kpi.current is not None and kpi.current.minutes == 1


def test_delty_pocitadel_a_rth_mlceni(tmp_path: Path) -> None:
    kpi = StreamKpi(report_path=tmp_path / "kpi.jsonl")
    # První vzorek jen zapamatuje historii (5 reconnectů z noci se nepočítá)
    sample(kpi, RTH, reconnects=5, rate_limited=60)
    sample(
        kpi, RTH + dt.timedelta(minutes=1), reconnects=6, rate_limited=61, rate_limit_active=True
    )
    sample(kpi, RTH + dt.timedelta(minutes=1))  # táž minuta = no-op
    sample(kpi, RTH + dt.timedelta(minutes=2), last_event_at=RTH - dt.timedelta(minutes=5))
    sample(kpi, RTH + dt.timedelta(minutes=3), tasty_dead_share=0.2, greeks_share=0.5)
    today = kpi.current
    assert today is not None
    assert today.minutes == 4 and today.rth_minutes == 4
    assert today.drops == 1 and today.rate_limit_events == 1 and today.rate_limit_minutes == 1
    assert today.rth_silent == 1 and today.rth_tasty_dead == 1
    assert today.greeks_share is not None and abs(today.greeks_share - (0.98 * 3 + 0.5) / 4) < 1e-9
    verdicts = today.verdicts()
    assert verdicts["drops"] is True and verdicts["rate_limit"] is False
    assert verdicts["silent"] is False  # 1/4 minut bez eventu
    assert kpi.status_fields()["today"]["passed"] is False  # type: ignore[index]


def test_noc_se_nepocita_do_rth_a_seance_se_uzavre_do_reportu(tmp_path: Path) -> None:
    path = tmp_path / "kpi.jsonl"
    kpi = StreamKpi(report_path=path)
    sample(kpi, NIGHT, last_event_at=None)  # noc: mlčení se do RTH nepočítá
    sample(kpi, NIGHT + dt.timedelta(minutes=1), connected=False)
    assert kpi.current is not None and kpi.current.rth_minutes == 0
    assert kpi.current.disconnected_minutes == 1
    # Další obchodní seance (Globex začíná 22:00 UTC předchozího dne) → uzávěrka
    sample(kpi, dt.datetime(2026, 9, 17, 23, 0, tzinfo=dt.UTC))
    assert kpi.last_closed is not None and kpi.last_closed.session == "2026-09-17"
    assert kpi.current.session == "2026-09-18"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["session"] == "2026-09-17"
    assert rows[0]["verdicts"]["silent"] is None  # bez RTH minut = neměřeno
    assert rows[0]["passed"] is True  # nic neprošlo jako False
