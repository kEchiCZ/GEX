"""Upozornění před releasem (#1296, ADR-0044) — text, etapy, dosah úrovní, zákaz směru."""

import datetime as dt
import re
from zoneinfo import ZoneInfo

import pytest

from gexlens_news.preopen import MESSAGE_BUDGET, SessionLevels, due_stages
from gexlens_news.release_hypotheses import STATUS_REJECTED, STATUS_TESTING, STATUS_VERIFIED
from gexlens_news.release_moves import SizeStats
from gexlens_news.release_preview import (
    PREVIEW_KIND,
    PREVIEW_LEADS,
    STAGE_T15,
    STAGE_T60,
    HypothesisView,
    build_preview,
    stages_to_send,
)
from gexlens_news.releases import FAMILY_OF_SERIES, ReleaseCluster, ReleaseEvent

PRAGUE = ZoneInfo("Europe/Prague")
UTC = dt.UTC
CPI_AT = dt.datetime(2026, 9, 11, 12, 30, tzinfo=UTC)  # 14:30 Praha
NFP_AT = dt.datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
CPI_STATS = SizeStats(
    n=23,
    mult_p50=0.401,
    mult_p75=0.485,
    raw_p50_bp=39.0,
    raw_p75_bp=56.0,
    vol_ref_median_bp=101.6,
)
H1_TESTING = HypothesisView(
    status=STATUS_TESTING,
    hits=0,
    n=0,
    wilson_lb=None,
    wilson_ub=None,
    historical_hits=13,
    historical_n=14,
)


def cluster(at: dt.datetime, *titles: str, forecast: str | None = None) -> ReleaseCluster:
    events = tuple(
        ReleaseEvent(
            id=index,
            ts=at,
            title=f"USD {title}",
            impact="high",
            forecast_text=forecast if index == 1 else None,
        )
        for index, title in enumerate(titles, 1)
    )
    return ReleaseCluster(ts=at, events=events)


CPI = cluster(CPI_AT, "Core CPI m/m", "CPI m/m", "Core CPI y/y", "CPI y/y", forecast="0.3%")


def levels(at: dt.datetime, **values: float) -> SessionLevels:
    return SessionLevels(
        ts=at,
        expiry=at.date(),
        call_wall=values.get("call_wall"),
        put_wall=values.get("put_wall"),
        flip=values.get("flip"),
        centroid=values.get("centroid"),
    )


ES_LEVELS = {"call_wall": 7810.0, "put_wall": 7735.0, "flip": 7785.0, "centroid": 7800.0}


def preview(
    now: dt.datetime,
    release: ReleaseCluster = CPI,
    *,
    symbol: str = "ES",
    price: float | None = 7805.5,
    session_levels: SessionLevels | None | str = "fresh",
    stats: SizeStats | None = CPI_STATS,
    history_n: int = 23,
    vol_now_bp: float | None = 95.4,
    h1: HypothesisView | None = H1_TESTING,
    m1: HypothesisView | None = None,
) -> str:
    """Text upozornění CPI × ES z příkladu v návrhu; parametry přepisují jednotlivé vstupy."""
    chosen = (
        levels(now - dt.timedelta(minutes=1), **ES_LEVELS)
        if session_levels == "fresh"
        else session_levels
    )
    assert not isinstance(chosen, str)
    payload = build_preview(
        symbol,
        release,
        now=now,
        tz=PRAGUE,
        price=price,
        levels=chosen,
        stats=stats,
        history_n=history_n,
        vol_now_bp=vol_now_bp,
        h1=h1,
        m1=m1,
    )
    return str(payload["message"])


def test_cpi_t60_es_golden() -> None:
    now = CPI_AT - dt.timedelta(minutes=60)
    payload = build_preview(
        "ES",
        CPI,
        now=now,
        tz=PRAGUE,
        price=7805.5,
        levels=levels(now, **ES_LEVELS),
        stats=CPI_STATS,
        history_n=23,
        vol_now_bp=95.4,
        h1=H1_TESTING,
        m1=None,
    )
    assert payload["message"] == "\n".join(
        [
            "CPI za 60 min (14:30) — ES 7805.5",
            "Vyjde: Core CPI m/m (odhad 0.3%), CPI m/m, Core CPI y/y, CPI y/y",
            "Očekávaná výchylka do 15 min: 30–36 bodů (38–46 bp)",
            "medián–p75 z 23 CPI · volatilita 0.94× obvyklé (bez přepočtu 39–56 bp)",
            "V dosahu 7769–7842: call zeď 7810 (+4.5) · těžiště 7800 (-5.5) · flip 7785 (-20.5)",
            "Dál: put zeď 7735 (-70.5)",
            "H1 – OVĚŘUJE SE: jádro inflace teplejší než odhad → historicky za 15 min níž "
            "ve 13 ze 14 (živě 0 z 0)",
            "Směr jinak bez prokazatelného efektu.",
        ]
    )
    assert payload["kind"] == PREVIEW_KIND
    assert payload["symbol"] == "ES"
    assert payload["ts_event"] == CPI_AT.isoformat()
    assert payload["event_ids"] == [1, 2, 3, 4]


def test_nfp_t15_nq_golden_bez_smeru() -> None:
    now = NFP_AT - dt.timedelta(minutes=15)
    nfp = cluster(
        NFP_AT,
        "Non-Farm Employment Change",
        "Unemployment Rate",
        "Average Hourly Earnings m/m",
        forecast="58K",
    )
    payload = build_preview(
        "NQ",
        nfp,
        now=now,
        tz=PRAGUE,
        price=30921.75,
        levels=levels(
            now,
            call_wall=30925.0,
            put_wall=30875.0,
            flip=30835.0,
            centroid=30900.0,
        ),
        stats=SizeStats(
            n=23,
            mult_p50=0.245,
            mult_p75=0.306,
            raw_p50_bp=45.0,
            raw_p75_bp=56.0,
            vol_ref_median_bp=165.3,
        ),
        history_n=23,
        vol_now_bp=147.1,
        h1=H1_TESTING,
        m1=None,
    )
    assert payload["message"] == "\n".join(
        [
            "NFP za 15 min (14:30) — NQ 30921.75",
            "Vyjde: Non-Farm Employment Change (odhad 58K), Unemployment Rate, "
            "Average Hourly Earnings m/m",
            "Očekávaná výchylka do 15 min: 111–139 bodů (36–45 bp)",
            "medián–p75 z 23 NFP · volatilita 0.89× obvyklé (bez přepočtu 45–56 bp)",
            "V dosahu 30783–31061: call zeď 30925 (+3.25) · těžiště 30900 (-21.75) · "
            "put zeď 30875 (-46.75) · flip 30835 (-86.75)",
            "Směr: bez prokazatelného efektu.",
        ]
    )


def test_etapy_t60_a_t15() -> None:
    def due(minutes_before: float, done: set[str] | None = None) -> list[str]:
        now = CPI_AT - dt.timedelta(minutes=minutes_before)
        return due_stages(now, CPI_AT, done or set(), PREVIEW_LEADS)

    assert due(61) == []
    assert due(60) == [STAGE_T60]
    assert due(15, {STAGE_T60}) == [STAGE_T15]
    assert due(14, {STAGE_T60, STAGE_T15}) == []
    assert due(0) == []
    # Restart v T−10 bez stavu: odejde jen T15, označí se obě
    assert stages_to_send(due(10)) == (STAGE_T15, [STAGE_T60, STAGE_T15])
    assert stages_to_send([]) == (None, [])


def test_chybejici_data_jsou_videt() -> None:
    now = CPI_AT - dt.timedelta(minutes=60)
    no_price = preview(now, price=None)
    assert "ES cena chybí" in no_price
    assert "Očekávaná výchylka do 15 min: 38–46 bp" in no_price.splitlines()
    assert "bod" not in no_price and "Úrovně ES: call zeď 7810" in no_price
    assert "Úrovně ES chybí" in preview(now, session_levels=None)
    no_vol = preview(now, vol_now_bp=None)
    assert "Očekávaná výchylka do 15 min: 30–44 bodů (39–56 bp)" in no_vol
    assert "medián–p75 z 23 CPI · bez přepočtu na dnešní volatilitu" in no_vol
    short = preview(now, stats=None, history_n=5)
    assert "málo historie (n = 5, potřeba aspoň 8)" in short
    assert "V dosahu" not in short
    stale = preview(now, session_levels=levels(now - dt.timedelta(minutes=40), **ES_LEVELS))
    assert "(úrovně z 12:50)" in stale  # T−60 = 13:30 Praha, úrovně o 40 min starší


def test_zadna_uroven_v_dosahu() -> None:
    now = CPI_AT - dt.timedelta(minutes=60)
    far = levels(now, call_wall=7900.0, put_wall=7700.0)
    message = preview(now, session_levels=far)
    assert "V dosahu 7769–7842: žádná úroveň" in message
    assert "Dál: call zeď 7900 (+94.5) · put zeď 7700 (-105.5)" in message


def test_radek_h1_podle_stavu() -> None:
    now = CPI_AT - dt.timedelta(minutes=60)
    verified = HypothesisView(STATUS_VERIFIED, 9, 10, 0.596, 0.982, 13, 14)
    assert (
        "H1 – ověřeno živě 9 z 10: jádro inflace teplejší než odhad → za 15 min níž "
        "s pravděpodobností 90 % [60–98 %]"
    ) in preview(now, h1=verified)
    rejected = HypothesisView(STATUS_REJECTED, 1, 10, 0.018, 0.404, 13, 14)
    message = preview(now, h1=rejected)
    assert "H1" not in message and message.endswith("Směr: bez prokazatelného efektu.")
    (testing,) = [line for line in preview(now).splitlines() if line.startswith("H1")]
    assert "OVĚŘUJE SE" in testing and "13 ze 14" in testing and "%" not in testing
    # PPI/PCE s headline jádra — H1 ano; CPI bez jádra v headline — ne
    assert "H1 – OVĚŘUJE SE" in preview(now, cluster(CPI_AT, "Core PPI m/m", "PPI m/m"))
    assert "H1 – OVĚŘUJE SE" in preview(now, cluster(CPI_AT, "Core PCE Price Index m/m"))
    assert "H1" not in preview(now, cluster(CPI_AT, "CPI m/m", "CPI y/y"))


M1_VERIFIED = HypothesisView(STATUS_VERIFIED, 10, 10, 0.72, 1.0, 107, 111)


def test_m1_dovetek_jen_po_overeni() -> None:
    now = CPI_AT - dt.timedelta(minutes=60)
    assert "Velikost nad běžným dnem ověřena živě 10 z 10 (M1)" in preview(now, m1=M1_VERIFIED)
    testing = HypothesisView(STATUS_TESTING, 3, 3, None, None, 107, 111)
    assert "M1" not in preview(now, m1=testing)


@pytest.mark.parametrize(
    ("titles", "label"),
    [
        (("Retail Sales m/m", "Core Retail Sales m/m"), "Retail Sales (slabší řada)"),
        (("ISM Services PMI",), "ISM Services (slabší řada)"),
    ],
)
def test_m1_dovetek_ne_u_slabsi_rady(titles: tuple[str, ...], label: str) -> None:
    """M1 slabší řady nehodnotí — ověřená M1 u nich nesmí tvrdit ověření velikosti."""
    now = CPI_AT - dt.timedelta(minutes=60)
    message = preview(now, cluster(CPI_AT, *titles), m1=M1_VERIFIED)
    assert message.startswith(label)
    assert "M1" not in message and "ověřena" not in message


def test_body_ve_spravnem_tvaru() -> None:
    """Rozsah bodů: tvar podle horní meze (1 bod, 2–4 body, 5+ bodů)."""
    now = CPI_AT - dt.timedelta(minutes=60)
    tiny = SizeStats(
        n=10, mult_p50=0.01, mult_p75=0.03, raw_p50_bp=1, raw_p75_bp=3, vol_ref_median_bp=100.0
    )
    message = preview(now, stats=tiny, vol_now_bp=100.0, price=10_000.0)
    assert "Očekávaná výchylka do 15 min: 1–3 body (1–3 bp)" in message


FORBIDDEN = re.compile(r"chladnější|výš|růst|LONG|SHORT|🟢|🔴", re.IGNORECASE)


@pytest.mark.parametrize("series", sorted(FAMILY_OF_SERIES))
def test_zadne_zakazane_smery(series: str) -> None:
    """Směr smí nést jen řádek H1 („níž“); opačný scénář ani jiné směry nikde."""
    now = CPI_AT - dt.timedelta(minutes=15)
    for h1 in (H1_TESTING, HypothesisView(STATUS_VERIFIED, 9, 10, 0.596, 0.982, 13, 14)):
        message = preview(now, cluster(CPI_AT, series), symbol="NQ", h1=h1)
        assert not FORBIDDEN.search(message), message
        for line in message.splitlines():
            if "níž" in line:
                assert line.startswith("H1 – "), line


def test_delka_pod_rozpoctem() -> None:
    now = CPI_AT - dt.timedelta(minutes=60)
    many = cluster(CPI_AT, *[f"Řada s dlouhým názvem číslo {k}" for k in range(40)])
    message = preview(now, many)
    assert len(message) <= MESSAGE_BUDGET
    assert "a další" in message
    assert len(preview(now)) <= MESSAGE_BUDGET


def test_vzdalenost_z_zobrazene_urovne() -> None:
    """Těžiště 7622,24 se ukáže jako 7622 a vzdálenost se počítá od 7622, ne od 7622,24."""
    now = CPI_AT - dt.timedelta(minutes=60)
    message = preview(now, price=7639.25, session_levels=levels(now, centroid=7622.24))
    assert "těžiště 7622 (-17.25)" in message
