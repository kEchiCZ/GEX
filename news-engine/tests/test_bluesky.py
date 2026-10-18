"""Bluesky Jetstream kolektor (#578): filtr, normalizace, povodňová pojistka."""

import asyncio
import contextlib
import datetime as dt
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence

import pytest

from gexlens_news.collectors import bluesky as module
from gexlens_news.collectors.bluesky import (
    JETSTREAM_URL,
    MAX_PER_MINUTE,
    TITLE_MAX_CHARS,
    BlueskyStream,
    matches,
    normalize_post,
)
from gexlens_news.model import NewsEvent

NOW = dt.datetime(2026, 8, 27, 15, 0, tzinfo=dt.UTC)
US = 1_000_000


def commit(
    text: str,
    *,
    did: str = "did:plc:abc",
    langs: list[str] | None = None,
    time_us: int | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
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
    if time_us is not None:
        message["time_us"] = time_us
    return message


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
    record = future["commit"]["record"]  # type: ignore[index]
    record["createdAt"] = "2027-01-01T00:00:00Z"
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


def test_kurator_dostane_priznak_curated_ostatni_ne() -> None:
    """#1291: sociální zpráva je významná jen od kurátora — příznak nese `raw`."""
    writer = _Writer()
    stream = BlueskyStream(writer, curated_authors=["did:vip"])
    stream._handle(__import__("json").dumps(commit("thread from the floor", did="did:vip")))
    stream._handle(__import__("json").dumps(commit("$SPY ripping", did="did:crowd")))
    curated, crowd = writer.events
    assert curated.raw == {"did": "did:vip", "langs": ["en"], "curated": True}
    assert "curated" not in crowd.raw


def test_entity_v_postu_se_dekoduje_pred_orezem_titulku() -> None:
    """Boti přeposílající RSS píšou `S&amp;P` doslova (#1500)."""
    event = normalize_post(commit("MOC IMBALANCE S&amp;P 500: 1904.3 MLN $MACRO"), NOW)
    assert event is not None
    assert event.title == "MOC IMBALANCE S&P 500: 1904.3 MLN $MACRO"

    # Ořez titulku počítá znaky po dekódování — entita se nepřeřízne
    long_text = "S&amp;P " + "x" * TITLE_MAX_CHARS
    event2 = normalize_post(commit(long_text), NOW)
    assert event2 is not None
    assert event2.title.startswith("S&P x")
    assert event2.body == "S&P " + "x" * TITLE_MAX_CHARS

    # Holý `&` v odkazu není entita — `&region=US` nesmí být `®ion=US`
    link = "CPI preview $SPY https://x.com/p?date=2026-09-23&region=US&utm_source=social"
    event3 = normalize_post(commit(link), NOW)
    assert event3 is not None
    assert event3.title == link


# ── Reconnect s cursorem (#1509) ─────────────────────────


def _ago_us(seconds: float) -> int:
    """`time_us` Jetstreamu před `seconds` s (záporné = až po připojení)."""
    return time.time_ns() // 1_000 - int(seconds * US)


class _FakeSocket:
    def __init__(self, frames: list[dict[str, object]], *, stop: asyncio.Event | None) -> None:
        self._frames = frames
        self._stop = stop

    async def recv(self) -> str:
        if self._frames:
            return json.dumps(self._frames.pop(0))
        if self._stop is not None:
            self._stop.set()
        raise ConnectionError("spojení spadlo")


class _FakeJetstream:
    """Skriptovaný `websockets.connect`: každé připojení odehraje jednu session.

    Session `None` = připojení selže, jinak socket vydá své rámce a spadne.
    Poslední session nastaví stop, takže `run` doběhne.
    """

    def __init__(self, sessions: list[list[dict[str, object]] | None], stop: asyncio.Event) -> None:
        self._sessions = sessions
        self._stop = stop
        self.urls: list[str] = []

    @contextlib.asynccontextmanager
    async def connect(self, url: str, **_: object) -> AsyncIterator[_FakeSocket]:
        self.urls.append(url)
        frames = self._sessions.pop(0)
        last = not self._sessions
        if frames is None:
            if last:
                self._stop.set()
            raise OSError("connection refused")
        yield _FakeSocket(frames, stop=self._stop if last else None)


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    stream: BlueskyStream,
    sessions: list[list[dict[str, object]] | None],
) -> list[str]:
    """Pustí stream proti skriptovanému Jetstreamu bez reálného backoffu; vrací URL."""
    monkeypatch.setattr(module, "RECONNECT_BASE_S", 0.001)
    monkeypatch.setattr(module, "RECONNECT_MAX_S", 0.004)
    stop = asyncio.Event()
    fake = _FakeJetstream(sessions, stop)
    monkeypatch.setattr("websockets.connect", fake.connect)
    await asyncio.wait_for(stream.run(stop), timeout=5.0)
    return fake.urls


async def test_reconnect_s_cursorem_bez_duplicit_na_hranici(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """První připojení živě, reconnect s `cursor` = `time_us` poslední zprávy."""
    times = [_ago_us(300 - index) for index in range(5)]
    posts = [commit(f"$SPY post {index}", time_us=times[index]) for index in range(5)]
    writer = _Writer()
    stream = BlueskyStream(writer)

    urls = await _run(monkeypatch, stream, [posts[:3], posts[2:]])

    assert urls == [JETSTREAM_URL, f"{JETSTREAM_URL}&cursor={times[2]}"]
    # Přehrání je inkluzivní: post 2 přišel podruhé, zapsal se jednou
    assert [event.title for event in writer.events] == [f"$SPY post {i}" for i in range(5)]
    assert stream.seen == 5


async def test_backoff_se_po_funkcnim_spojeni_resetuje(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pád spojení, které doručilo data, = reconnect za základ; selhání dál zdvojují."""
    caplog.set_level(logging.WARNING, logger=module.__name__)
    post = commit("$SPY tick", time_us=_ago_us(10))

    await _run(monkeypatch, BlueskyStream(_Writer()), [None, None, [post], None, []])

    waits = [
        record.args[-1]
        for record in caplog.records
        if "reconnect za" in record.msg and isinstance(record.args, tuple)
    ]
    # Session [] se připojila, ale nic nedoručila — není to fungující spojení
    assert waits == [0.001, 0.002, 0.001, 0.002, 0.004]


def test_povodnova_pojistka_pocita_minuty_podle_casu_zpravy() -> None:
    """Přehrání 60 postů ze 3 původních minut projde celé, i když dorazí naráz.

    Živá povodeň v jedné minutě Jetstreamu se dál ořízne na MAX_PER_MINUTE.
    """
    minute0 = (time.time_ns() // 1_000 // (60 * US) - 10) * 60 * US
    writer = _Writer()
    stream = BlueskyStream(writer)
    for index in range(60):
        at = minute0 + (index // 20) * 60 * US + (index % 20) * US
        stream._handle(json.dumps(commit(f"$SPY replay {index}", time_us=at)))
    assert len(writer.events) == 60
    assert stream.flood_dropped == 0

    live = minute0 + 5 * 60 * US
    for index in range(MAX_PER_MINUTE + 5):
        stream._handle(json.dumps(commit(f"$SPY live {index}", time_us=live + index)))
    assert len(writer.events) == 60 + MAX_PER_MINUTE
    assert stream.flood_dropped == 5


async def test_log_prehrani_nedokonceneho_i_dohnaneho(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log reconnectu: výpadek, pak počty přehraných, prošlých filtrem a zapsaných."""
    caplog.set_level(logging.INFO, logger=module.__name__)
    before = commit("$SPY before outage", time_us=_ago_us(300))
    identity: dict[str, object] = {"kind": "identity", "did": "did:x", "time_us": _ago_us(190)}
    interrupted = [before, commit("$QQQ during outage", time_us=_ago_us(200)), identity]
    caught_up = [
        identity,  # hranice cursoru po druhém pádu
        commit("just had a great coffee", time_us=_ago_us(180)),
        commit("FOMC during outage", time_us=_ago_us(170)),
        commit("$NVDA beats during outage", time_us=_ago_us(160)),
        commit("$SPY live again", time_us=_ago_us(-60)),  # po připojení = živý proud
    ]
    writer = _Writer()

    await _run(monkeypatch, BlueskyStream(writer), [[before], interrupted, caught_up])

    messages = [record.getMessage() for record in caplog.records]
    assert any("s cursorem — výpadek 300 s" in message for message in messages)
    assert any(
        "přehrání nedokončeno" in message
        and "přehráno 2 zpráv, filtrem prošlo 1, zapsáno 1" in message
        for message in messages
    )
    assert any(
        "přehrání dohnáno" in message and "přehráno 3 zpráv, filtrem prošlo 2, zapsáno 2" in message
        for message in messages
    )
    assert [event.title for event in writer.events] == [
        "$SPY before outage",
        "$QQQ during outage",
        "FOMC during outage",
        "$NVDA beats during outage",
        "$SPY live again",
    ]
