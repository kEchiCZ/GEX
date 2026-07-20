"""Test throttle logiky živého spotu (#128) — bez ib_async, deterministický čas."""

from gexlens_engine.runtime import PublisherLike
from gexlens_engine.spot_stream import SpotStreamer


class _NullPublisher(PublisherLike):
    async def status(self, **fields: object) -> None: ...
    async def publish(self, channel: str, data: dict[str, object]) -> None: ...


def test_spot_streamer_throttles_filters_nan_and_stops() -> None:
    streamer = SpotStreamer(_NullPublisher(), "ES", min_interval_s=0.2)

    assert streamer.sample(29000.0, 0.0) == 29000.0  # první tick projde
    assert streamer.sample(29001.0, 0.1) is None  # < 0.2 s → throttle
    assert streamer.sample(29002.0, 0.25) == 29002.0  # ≥ 0.2 s → projde
    assert streamer.sample(float("nan"), 0.6) is None  # NaN → zahozeno
    assert streamer.sample(29003.0, 0.9) == 29003.0

    streamer.stop()
    assert streamer.sample(29004.0, 2.0) is None  # po stopu už nic
