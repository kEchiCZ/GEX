"""Bluesky Jetstream kolektor (#578): filtr, normalizace, povodňová pojistka."""

import datetime as dt
from collections.abc import Sequence

from gexlens_news.collectors.bluesky import (
    MAX_PER_MINUTE,
    BlueskyStream,
    matches,
    normalize_post,
)
from gexlens_news.model import NewsEvent

NOW = dt.datetime(2026, 8, 27, 15, 0, tzinfo=dt.UTC)


def commit(text: str, *, did: str = "did:plc:abc", langs: list[str] | None = None) -> dict[str, object]:
    return {
        "kind": "commit",
        "did": did,
        "commit": {
            "operation": "create",
            "collection": "app.bsky.feed.post",
            "rkey": "3kabc",
            "record": {
                "text": text,
                "createdAt": "2026-08-27T14:59:30Z",
                "langs": langs if langs is not None else ["en"],
            },
        },
    }


class _Writer:
    def __init__(self) -> None:
        self.events: list[NewsEvent] = []

    def write(self, events: Sequence[NewsEvent]) -> int:
        self.events.extend(events)
        return len(events)


def test_matches_cashtag_klicova_slova_a_kurator() -> None:
    assert matches("NVDA beats: $NVDA up 5 % after hours", "did:x", frozenset())
    assert matches("FOMC minutes point to a rate cut in September", "did:x", frozenset())
    assert not matches("just had a great coffee", "did:x", frozenset())
    # Kurátorovaný autor projde s čímkoli (novinář — bere se celý výstup)
    assert matches("thread: what I'm hearing from the floor", "did:vip", frozenset({"did:vip"}))


def test_normalize_post_prevede_a_filtruje() -> None:
    event = normalize_post(commit("CPI came in hot, $SPY selling off"), NOW)
    assert event is not None
    assert event.source == "bluesky" and event.kind == "social"
    assert event.ts_event == dt.datetime(2026, 8, 27, 14, 59, 30, tzinfo=dt.UTC)
    assert event.source_uid == "at://did:plc:abc/app.bsky.feed.post/3kabc"
    # Neanglické posty mimo (klasifikátor je EN); delete/identity zprávy taky
    assert normalize_post(commit("čau trhy", langs=["cs"]), NOW) is None
    assert normalize_post({"kind": "identity"}, NOW) is None
    # Budoucí createdAt (rozbité hodiny klienta) nesmí předběhnout ingest
    future = commit("$SPY test")
    future["commit"]["record"]["createdAt"] = "2027-01-01T00:00:00Z"
    event2 = normalize_post(future, NOW)
    assert event2 is not None and event2.ts_event == NOW


def test_stream_filtruje_a_drzi_povodnovy_strop() -> None:
    writer = _Writer()
    stream = BlueskyStream(writer)
    stream._handle(b'{"kind":"identity"}')  # ne-post zprávy nepadají
    for index in range(MAX_PER_MINUTE + 5):
        stream._handle(__import__("json").dumps(commit(f"$SPY tick {index}")).encode())
    stream._handle(__import__("json").dumps(commit("nothing about markets")).encode())
    assert len(writer.events) == MAX_PER_MINUTE
    assert stream.flood_dropped == 5
    assert stream.matched == MAX_PER_MINUTE
