"""Pásmové metriky (#575 fáze 1): ostrost obou variant, hloubka, krajní stavy."""

import datetime as dt

from gexlens_engine.compute.bandregime import band_context, band_metrics
from gexlens_engine.compute.gexfield import GexProfile, price_weight_per_percent

TS = dt.datetime(2026, 8, 13, 14, 0, tzinfo=dt.UTC)


def profile_from_weighted(weighted: list[float], grid_start: float, step: float) -> GexProfile:
    """Profil, jehož VÁŽENÁ podoba ($/1 %) je přesně `weighted` — testy tak
    píšou očekávání v jednotce, ve které metrika měří."""
    raw = [
        value / price_weight_per_percent(grid_start + i * step) for i, value in enumerate(weighted)
    ]
    return GexProfile(ts_min=TS, grid_start=grid_start, grid_step=step, values=tuple(raw))


# Lichoběžníková zóna: náběh 0→100 přes dva kroky, plato, sráz 100→0 přes JEDEN
# krok — pravá hrana je ostřejší než levá. Mřížka po 10 bodech od 7000.
#   index:    0    1    2     3     4     5     6    7
#   vážené:   0   50  100   100   100   100     0    0
ZONE = [0.0, 50.0, 100.0, 100.0, 100.0, 100.0, 0.0, 0.0]


def test_hloubka_na_hranach_a_uvnitr() -> None:
    profile = profile_from_weighted(ZONE, 7000.0, 10.0)
    # Uprostřed plata (7040): hodnota 100 = nad Major → depth +1
    assert band_metrics(profile, 7040.0).depth == 1.0  # type: ignore[union-attr]
    # Na úrovni All (40 % ze 100 = 40): náběh 0→50 protíná 40 v 7008 → depth 0
    metrics = band_metrics(profile, 7008.0)
    assert metrics is not None
    assert abs(metrics.depth) < 0.01
    # Pod zónou (7004: hodnota 20 < All 40) → depth záporná
    metrics_below = band_metrics(profile, 7004.0)
    assert metrics_below is not None
    assert metrics_below.depth < 0


def test_ostrost_meri_nejblizsi_hranu_a_obe_normalizace() -> None:
    profile = profile_from_weighted(ZONE, 7000.0, 10.0)
    # Cena u PRAVÉ (ostré) hrany: sráz 100→0 na jednom kroku (10 b).
    # All (40) kříží v 7056, Major (65) v 7053,5 → spread 2,5 b.
    metrics = band_metrics(profile, 7050.0)
    assert metrics is not None
    # Šířka zóny: All kříží v 7008 (náběh) a 7056 (sráz) → 48 b
    assert abs(metrics.sharpness - 2.5 / 48.0) < 0.01  # varianta A
    assert abs(metrics.sharpness_pct - 2.5 / 7050.0 * 100.0) < 0.005  # varianta B

    # Cena u LEVÉ (pozvolné) hrany: náběh 0→50→100; All v 7008, Major v 7013
    # → spread 5 b — levá hrana je 2× měkčí než pravá
    metrics_left = band_metrics(profile, 7015.0)
    assert metrics_left is not None
    assert abs(metrics_left.sharpness - 5.0 / 48.0) < 0.01
    assert metrics_left.sharpness > metrics.sharpness  # měkčí > ostřejší


def test_krajni_stavy_vraci_none_nebo_prazdno() -> None:
    profile = profile_from_weighted(ZONE, 7000.0, 10.0)
    # Cena mimo mřížku
    assert band_metrics(profile, 6900.0) is None
    # Profil bez kladné části (čistě negativní gamma) — žádná tlumící zóna
    negative = profile_from_weighted([-10.0] * 8, 7000.0, 10.0)
    assert band_metrics(negative, 7040.0) is None
    # Zóna sahá na kraj mřížky → hrana neurčitelná (konvence #601: neměřit)
    edge = profile_from_weighted([100.0] * 8, 7000.0, 10.0)
    assert band_metrics(edge, 7040.0) is None
    # band_context: None profil → prázdný dict (setup bez klíčů, žádné lhaní)
    assert band_context(None, 7040.0) == {}
    keys = band_context(profile, 7040.0)
    assert set(keys) == {"band_sharpness", "band_sharpness_pct", "band_depth"}
