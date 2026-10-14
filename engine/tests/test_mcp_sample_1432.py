"""Ukázka výstupu MCP nad REST (E-3.1, #1432) — nad uloženými odpověďmi API.

Odpovědi jsou zkrácený záznam produkčního API ze středy 14. 10. 2026 13:23 UTC
(`fixtures/mcp_sample_1432_api.json`, bez údajů účtu); živé API se nevolá.
Skript `scripts/mcp_sample_1432.py` se načítá přes importlib.
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_sample_1432_api.json"
#: Okamžik záznamu: středa 14. 10. 2026 8:23 CT, trh otevřený
RECORDED = dt.datetime(2026, 10, 14, 13, 23, 7, tzinfo=dt.UTC)
ENVELOPE = {"value", "unit", "as_of", "age_s", "market_open", "source", "scope"}


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mcp() -> Any:
    return _load_script("mcp_sample_1432")


@pytest.fixture
def responses() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))["responses"]
    return copy.deepcopy(data)


def _fetch(mcp: Any, responses: dict[str, Any]) -> Any:
    """Fetch nad záznamem: bary a tendence neuloženého dne = prázdný den, jinak 404."""

    def fetch(path: str) -> Any:
        if path in responses:
            return responses[path]
        if path.startswith("/bars/"):
            return {"bars": []}
        if path.startswith("/tendency/"):
            return {"tendency": []}
        raise mcp.ApiError(path, "HTTP 404")

    return fetch


def _values(result: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for symbol, tools in result["symbols"].items():
        for tool in tools.values():
            for key, value in tool["values"].items():
                yield f"{symbol}.{tool['tool']}.{key}", value
    for key, value in result["setup_library"]["values"].items():
        yield f"setup_library.{key}", value


def test_kazde_cislo_nese_obalku_a_obchodni_den(mcp: Any, responses: dict[str, Any]) -> None:
    result = mcp.sample(_fetch(mcp, responses), ["ES", "NQ"], mcp.make_clock(RECORDED))
    assert result["trading_session"] == "2026-10-14"
    assert result["market_open"] is True
    seen = 0
    for name, value in _values(result):
        assert value.keys() >= ENVELOPE, name
        if value["value"] is None:
            assert "reason" in value or "error" in value, name
        else:
            assert value["as_of"] is not None and value["age_s"] >= 0, name
        seen += 1
    assert seen > 40
    levels = result["symbols"]["ES"]["levels_now"]
    assert levels["data_session"] == "2026-10-14"
    assert levels["errors"] == []
    assert "20261014 (E2C, 0DTE)" in levels["values"]["call_wall"]["scope"]


def test_vzdalenost_pocita_cista_funkce(mcp: Any, responses: dict[str, Any]) -> None:
    assert mcp.level_distance(7870.0, 7823.0, None) == (47.0, None)
    assert mcp.level_distance(7790.0, 7823.0, 21.6) == (-33.0, -1.528)
    assert mcp.level_distance(None, 7823.0, 21.6) == (None, None)
    assert mcp.level_distance(7870.0, None, 21.6) == (None, None)
    assert mcp.level_distance(7870.0, 7823.0, 0.0) == (47.0, None)
    result = mcp.levels_now(
        _fetch(mcp, responses), "ES", mcp.make_clock(RECORDED), dt.date(2026, 10, 14)
    )
    values = result["values"]
    wall = values["call_wall"]
    assert wall["distance_pt"] == round(wall["value"] - values["spot"]["value"], 2)
    assert wall["distance_em"] is None and "E-3.5" in wall["distance_em_reason"]
    assert wall["prototype"].startswith("E-3.7")


def test_chybejici_hodnota_je_null_s_duvodem_a_omitted_s_ukolem(
    mcp: Any, responses: dict[str, Any]
) -> None:
    result = mcp.levels_now(
        _fetch(mcp, responses), "ES", mcp.make_clock(RECORDED), dt.date(2026, 10, 14)
    )
    flip = result["values"]["flip"]
    assert flip["value"] is None and "E-3.5" in flip["reason"] and flip["as_of"] is None
    omitted = {item["field"]: item["task"] for item in result["omitted"]}
    assert omitted["max_pain"] == "E-3.12b #1443"
    assert omitted["measured"].startswith("E-5.13")


def test_tendence_nekalibrovana_s_verzi_vah(mcp: Any, responses: dict[str, Any]) -> None:
    result = mcp.levels_now(
        _fetch(mcp, responses), "NQ", mcp.make_clock(RECORDED), dt.date(2026, 10, 14)
    )
    tendency = result["values"]["tendency"]
    assert tendency["calibrated"] is False
    assert tendency["weights_version"] == 4


def test_otevreny_trh_stare_minutove_hodnoty_jsou_chyba(
    mcp: Any, responses: dict[str, Any]
) -> None:
    """10 min bez nové minuty při otevřeném trhu = výpadek, ne poslední hodnota."""
    clock = mcp.make_clock(RECORDED + dt.timedelta(minutes=10))
    result = mcp.sample(_fetch(mcp, responses), ["ES"], clock)
    levels = result["symbols"]["ES"]["levels_now"]
    assert "call_wall" in levels["errors"] and "spot" in levels["errors"]
    wall = levels["values"]["call_wall"]
    assert wall["value"] is None and wall["error"]["code"] == "stale"
    # Denní řady stáří minut nehlídá — řádek včerejší seance je čerstvý
    assert result["symbols"]["ES"]["vol_context"]["errors"] == []


def test_zavreny_trh_vrati_posledni_seanci_bez_chyby(mcp: Any, responses: dict[str, Any]) -> None:
    """Sobota 17. 10.: poslední seance s bary (záznam 14. 10.), stáří bez chyby."""
    clock = mcp.make_clock(dt.datetime(2026, 10, 17, 10, 0, tzinfo=dt.UTC))
    assert clock.market_open is False
    result = mcp.sample(_fetch(mcp, responses), ["ES", "NQ"], clock)
    for symbol in ("ES", "NQ"):
        for tool in result["symbols"][symbol].values():
            assert tool["errors"] == [], (symbol, tool["tool"])
        levels = result["symbols"][symbol]["levels_now"]
        assert levels["data_session"] == "2026-10-14"
        assert levels["values"]["call_wall"]["value"] is not None
        assert levels["values"]["call_wall"]["age_s"] > 2 * 24 * 3600


def test_chyba_api_pri_zavrenem_trhu_hledani_ukonci(mcp: Any, responses: dict[str, Any]) -> None:
    def down(path: str) -> Any:
        raise mcp.ApiError(path, "ConnectError")

    clock = mcp.make_clock(dt.datetime(2026, 10, 17, 10, 0, tzinfo=dt.UTC))
    assert mcp.data_session(down, "ES", clock) == clock.trading_session
    spot = mcp.levels_now(down, "ES", clock, clock.trading_session)["values"]["spot"]
    assert spot["value"] is None and "ConnectError" in spot["reason"]


@pytest.mark.parametrize(
    ("now", "is_open", "session"),
    [
        # Denní pauza 16:30 CT: zavřeno, obchodní den ještě dnešní
        (dt.datetime(2026, 10, 14, 21, 30, tzinfo=dt.UTC), False, dt.date(2026, 10, 14)),
        # 17:30 CT: Globex otevřený, běží seance zítřka
        (dt.datetime(2026, 10, 14, 22, 30, tzinfo=dt.UTC), True, dt.date(2026, 10, 15)),
        # Neděle 18:00 CT: otevření týdne, seance pondělí
        (dt.datetime(2026, 10, 18, 23, 0, tzinfo=dt.UTC), True, dt.date(2026, 10, 19)),
    ],
)
def test_obchodni_den_ze_settle(
    mcp: Any, now: dt.datetime, is_open: bool, session: dt.date
) -> None:
    clock = mcp.make_clock(now)
    assert clock.market_open is is_open
    assert clock.trading_session == session


def test_svicka_nese_seanci_podle_globexu(mcp: Any) -> None:
    """Svíčka s UTC začátkem 12. 10. 22:00 je seance úterý 13. 10."""
    assert mcp._session_of_candle({"ts": "2026-10-12T22:00:00+00:00"}) == dt.date(2026, 10, 13)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # Středa ráno: poslední spočtená denní řada je úterní
        (RECORDED, dt.date(2026, 10, 13)),
        # Středa 20:40 UTC: po settle + rezervě už středa
        (dt.datetime(2026, 10, 14, 20, 40, tzinfo=dt.UTC), dt.date(2026, 10, 14)),
        # Pondělí ráno: pátek, ne neděle
        (dt.datetime(2026, 10, 19, 14, 0, tzinfo=dt.UTC), dt.date(2026, 10, 16)),
    ],
)
def test_ocekavana_denni_seance(mcp: Any, now: dt.datetime, expected: dt.date) -> None:
    assert mcp.expected_daily_session(mcp.make_clock(now)) == expected


def test_pondeli_denni_radek_ze_ctvrtka_je_zastaraly(mcp: Any, responses: dict[str, Any]) -> None:
    """Pondělí 19. 10. při otevřeném trhu: řádek čtvrtka 15. 10. ≠ pátek → chyba."""
    clock = mcp.make_clock(dt.datetime(2026, 10, 19, 14, 0, tzinfo=dt.UTC))
    responses["/volregime/ES?limit=1"]["rows"][0]["session_date"] = "2026-10-15"
    stale = mcp.vol_context(_fetch(mcp, responses), "ES", clock)["values"]["vol_regime"]
    assert stale["value"] is None and "2026-10-16" in stale["error"]["detail"]
    responses["/volregime/ES?limit=1"]["rows"][0]["session_date"] = "2026-10-16"
    fresh = mcp.vol_context(_fetch(mcp, responses), "ES", clock)["values"]["vol_regime"]
    assert fresh["value"] == "low" and "error" not in fresh


def test_prazdny_seznam_a_chyba_db_nejsou_nula(mcp: Any, responses: dict[str, Any]) -> None:
    responses["/volregime/ES?limit=1"] = {"symbol": "ES", "rows": []}
    responses["/tendency/ES?date=2026-10-14"]["tendency"] = []
    del responses["/setups/summary?symbols=ES,NQ"]
    fetch = _fetch(mcp, responses)
    clock = mcp.make_clock(RECORDED)
    session = dt.date(2026, 10, 14)
    vol = mcp.vol_context(fetch, "ES", clock)["values"]["vol_regime"]
    assert vol["value"] is None and "E-3.5" in vol["reason"]
    tendency = mcp.levels_now(fetch, "ES", clock, session)["values"]["tendency"]
    assert tendency["value"] is None and "E-3.5" in tendency["reason"]
    library = mcp.setup_library(fetch, ["ES", "NQ"], clock)["values"]["library"]
    assert library["value"] is None and "HTTP 404" in library["reason"]


def test_iv_rady_se_nemichaji_a_rank_bez_vzorku_ma_duvod(
    mcp: Any, responses: dict[str, Any]
) -> None:
    values = mcp.vol_context(_fetch(mcp, responses), "ES", mcp.make_clock(RECORDED))["values"]
    assert {"iv_ibkr", "iv_tasty", "iv_own_atm"} <= values.keys()
    assert "sample 0" in values["iv_tasty"]["scope"]
    own = values["iv_own_atm"]
    assert own["iv_rank"] is None and "ADR-0028" in own["iv_rank_reason"]


def test_hodnota_bez_duvodu_se_nevyda(mcp: Any) -> None:
    with pytest.raises(ValueError, match="bez důvodu"):
        mcp.measured(
            None,
            clock=mcp.make_clock(RECORDED),
            unit="bod",
            as_of=RECORDED,
            source="test",
            scope="test",
        )


def test_knihovna_bez_udaju_uctu(mcp: Any, responses: dict[str, Any]) -> None:
    """Buňky šablon ano, brzdy účtu jen opt-in (ot. 10, E-3.17)."""
    result = mcp.setup_library(_fetch(mcp, responses), ["ES", "NQ"], mcp.make_clock(RECORDED))
    assert "brakes" not in result["values"]
    assert {item["field"] for item in result["omitted"]} == {"brakes"}
    cell = result["values"]["ES:trend_continuation"]
    assert cell["value"] == "block" and cell["n_setups"] == 286
    assert cell["n_setups_needed"] == 458


def test_kazde_vedlejsi_cislo_ma_jednotku(mcp: Any, responses: dict[str, Any]) -> None:
    """Zkreslení 6: podíl 0–1 × %, body × násobky EM — jednotka u každého čísla."""
    result = mcp.sample(_fetch(mcp, responses), ["ES", "NQ"], mcp.make_clock(RECORDED))
    for name, value in _values(result):
        units = value.get("units", {})
        for key, item in value.items():
            if key in ENVELOPE or key == "age_s" or isinstance(item, bool):
                continue
            if isinstance(item, int | float) or key in mcp.FIELD_UNITS:
                assert key in units, (name, key)
    with pytest.raises(ValueError, match="bez jednotky"):
        mcp.measured(
            1.0,
            clock=mcp.make_clock(RECORDED),
            unit="bod",
            as_of=RECORDED,
            source="test",
            scope="test",
            unknown_share=0.5,
        )


def test_tendence_ve_vecerni_seanci_cte_oba_utc_dny(mcp: Any) -> None:
    """17:11 CT 14. 10.: seance 15. 10. začala ve 22:00 UTC dne 14. 10. — `/tendency?date=`
    filtruje UTC den, řádek seance je tedy v odpovědi na 14. 10., ne 15. 10."""
    row = {
        "ts_min": "2026-10-14T22:10:00+00:00",
        "score": 0.21,
        "band": "long",
        "weights_version": 4,
    }
    old = {
        "ts_min": "2026-10-14T13:21:00+00:00",
        "score": -0.5,
        "band": "short",
        "weights_version": 4,
    }
    responses = {
        "/tendency/ES?date=2026-10-14": {"tendency": [old, row]},
        "/tendency/ES?date=2026-10-15": {"tendency": []},
    }
    clock = mcp.make_clock(dt.datetime(2026, 10, 14, 22, 11, 30, tzinfo=dt.UTC))
    assert clock.market_open is True and clock.trading_session == dt.date(2026, 10, 15)
    tendency = mcp.tendency_now(_fetch(mcp, responses), "ES", clock, clock.trading_session)
    assert tendency["value"] == 0.21 and tendency["age_s"] == 90
    assert "date=2026-10-14" in tendency["source"] and "date=2026-10-15" in tendency["source"]


def test_session_context_hodnoty_a_konec_predchozi_seance(
    mcp: Any, responses: dict[str, Any]
) -> None:
    values = mcp.session_context(
        _fetch(mcp, responses), "ES", mcp.make_clock(RECORDED), dt.date(2026, 10, 14)
    )["values"]
    spot = values["spot"]["value"]
    assert values["session_high"]["distance_pt"] == round(values["session_high"]["value"] - spot, 2)
    prev = values["prev_session_close"]
    # Seance 13. 10. končí obchodováním v 16:00 CT = 21:00 UTC, ne otevřením další seance
    assert prev["as_of"] == "2026-10-13T21:00:00+00:00"
    assert "ne settlement" in prev["note"] and "error" not in prev


def test_candles_http_500_je_null_s_duvodem(mcp: Any, responses: dict[str, Any]) -> None:
    """Živé selhání 14. 10. 13:45 UTC (#1562): 500 na /candles není prázdná seance."""
    base = _fetch(mcp, responses)

    def fetch(path: str) -> Any:
        if path.startswith("/candles/"):
            raise mcp.ApiError(path, "HTTP 500")
        return base(path)

    values = mcp.session_context(fetch, "ES", mcp.make_clock(RECORDED), dt.date(2026, 10, 14))[
        "values"
    ]
    assert values["session"]["value"] is None and "HTTP 500" in values["session"]["reason"]
    assert values["prev_session"]["value"] is None
    assert values["spot"]["value"] is not None


def test_denni_pauza_vrati_dnesni_seanci_bez_chyby(mcp: Any, responses: dict[str, Any]) -> None:
    """16:30 CT: trh zavřený, obchodní den stále 14. 10. — poslední hodnoty, stáří bez chyby."""
    clock = mcp.make_clock(dt.datetime(2026, 10, 14, 21, 30, tzinfo=dt.UTC))
    assert clock.market_open is False
    result = mcp.sample(_fetch(mcp, responses), ["ES"], clock)
    for tool in result["symbols"]["ES"].values():
        assert tool["errors"] == [], tool["tool"]
    levels = result["symbols"]["ES"]["levels_now"]
    assert levels["data_session"] == "2026-10-14"
    assert levels["values"]["call_wall"]["age_s"] > mcp.MINUTE_LIMIT_S
