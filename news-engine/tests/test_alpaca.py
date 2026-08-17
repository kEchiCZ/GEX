"""Testy Alpaca news WS collectoru (#387): normalizace a zpracování rámců."""

import asyncio
import datetime as dt
import json
from collections.abc import Sequence

import pytest

from gexlens_news.collectors.alpaca import AlpacaNewsStream, normalize_message
from gexlens_news.model import NewsEvent

NOW = dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.UTC)


def news_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "T": "n",
        "id": 42,
        "headline": "Fed signals patience",
        "summary": "  ",
        "author": "Benzinga",
        "created_at": "2026-07-30T13:59:30+00:00",
        "url": "https://example.com/x",
        "symbols": ["SPY", "QQQ"],
        "source": "benzinga",
    }
    payload.update(overrides)
    return payload


def test_normalize_message_maps_news_frame() -> None:
    event = normalize_message(news_payload(), NOW)
    assert event is not None
    assert event.source == "alpaca"
    assert event.kind == "headline"
    assert event.title == "Fed signals patience"
    assert event.summary is None  # prázdný summary → None
    assert event.source_uid == "42"
    assert event.symbols == ["SPY", "QQQ"]
    assert event.ts_event == dt.datetime(2026, 7, 30, 13, 59, 30, tzinfo=dt.UTC)
    assert event.ts_ingested == NOW
    # Tokeny/klíče do raw nepatří (S10) — jen neškodná metadata
    assert set(event.raw) == {"benzinga_url", "author"}


def test_normalize_message_skips_control_and_broken_frames() -> None:
    assert normalize_message({"T": "success", "msg": "authenticated"}, NOW) is None
    assert normalize_message(news_payload(headline=""), NOW) is None
    # Vadné datum → ts_ingested (event se neztratí kvůli razítku)
    event = normalize_message(news_payload(created_at="not-a-date"), NOW)
    assert event is not None
    assert event.ts_event == NOW


class RecordingWriter:
    def __init__(self) -> None:
        self.batches: list[Sequence[NewsEvent]] = []

    def write(self, events: Sequence[NewsEvent]) -> int:
        self.batches.append(events)
        return len(events)


def test_handle_writes_news_and_raises_on_error_frame() -> None:
    writer = RecordingWriter()
    stream = AlpacaNewsStream("key", "secret", writer)
    asyncio.run(
        stream._handle('[{"T":"success","msg":"connected"}, ' + json.dumps(news_payload()) + "]")
    )
    assert len(writer.batches) == 1
    assert writer.batches[0][0].title == "Fed signals patience"

    # Error rámec (typicky špatné klíče) musí session shodit → reconnect + log
    with pytest.raises(RuntimeError, match="auth failed"):
        asyncio.run(stream._handle('[{"T":"error","msg":"auth failed"}]'))


# ── Plné znění článku (#743) ───────────────────────────────────────


def test_content_se_uklada_jako_body_bez_html() -> None:
    """Alpaca posílá plný článek v `content` — dřív se zahazoval."""
    payload = {
        "T": "n",
        "id": 1,
        "headline": "NVIDIA beats estimates",
        "summary": "Krátký perex",
        "content": "<p>Plný <b>text</b> článku.</p><script>track()</script>",
        "created_at": "2026-08-17T12:00:00Z",
        "symbols": ["NVDA"],
    }

    event = normalize_message(payload, NOW)

    assert event is not None
    assert event.body == "Plný text článku."  # bez značek i bez skriptu
    assert event.summary == "Krátký perex"  # perex zůstává vlastním polem


def test_bez_content_zustane_body_prazdne() -> None:
    """Plný text má jen ~30 % zpráv; zbytek se musí chovat jako dřív."""
    payload = {
        "T": "n",
        "id": 2,
        "headline": "Zpráva bez těla",
        "created_at": "2026-08-17T12:00:00Z",
    }

    event = normalize_message(payload, NOW)

    assert event is not None
    assert event.body is None
