"""Testy ranního retro passu (#284): plánování, pořadí fází, odolnost."""

import datetime as dt

from gexlens_news.retro_pass import RetroPass, should_run

RUN_AT = dt.time(5, 30)


class FakeJob:
    def __init__(self, result: object = 0, *, fails: bool = False) -> None:
        self.result = result
        self.fails = fails
        self.calls: list[dt.datetime] = []

    def run(self, now: dt.datetime) -> object:
        self.calls.append(now)
        if self.fails:
            raise RuntimeError("job selhal")
        return self.result


def make(**overrides: object) -> tuple[RetroPass, dict[str, FakeJob]]:
    jobs = {
        "classification": FakeJob(3),
        "reactions": FakeJob(8),
        # SentIndexJob vrací (počet bodů, topicy)
        "sentindex": FakeJob((120, [])),
    }
    jobs.update(overrides)  # type: ignore[arg-type]
    return (
        RetroPass(jobs["classification"], jobs["reactions"], jobs["sentindex"], run_at=RUN_AT),
        jobs,
    )


# ── Plánování ──────────────────────────────────────────────────────


def test_runs_once_a_day_after_the_configured_time() -> None:
    day = dt.date(2026, 7, 29)
    before = dt.datetime.combine(day, dt.time(4, 0), tzinfo=dt.UTC)
    after = dt.datetime.combine(day, dt.time(6, 0), tzinfo=dt.UTC)

    assert not should_run(before, RUN_AT, None)
    assert should_run(after, RUN_AT, None)
    # Už proběhl dnes → podruhé ne
    assert not should_run(after, RUN_AT, day)
    # Zítra zase ano
    assert should_run(after + dt.timedelta(days=1), RUN_AT, day)


def test_late_start_still_catches_up() -> None:
    """Po výpadku se pass dožene i se zpožděním, nevynechá celý den."""
    late = dt.datetime(2026, 7, 29, 11, 0, tzinfo=dt.UTC)
    assert should_run(late, RUN_AT, None)


def test_due_uses_internal_state() -> None:
    retro, _ = make()
    now = dt.datetime(2026, 7, 29, 6, 0, tzinfo=dt.UTC)
    assert retro.due(now)
    retro.run(now)
    assert not retro.due(now)
    assert retro.last_run == now.date()


# ── Fáze ───────────────────────────────────────────────────────────


def test_phases_run_in_order_and_report_counts() -> None:
    """Klasifikace první — bez kategorie by event nevstoupil do modelu."""
    retro, jobs = make()
    now = dt.datetime(2026, 7, 29, 6, 0, tzinfo=dt.UTC)

    result = retro.run(now)

    assert jobs["classification"].calls == [now]
    assert jobs["reactions"].calls == [now]
    assert jobs["sentindex"].calls == [now]
    assert result.classified == 3
    assert result.reactions == 8
    assert result.index_points == 120
    assert result.processed == 11
    assert "11 položek" in result.describe()


def test_failing_phase_does_not_stop_the_rest() -> None:
    """Retro pass má dohnat, co jde — ne spadnout na první chybě."""
    retro, jobs = make(classification=FakeJob(fails=True))
    now = dt.datetime(2026, 7, 29, 6, 0, tzinfo=dt.UTC)

    result = retro.run(now)

    assert result.classified == 0  # selhala
    assert result.reactions == 8  # ostatní proběhly
    assert result.index_points == 120
    assert jobs["sentindex"].calls == [now]
    # I při selhání se den označí za zpracovaný, jinak by pass běžel ve smyčce
    assert retro.last_run == now.date()


def test_quiet_night_reports_zero() -> None:
    """Nula neznamená chybu — noc byla průběžně zpracovaná."""
    retro, _ = make(classification=FakeJob(0), reactions=FakeJob(0))
    result = retro.run(dt.datetime(2026, 7, 29, 6, 0, tzinfo=dt.UTC))
    assert result.processed == 0
    assert "0 položek" in result.describe()
