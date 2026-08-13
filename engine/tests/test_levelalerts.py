"""Testy cenových alertů na úrovně (#675): práh, cooldown, re-arm hystereze."""

from gexlens_engine.compute.levelalerts import (
    LevelProximityWatcher,
    ProximityAlert,
    strike_step,
)
from gexlens_engine.compute.levels import GexLevels


def make_levels(
    flip: float | None = None,
    call_wall: float | None = None,
    put_wall: float | None = None,
) -> GexLevels:
    return GexLevels(
        flip=flip, call_wall=call_wall, put_wall=put_wall, centroid=None, total_gex=0.0
    )


def test_strike_step_basic() -> None:
    assert strike_step([4900.0, 4905.0, 4910.0]) == 5.0


def test_strike_step_duplicates_and_uneven() -> None:
    # Duplicitní striky (C i P na stejném striku) nesmí dát krok 0
    assert strike_step([4900.0, 4900.0, 4910.0, 4935.0]) == 10.0


def test_strike_step_degenerate() -> None:
    assert strike_step([4900.0]) == 0.0
    assert strike_step([]) == 0.0


def test_fires_on_entering_zone() -> None:
    watcher = LevelProximityWatcher(near_points=5.0, cooldown_s=900.0)
    fired = watcher.observe(make_levels(call_wall=5000.0), spot=4996.0, now=0.0)
    assert fired == [
        ProximityAlert(
            level_name="call_wall", label="call wall", level=5000.0, price=4996.0, distance=4.0
        )
    ]


def test_no_repeat_while_inside_zone() -> None:
    watcher = LevelProximityWatcher(near_points=5.0, cooldown_s=60.0)
    assert watcher.observe(make_levels(flip=5000.0), spot=4997.0, now=0.0)
    # Konsolidace u úrovně: další minuty v zóně mlčí i po uplynutí cooldownu
    for minute in range(1, 10):
        assert watcher.observe(make_levels(flip=5000.0), spot=4998.0, now=minute * 60.0) == []


def test_rearm_requires_leaving_zone_and_cooldown() -> None:
    watcher = LevelProximityWatcher(near_points=5.0, cooldown_s=600.0, rearm_ratio=2.0)
    assert watcher.observe(make_levels(put_wall=4900.0), spot=4903.0, now=0.0)
    # Odchod jen kousek za práh (≤ 2× near) neodjistí
    assert watcher.observe(make_levels(put_wall=4900.0), spot=4908.0, now=60.0) == []
    assert watcher.observe(make_levels(put_wall=4900.0), spot=4904.0, now=120.0) == []
    # Odchod za re-arm hranici odjistí, ale cooldown ještě běží
    assert watcher.observe(make_levels(put_wall=4900.0), spot=4915.0, now=180.0) == []
    assert watcher.observe(make_levels(put_wall=4900.0), spot=4903.0, now=240.0) == []
    # Po cooldownu a novém vstupu do zóny vystřelí znovu
    assert watcher.observe(make_levels(put_wall=4900.0), spot=4915.0, now=300.0) == []
    fired = watcher.observe(make_levels(put_wall=4900.0), spot=4902.0, now=700.0)
    assert len(fired) == 1 and fired[0].level_name == "put_wall"


def test_missing_level_is_ignored() -> None:
    watcher = LevelProximityWatcher(near_points=5.0, cooldown_s=60.0)
    assert watcher.observe(make_levels(), spot=5000.0, now=0.0) == []


def test_multiple_levels_fire_independently() -> None:
    watcher = LevelProximityWatcher(near_points=5.0, cooldown_s=60.0)
    fired = watcher.observe(make_levels(flip=5000.0, call_wall=5003.0), spot=5001.0, now=0.0)
    assert {alert.level_name for alert in fired} == {"flip", "call_wall"}


def test_zero_threshold_disables() -> None:
    watcher = LevelProximityWatcher(near_points=0.0, cooldown_s=60.0)
    assert watcher.observe(make_levels(flip=5000.0), spot=5000.0, now=0.0) == []
