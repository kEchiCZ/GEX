"""Živý stav plánu opce QQQ (F3b #1757, ADR-0062 K5 B): přechody, zprávy, idempotence, brány.

Golden 2. 11. je plán v 9:31 ET z `option_plan_1741.json` se syntetickými kotacemi
(`option_plan_result_1748.json`). Ostatní případy stojí na ručně složeném plánu
z `test_option_plan_result_1748` (short, flip 600, EM 5, cíle 595 / 593, 17. 11.).
PG = SQLite v `tmp_path`, Parquet v `tmp_path`.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_option_plan_eval_1748 import Harness
from test_option_plan_result_1748 import (
    DAY,
    EXIT,
    EXPIRY,
    GOLDEN,
    GOLDEN_BARS,
    GOLDEN_EXPIRY,
    GOLDEN_QUOTES,
    OPEN,
    STRIKES,
    TEST_AND_ENTRY,
    Quote,
    _plan,
    fives,
    flat,
    part,
    synthetic_quotes,
    until_exit,
)
from test_tasty_plan_quotes import _Harness as RecorderHarness
from test_tasty_plan_quotes import _plan as recorder_plan
from test_tasty_plan_quotes import _ts as recorder_ts

import gexlens_engine.option_plan_live as live
from gexlens_engine.compute.option_plan import (
    MAIN_RULES,
    NO_ENTRY_BY_EXIT,
    WAITING,
    AccountParams,
    LiveTransition,
    PlanResult,
    evaluate_plan,
    live_transitions,
    plan_to_json,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.option_plan_live import (
    ALERT_KIND,
    OptionPlanLiveWatcher,
    live_message,
    live_part,
)
from gexlens_engine.storage.parquet_store import OptionQuoteRow

UTC = dt.UTC
MINUTE = dt.timedelta(minutes=1)
PRAGUE = live.PRAGUE_TZ
#: Syntetické parametry účtu testu (ne skutečný účet; výchozí hodnota v kódu není)
ACCOUNT_USD = 10_000.0


def account(max_loss_usd: float | None = 150.0) -> AccountParams:
    return AccountParams(account_usd=ACCOUNT_USD, max_loss_usd=max_loss_usd)


def _at(hour: int, minute: int, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def hand(**overrides: Any) -> dict[str, Any]:
    """Ručně složený plán s úrovní vstupu A (flip 600) jako `plan_to_json`."""
    return part(**({"level_a": ["flip", 600.0]} | overrides))


def live_at(
    plan: dict[str, Any],
    bars: Sequence[Bar],
    now: dt.datetime | None,
    *,
    quotes: Sequence[Quote] | None = None,
    max_loss_usd: float | None = 150.0,
) -> PlanResult:
    expiry = dt.date.fromisoformat(plan["variants"][0]["expiry"])
    return evaluate_plan(
        plan,
        rules=MAIN_RULES,
        bars=bars,
        quotes=quotes if quotes is not None else synthetic_quotes(bars, STRIKES, expiry),
        max_loss_usd=max_loss_usd,
        now=now,
    )


def first_seen(
    plan: dict[str, Any],
    bars: Sequence[Bar],
    start: dt.datetime,
    end: dt.datetime,
    **kwargs: Any,
) -> dict[str, tuple[dt.datetime, LiveTransition, str]]:
    """Přechody po minutách jako hlídač: kdy se který poprvé objevil a jeho zpráva."""
    params = kwargs.pop("params", None) or account(kwargs.get("max_loss_usd", 150.0))
    seen: dict[str, tuple[dt.datetime, LiveTransition, str]] = {}
    now = start
    while now <= end:
        result = live_at(plan, bars, now, **kwargs)
        for transition in live_transitions(result, now):
            if transition.event not in seen:
                seen[transition.event] = (
                    now,
                    transition,
                    live_message(1, plan, result, transition, params),
                )
        now += MINUTE
    return seen


# ── golden 2. 11. ───────────────────────────────────────────────────

GOLDEN_PART = plan_to_json(_plan(bars=()))
GOLDEN_OPEN = dt.datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
GOLDEN_EXIT = dt.datetime(2026, 11, 2, 20, 45, tzinfo=UTC)


def golden_at(now: dt.datetime | None) -> PlanResult:
    return live_at(
        GOLDEN_PART, GOLDEN_BARS, now, quotes=GOLDEN_QUOTES, max_loss_usd=GOLDEN["max_loss_usd"]
    )


def test_golden_po_minutach_vstup_tp1_tp2_jednou() -> None:
    """AC #1757: vstup A 16:35 SEČ, TP1 v 5min baru 18:30–18:35, TP2 18:45, nic dalšího."""
    seen = first_seen(
        GOLDEN_PART,
        GOLDEN_BARS,
        GOLDEN_OPEN + MINUTE,
        GOLDEN_EXIT,
        quotes=GOLDEN_QUOTES,
        max_loss_usd=GOLDEN["max_loss_usd"],
    )
    assert list(seen) == ["vstup", "TP1", "TP2"]
    when = {
        event: (now, transition.ts, transition.final)
        for event, (now, transition, _) in seen.items()
    }
    # Přechod se objeví v minutě, kdy skončil bar, který ho nese (hlídač po zápisu minuty)
    assert when["vstup"] == (
        _at(15, 35, GOLDEN_OPEN.date()),
        _at(15, 35, GOLDEN_OPEN.date()),
        False,
    )
    assert when["vstup"][1].astimezone(PRAGUE).strftime("%H:%M") == "16:35"
    tp1 = when["TP1"][1]
    assert when["TP1"] == (tp1, tp1, False)
    assert _at(17, 30, tp1.date()) < tp1 <= _at(17, 35, tp1.date())
    assert when["TP2"] == (_at(17, 45, tp1.date()), _at(17, 45, tp1.date()), True)


def test_golden_zivy_stav_cekani_otevreny_obchod_a_shoda_s_f3() -> None:
    """Před spouští `čeká`; otevřený obchod bez exitu a výstupu z držení; od exitu = F3."""
    before = golden_at(_at(15, 34, GOLDEN_OPEN.date()))
    assert (before.status, before.reasons, before.events) == ("čeká", (WAITING,), ())
    assert live_transitions(before, _at(15, 34, GOLDEN_OPEN.date())) == ()
    open_trade = golden_at(_at(17, 0, GOLDEN_OPEN.date()))
    assert (open_trade.status, open_trade.outcome) == ("spuštěno", None)
    assert [event.kind for event in open_trade.events] == ["vstup"]
    assert all(v.exit_tp1 is None and v.exit_hold is None for v in open_trade.variants)
    after_tp1 = golden_at(_at(17, 40, GOLDEN_OPEN.date()))
    assert [event.kind for event in after_tp1.events] == ["vstup", "TP1"]
    main = after_tp1.variants[0]
    assert main.exit_tp1 is not None and main.exit_hold is None
    want = GOLDEN["expected"]["main"]["variants"]["hlavní"]
    assert main.exit_tp1.bid == pytest.approx(want["bid_tp1"], abs=1e-6)
    # Večerní výsledek F3 = konečný stav téže funkce
    final = golden_at(None)
    assert golden_at(GOLDEN_EXIT) == final
    assert golden_at(settle_ts(GOLDEN_OPEN.date())) == final
    assert golden_at(_at(17, 45, GOLDEN_OPEN.date())).events == final.events


def test_golden_zpravy_vstup_tp1_tp2() -> None:
    """Text podle Rozhodnuto 18. 11. (3 A): vstup se SL, TP, exitem, R:R a verdiktem, bez fotky."""
    seen = first_seen(
        GOLDEN_PART,
        GOLDEN_BARS,
        _at(15, 35, GOLDEN_OPEN.date()),
        _at(17, 45, GOLDEN_OPEN.date()),
        quotes=GOLDEN_QUOTES,
        max_loss_usd=GOLDEN["max_loss_usd"],
    )
    entry, tp1, tp2 = (seen[event][2] for event in ("vstup", "TP1", "TP2"))
    assert entry == (
        "Plán QQQ #1 · VSTUP A 10:35 ET (16:35 Praha) za 697,99, odmítnutí od flip 698,21, "
        "SL 699,66 (5min close). Hlavní put 687: ask 1,19, R:R z kotace 4,41 → obchodovat "
        "(v rozpočtu). Záložní put 686: ask 1,02, R:R z kotace 4,48 → obchodovat (v rozpočtu). "
        "TP1 691,99, TP2 690,59, exit 15:45 ET. TWS: když QQQ ≥ 699,66 → prodat put 687."
    )
    assert tp1 == (
        "Plán QQQ #1 · TP1 691,99 v 12:31 ET (18:31 Praha). Hlavní put 687 bid 2,63 "
        "(+143 $/kontrakt), záložní put 686 bid 2,30 (+128 $/kontrakt). "
        "SL posunut na vstup 697,99."
    )
    assert tp2 == (
        "Plán QQQ #1 · TP2 690,59 v 12:45 ET (18:45 Praha). Hlavní put 687 bid 2,98 "
        "(+179 $/kontrakt), záložní put 686 bid 2,62 (+160 $/kontrakt). Plán končí."
    )
    # Dedup Telegramu bere prvních 80 znaků za 10 min: přechody se v nich liší (16. 11.
    # přišly TP1 a TP2 minutu po sobě)
    assert len({message[:80] for message in (entry, tp1, tp2)}) == 3


# ── ručně složený plán: přechody a zprávy ───────────────────────────

SL_PATH = until_exit(fives(*TEST_AND_ENTRY, (600.9, 599.6, 600.9)), 601.0)
STOP_AT_ENTRY_PATH = until_exit(
    fives(*TEST_AND_ENTRY, (599.0, 594.9, 597.0), (599.7, 597.0, 599.6)), 599.6
)


def test_exit_jen_v_exitu_a_v_exitu_konecny() -> None:
    path = until_exit(fives(*TEST_AND_ENTRY), 597.5)
    plan = hand()
    before = live_at(plan, path, EXIT - MINUTE)
    assert before.outcome is None
    assert [(t.event, t.final) for t in live_transitions(before, EXIT - MINUTE)] == [
        ("vstup", False)
    ]
    result = live_at(plan, path, EXIT)
    transitions = live_transitions(result, EXIT)
    assert [(t.event, t.ts, t.final) for t in transitions] == [
        ("vstup", _at(14, 40), False),
        ("exit", EXIT, True),
    ]
    message = live_message(3, plan, result, transitions[1], account())
    assert message.startswith(
        "Plán QQQ #3 · EXIT 15:45 ET (21:45 Praha), QQQ 597,50. Hlavní put 590"
    )
    assert message.endswith("Plán končí.")


def test_sl_a_stop_na_vstupu() -> None:
    sl = first_seen(hand(), SL_PATH, OPEN + MINUTE, EXIT)
    assert [(event, now) for event, (now, _, _) in sl.items()] == [
        ("vstup", _at(14, 40)),
        ("SL", _at(14, 45)),
    ]
    assert sl["SL"][1].final
    assert sl["SL"][2].startswith("Plán QQQ #1 · SL: 5min close 600,90 v 09:45 ET (15:45 Praha).")
    assert "(−" in sl["SL"][2] and sl["SL"][2].endswith("Plán končí.")
    stop = first_seen(hand(), STOP_AT_ENTRY_PATH, OPEN + MINUTE, EXIT)
    assert list(stop) == ["vstup", "TP1", "stop na vstupu"]
    assert stop["TP1"][2].endswith("SL posunut na vstup 599,50.")
    assert stop["stop na vstupu"][2].startswith(
        "Plán QQQ #1 · STOP NA VSTUPU: 5min close 599,60 v 09:50 ET (15:50 Praha)."
    )


def test_bez_spouste_ceka_a_v_exitu_bez_vstupu() -> None:
    path = flat(598.0, EXIT, OPEN)
    plan = hand()
    waiting = live_at(plan, path, _at(16, 0))
    assert (waiting.status, waiting.reasons) == ("čeká", (WAITING,))
    assert live_transitions(waiting, _at(16, 0)) == ()
    done = live_at(plan, path, EXIT)
    assert (done.status, done.reasons) == ("bez vstupu", (NO_ENTRY_BY_EXIT,))
    (transition,) = live_transitions(done, EXIT)
    assert (transition.event, transition.final) == ("bez vstupu", True)
    assert live_message(4, plan, done, transition, account()) == (
        "Plán QQQ #4 · BEZ VSTUPU do exitu 15:45 ET (21:45 Praha). Plán končí."
    )


def test_plan_bez_urovne_a_ani_bez_opce_se_nesleduje() -> None:
    """Úroveň A do 1 EM chybí = „neobchodovat“ už v 9:15 ET, živý stav nic nehlásí."""
    blocked = hand(levels={"flip": 610.0}, level_a=None)
    result = live_at(blocked, [], _at(15, 0))
    assert result.status == "bez vstupu" and result.reasons[0].startswith("vstup A není")
    assert live_transitions(result, _at(15, 0)) == ()
    stored = {"slot": "9:15", "reasons": [], "levels_nq": {}, "shadows": {}}
    assert live_part({**stored, "main": blocked}) is None
    assert live_part({**stored, "main": None}) is None
    assert live_part({**stored, "main": hand(variants=[])}) is None
    assert live_part({**stored, "main": hand()}) == hand()


def test_mezera_v_barech_jeden_konecny_prechod() -> None:
    """Rozhodnuto 18. 11. (4a A): chybějící 1m bar = mezera celého dne, dál se nesleduje."""
    path = [bar for bar in SL_PATH if bar.ts != _at(14, 42)]
    plan = hand()
    entry = live_at(plan, path, _at(14, 40))
    assert [t.event for t in live_transitions(entry, _at(14, 40))] == ["vstup"]
    gap = live_at(plan, path, _at(14, 43))
    (transition,) = live_transitions(gap, _at(14, 43))
    assert (transition.event, transition.ts, transition.final) == ("mezera", _at(14, 43), True)
    assert live_message(5, plan, gap, transition, account()) == (
        "Plán QQQ #5 · MEZERA: bary QQQ: chybí 1 z 13 minut od US openu do 09:43 ET "
        "(první 09:42 ET). Živý stav se dál nesleduje, výsledek přijde po close."
    )


def test_chybejici_kotace_pri_vstupu_je_mezera_ne_odhad() -> None:
    quotes = [
        q
        for q in synthetic_quotes(SL_PATH, STRIKES, EXPIRY)
        if not (q.strike == 590.0 and q.ts_min == _at(14, 39))
    ]
    plan = hand()
    result = live_at(plan, SL_PATH, _at(14, 45), quotes=quotes)
    main, backup = result.variants
    assert (main.kind, backup.kind) == ("bez kotace", "obchod")
    entry, sl = live_transitions(result, _at(14, 45))
    message = live_message(6, plan, result, entry, account())
    assert (
        "Hlavní put 590: kotace při vstupu chybí → R:R nejde určit, zkontroluj ask v TWS."
        in message
    )
    assert "TWS: když QQQ ≥ 600,85 → prodat put 589." in message
    assert (
        "hlavní put 590: bez kotace při vstupu"
        in live_message(6, plan, result, sl, account()).lower()
    )


def test_vyrazeno_p3_prubeh_dal_hypoteticky() -> None:
    """Rozhodnuto 18. 11. (4b B): obě varianty vyřazené P3 — průběh dál, jen hypoteticky."""
    quotes = synthetic_quotes(STOP_AT_ENTRY_PATH, STRIKES, EXPIRY, ask_factor=3.0)
    seen = first_seen(hand(), STOP_AT_ENTRY_PATH, OPEN + MINUTE, EXIT, quotes=quotes)
    assert list(seen) == ["vstup", "TP1", "stop na vstupu"]
    entry = seen["vstup"][2]
    assert entry.count("→ neobchodovat — R:R ") == 2 and entry.count(" < 2 (P3)") == 2
    assert entry.endswith("Průběh dál jen hypoteticky.") and "TWS:" not in entry
    assert ", hypoteticky (vyřazeno P3). " in seen["TP1"][2]
    assert ", hypoteticky (vyřazeno P3). " in seen["stop na vstupu"][2]


def test_nad_rozpoctem_neobchodovat_p4_a_prubeh_hypoteticky() -> None:
    """K2, bod 4, K12 A: varianta, která projde P3, nad rozpočtem = neobchodovat (P4) jako plán."""
    seen = first_seen(hand(), STOP_AT_ENTRY_PATH, OPEN + MINUTE, EXIT, max_loss_usd=1.0)
    entry = seen["vstup"][2]
    assert entry.count("→ neobchodovat — nad rozpočtem (P4: čerpání −") == 2
    assert "R:R z kotace" in entry and "obchodovat (v rozpočtu)" not in entry
    assert entry.endswith("Průběh dál jen hypoteticky.") and "TWS:" not in entry
    assert ", hypoteticky (neobchodovat). " in seen["TP1"][2]


def test_rozpocet_nenastaven_neobchodovat_jako_plan() -> None:
    """Nevyplněné parametry účtu = „neobchodovat — rozpočet nenastaven“ (K2, bez defaultu)."""
    unset = AccountParams(account_usd=None, max_loss_usd=None)
    seen = first_seen(hand(), SL_PATH, _at(14, 40), _at(14, 45), max_loss_usd=None, params=unset)
    entry = seen["vstup"][2]
    reason = "→ neobchodovat — rozpočet nenastaven (chybí rozpočet na obchod a velikost účtu)"
    assert entry.count(reason) == 2 and "TWS:" not in entry
    assert seen["SL"][2].startswith(
        "Plán QQQ #1 · SL: 5min close 600,90 v 09:45 ET (15:45 Praha), "
    )
    assert ", hypoteticky (neobchodovat). " in seen["SL"][2]


def test_cil_1_za_vstupem_vstup_je_konecny() -> None:
    """Vstup za cílem 1 (`cíl 1 za vstupem`): vstup je konečný přechod, plán končí."""
    plan = hand(targets=[599.8, 593.0])
    result = live_at(plan, SL_PATH, _at(14, 41))
    assert result.outcome == "cíl 1 za vstupem"
    (transition,) = live_transitions(result, _at(14, 41))
    assert (transition.event, transition.final) == ("vstup", True)
    assert live_message(8, plan, result, transition, account()).endswith(
        "Cíl 1 leží za vstupem, plán končí."
    )


# ── hlídač: PG, Parquet, upozornění ─────────────────────────────────


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


#: Hlídač čte parametry účtu ze Settings; tady syntetické
LIVE_ACCOUNT = AccountParams(account_usd=ACCOUNT_USD, max_loss_usd=1234.5)


def watcher(h: Harness, clock: Clock) -> OptionPlanLiveWatcher:
    return OptionPlanLiveWatcher(
        repository=h.repo,
        derived_dir=h.settings.derived_dir,
        account=lambda: LIVE_ACCOUNT,
        publisher=h.publisher,
        clock=clock,
    )


def minutes(
    w: OptionPlanLiveWatcher, clock: Clock, session: dt.date, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, dict[str, Any]]]:
    """Hlídač po minutách (blokující průchod), upozornění 1 s po závěru minuty."""
    out: list[tuple[dt.datetime, dict[str, Any]]] = []
    until = start
    while until <= end:
        clock.now = until + dt.timedelta(seconds=1)
        out.extend((until, payload) for payload in w.run(session, until))
        until += MINUTE
    return out


def golden_quotes(h: Harness) -> None:
    rows = [
        OptionQuoteRow(
            q.ts_min, q.expiry, q.strike, q.right, q.bid, q.ask, None, q.iv, -0.2, q.quote_age_s
        )
        for q in GOLDEN_QUOTES
    ]
    h.writer.write_option_quotes("QQQ", GOLDEN_OPEN.date(), rows)


def test_hlidac_golden_tri_upozorneni_stiny_mlci(tmp_path: Path) -> None:
    """Stín B by vstoupil 15:35 SEČ (K10 B bez upozornění): první zpráva je vstup A 16:35 SEČ."""
    h = Harness(tmp_path)
    day = GOLDEN_OPEN.date()
    plan_id = h.plan(day=day, main=GOLDEN_PART)
    h.bars(GOLDEN_BARS)
    golden_quotes(h)
    clock = Clock(GOLDEN_OPEN)
    w = watcher(h, clock)
    sent = minutes(w, clock, day, GOLDEN_OPEN + MINUTE, GOLDEN_EXIT)
    assert [(until, payload["event"]) for until, payload in sent] == [
        (_at(15, 35, day), "vstup"),
        (_at(17, 31, day), "TP1"),
        (_at(17, 45, day), "TP2"),
    ]
    for _, payload in sent:
        assert payload["kind"] == ALERT_KIND == "option_plan_state"
        assert (payload["symbol"], payload["plan_id"]) == ("QQQ", plan_id)
        assert payload["message"].startswith(f"Plán QQQ #{plan_id} · ")
        for value in ("1234", "1 234", "10000", "10 000"):  # parametry účtu do zprávy nejdou
            assert value not in payload["message"]
    row = h.repo.get(plan_id)
    assert row is not None
    assert [(a["event"], a["ts"], a["final"]) for a in row.live_alerts] == [
        ("vstup", _at(15, 35, day).isoformat(), False),
        ("TP1", _at(17, 31, day).isoformat(), False),
        ("TP2", _at(17, 45, day).isoformat(), True),
    ]
    assert row.live_alerts[0]["sent_at"] == (_at(15, 35, day) + dt.timedelta(seconds=1)).isoformat()
    assert row.result is None and row.evaluated_at is None  # výsledek dá až F3 po close


def test_restart_po_vstupu_bez_duplicity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path)
    plan_id = h.plan(main=hand())
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    clock = Clock(OPEN)
    first = minutes(watcher(h, clock), clock, DAY, OPEN + MINUTE, _at(14, 41))
    assert [payload["event"] for _, payload in first] == ["vstup"]
    # Restart enginu: nový hlídač, stav jen z PG
    again = minutes(watcher(h, clock), clock, DAY, _at(14, 40), _at(14, 46))
    assert [(until, payload["event"]) for until, payload in again] == [(_at(14, 45), "SL")]
    reads: list[dt.datetime] = []
    real = live.read_bars

    def counted(*args: Any) -> Any:
        reads.append(args[3])
        return real(*args)

    monkeypatch.setattr(live, "read_bars", counted)
    # Další restart po konečném přechodu: partice se nečtou, nic se nepošle
    assert minutes(watcher(h, clock), clock, DAY, _at(14, 47), _at(14, 50)) == []
    assert reads == []
    row = h.repo.get(plan_id)
    assert row is not None and [a["event"] for a in row.live_alerts] == ["vstup", "SL"]


def test_skutecny_restart_v_rth_da_mezeru_a_konec(tmp_path: Path) -> None:
    """Restart po vstupu: F1b nezapíše bar minuty startu odběru → MEZERA a dál nic (4a A).

    Vstup se znovu nepošle, SL v 9:45 ET už upozornění nemá; výsledek dá F3 po close.
    """
    h = Harness(tmp_path)
    plan_id = h.plan(main=hand())
    restart = _at(14, 41)  # bar minuty, ve které odběr po restartu začal, chybí
    h.bars([bar for bar in SL_PATH if bar.ts != restart])
    h.quotes(DAY, SL_PATH)
    clock = Clock(OPEN)
    before = minutes(watcher(h, clock), clock, DAY, OPEN + MINUTE, _at(14, 40))
    assert [payload["event"] for _, payload in before] == ["vstup"]
    after = minutes(watcher(h, clock), clock, DAY, _at(14, 42), _at(14, 50))
    assert [(until, payload["event"]) for until, payload in after] == [(_at(14, 42), "mezera")]
    assert after[0][1]["message"].startswith(
        f"Plán QQQ #{plan_id} · MEZERA: bary QQQ: chybí 1 z 12 minut"
    )
    row = h.repo.get(plan_id)
    assert row is not None
    assert [(a["event"], a["final"]) for a in row.live_alerts] == [
        ("vstup", False),
        ("mezera", True),
    ]


def test_zapis_prechodu_jednou(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan_id = h.plan(main=hand())
    alert = {"event": "vstup", "ts": _at(14, 40).isoformat(), "sent_at": "x", "final": False}
    assert h.repo.claim_live_alert(plan_id, alert) is True
    assert h.repo.claim_live_alert(plan_id, {**alert, "sent_at": "y"}) is False
    row = h.repo.get(plan_id)
    assert row is not None and row.live_alerts == (alert,)
    with pytest.raises(LookupError):
        h.repo.claim_live_alert(999, alert)


def test_prace_jen_nad_dnem_planu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bary od US openu seance plánu a kotace seance; plán jiného dne ani den bez plánu nic."""
    h = Harness(tmp_path)
    h.plan(day=dt.date(2026, 11, 16), main=hand(day="2026-11-16"))
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    bars_calls: list[tuple[Any, ...]] = []
    quote_calls: list[tuple[Any, ...]] = []
    real_bars, real_quotes = live.read_bars, live.read_option_quotes

    def bars_spy(*args: Any) -> Any:
        bars_calls.append(args)
        return real_bars(*args)

    def quotes_spy(*args: Any) -> Any:
        quote_calls.append(args)
        return real_quotes(*args)

    monkeypatch.setattr(live, "read_bars", bars_spy)
    monkeypatch.setattr(live, "read_option_quotes", quotes_spy)
    clock = Clock(_at(14, 41))
    w = watcher(h, clock)
    assert w.run(DAY, _at(14, 40)) == []  # 17. 11. bez plánu: PG ano, Parquet ne
    assert bars_calls == [] and quote_calls == []
    h.plan(main=hand())
    assert [p["event"] for p in w.run(DAY, _at(14, 40))] == ["vstup"]
    ((_, symbol, since, until),) = bars_calls
    assert (symbol, since, until) == ("QQQ", OPEN - MINUTE, _at(14, 40))
    assert [call[1:] for call in quote_calls] == [("QQQ", DAY)]


@pytest.mark.parametrize(
    ("session", "until"),
    [
        (dt.date(2026, 11, 21), _at(15, 0, dt.date(2026, 11, 21))),  # sobota
        (dt.date(2026, 11, 22), _at(15, 0, dt.date(2026, 11, 22))),  # neděle
        (dt.date(2026, 11, 26), _at(15, 0, dt.date(2026, 11, 26))),  # Díkůvzdání, NYSE zavřená
        (DAY, OPEN),  # před US openem (minuta 9:29 ET)
        (DAY, _at(21, 1)),  # po settle
        (DAY, _at(22, 30)),  # denní pauza Globexu 16:30 CT
        # nedělní otevření Globexu 17:00 CT: seance pondělí, RTH až v pondělí
        (dt.date(2026, 11, 23), dt.datetime(2026, 11, 22, 23, 1, tzinfo=UTC)),
    ],
)
def test_brany_mimo_seanci_nic(session: dt.date, until: dt.datetime, tmp_path: Path) -> None:
    h = Harness(tmp_path)
    w = watcher(h, Clock(until))

    async def scenario() -> None:
        w.on_minute(session, until)

    asyncio.run(scenario())
    assert w._task is None


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 11, 21, 15, 0, tzinfo=UTC),  # sobota
        dt.datetime(2026, 11, 17, 22, 30, tzinfo=UTC),  # denní pauza
    ],
)
def test_zavreny_trh_zadne_upozorneni(now: dt.datetime, tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan_id = h.plan(main=hand())
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    assert watcher(h, Clock(now)).run(DAY, _at(14, 40)) == []
    row = h.repo.get(plan_id)
    assert row is not None and row.live_alerts == ()


def test_on_minute_uloha_na_pozadi_a_preskok(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(tmp_path)
    h.plan(main=hand())
    h.bars(SL_PATH)
    h.quotes(DAY, SL_PATH)
    w = watcher(h, Clock(_at(14, 40) + dt.timedelta(seconds=1)))

    async def scenario() -> None:
        w.on_minute(DAY, _at(14, 40))
        task = w._task
        assert task is not None and not task.done()  # volající na kontrolu nečeká
        w.on_minute(DAY, _at(14, 40))  # předchozí běží → přeskok
        assert w._task is task
        await task

    with caplog.at_level(logging.INFO, logger=live.__name__):
        asyncio.run(scenario())
    [(channel, payload)] = h.publisher.sent
    assert channel == "alerts" and payload["event"] == "vstup"
    assert "předchozí kontrola běží" in caplog.text


def test_odber_ohlasi_zapsanou_minutu(tmp_path: Path) -> None:
    """F1b po zápisu minuty ohlásí seanci a závěr minuty; chyba živého stavu odběr nezastaví."""
    h = RecorderHarness(tmp_path, recorder_ts(14, 16, 10))
    h.add_plan(recorder_plan())
    calls: list[tuple[dt.date, dt.datetime]] = []

    def written(session: dt.date, until: dt.datetime) -> None:
        calls.append((session, until))

    h.recorder.on_minute = written
    h.tick(recorder_ts(14, 16, 10))
    h.feed(recorder_ts(14, 16, 30))
    h.tick(recorder_ts(14, 17, 0.05))
    assert calls == [(dt.date(2026, 11, 3), recorder_ts(14, 17))]

    def broken(session: dt.date, until: dt.datetime) -> None:
        raise RuntimeError("x")

    h.recorder.on_minute = broken
    h.tick(recorder_ts(14, 18, 0.05))
    assert len(h.quotes()) == 4  # minuta 14:17 se zapsala i se selháním živého stavu


def test_golden_expiry_a_kontrakty() -> None:
    """Pojistka fixture: golden kotace nesou kontrakty plánu (expirace 1DTE, puty 687/686)."""
    assert {(q.expiry, q.strike) for q in GOLDEN_QUOTES} == {
        (GOLDEN_EXPIRY.strftime("%Y%m%d"), 687.0),
        (GOLDEN_EXPIRY.strftime("%Y%m%d"), 686.0),
    }
    assert [v["strike"] for v in GOLDEN_PART["variants"]] == [687.0, 686.0]
