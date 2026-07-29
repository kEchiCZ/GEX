"""Tier C crowd sentiment collectory (#290, SPEC 2.6 + 5.8, ADR-0014).

Kontinuální řady nálady davu — **nevstupují do SentIndexu** (kontrariánská
povaha; vlna WSB postů by index utopila víc než CPI). Ukládají se do
`crowd_sentiment` a zobrazují jako doplňkový pohled v News (SPEC 9.3).

Tři zdroje:

* **CNN Fear & Greed** — neoficiální endpoint (ADR-0014: holý request 418,
  browser hlavičky 200). Fetch přes curl_cffi s Chrome impersonací — stejný
  TLS blok jako u FF kalendáře (#277) hrozí i tady. Payload nese ~roční
  denní historii → backfill je zadarmo při každém fetchi (PK dedup).
* **Reddit** — OAuth client_credentials (bez klíčů se zdroj nespouští, S10);
  hot posty r/wallstreetbets a r/stocks, jen titulky + skóre.
* **PCR z GEXLens** — poměr put/call volume z vlastních snapshot partic
  aktivní expirace; odvozená řada, žádný nový externí zdroj.
"""

import datetime as dt
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import crowd_sentiment
from gexlens_news.http import BROWSER_UA

logger = logging.getLogger(__name__)

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
# Bez Origin/Referer endpoint vrací 418 „You're a bot" (ADR-0014)
CNN_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Origin": "https://edition.cnn.com",
    "Referer": "https://edition.cnn.com/",
}

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SUBREDDITS = {"wallstreetbets": "wsb_hot_avg", "stocks": "stocks_hot_avg"}
# Deskriptivní UA — Reddit holé klienty blokuje (ADR-0014)
REDDIT_UA = "gexlens-sentiment/0.1 (crowd collector)"
REDDIT_HOT_LIMIT = 25

# Kolik titulků se schová do raw pro zobrazení v UI (SPEC „jen titulky + skóre")
RAW_TITLES_LIMIT = 5


@dataclass(frozen=True)
class CrowdPoint:
    """Jeden bod řady `crowd_sentiment` (PK: ts+source+metric+symbol)."""

    ts: dt.datetime
    source: str
    metric: str
    value: float
    symbol: str = ""
    raw: dict[str, Any] | None = None


class CrowdWriter:
    """Idempotentní zápis bodů (ON CONFLICT DO NOTHING nad PK, RETURNING #367)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write(self, points: Sequence[CrowdPoint]) -> int:
        if not points:
            return 0
        insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        written = 0
        with self._engine.begin() as conn:
            for point in points:
                stmt = (
                    insert(crowd_sentiment)
                    .values(
                        ts=point.ts,
                        source=point.source,
                        metric=point.metric,
                        symbol=point.symbol,
                        value=point.value,
                        raw=point.raw,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            crowd_sentiment.c.ts,
                            crowd_sentiment.c.source,
                            crowd_sentiment.c.metric,
                            crowd_sentiment.c.symbol,
                        ]
                    )
                    .returning(crowd_sentiment.c.ts)
                )
                if conn.execute(stmt).first() is not None:
                    written += 1
        return written


# ── CNN Fear & Greed ───────────────────────────────────────────────


def _epoch_ms(value: object) -> dt.datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return dt.datetime.fromtimestamp(float(value) / 1000.0, tz=dt.UTC)


def parse_cnn_payload(payload: dict[str, Any]) -> list[CrowdPoint]:
    """Body z F&G payloadu: aktuální score + denní historie všech (sub)indexů.

    Formát je negarantovaný — nečitelná sekce se přeskočí, nikdy pád
    (SPEC 3.2). Historie je v payloadu vždy celá (~250 denních bodů), dedup
    řeší PK v DB, takže každý fetch je zároveň backfill.
    """
    points: list[CrowdPoint] = []

    current = payload.get("fear_and_greed")
    if isinstance(current, dict):
        score = current.get("score")
        ts = None
        try:
            ts = dt.datetime.fromisoformat(str(current.get("timestamp")))
        except ValueError:
            logger.debug("F&G bez čitelného timestampu — aktuální bod se přeskakuje")
        if isinstance(score, (int, float)) and ts is not None:
            points.append(
                CrowdPoint(
                    ts=ts.astimezone(dt.UTC),
                    source="cnn_fg",
                    metric="score",
                    value=float(score),
                    raw={"rating": current.get("rating")},
                )
            )

    for key, section in payload.items():
        if key == "fear_and_greed" or not isinstance(section, dict):
            continue
        metric = "score" if key == "fear_and_greed_historical" else key
        if len(metric) > 32:
            logger.debug("Metrika %r přes limit sloupce — přeskakuji", metric)
            continue
        data = section.get("data")
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            ts = _epoch_ms(entry.get("x"))
            value = entry.get("y")
            if ts is None or not isinstance(value, (int, float)):
                continue
            points.append(
                CrowdPoint(
                    ts=ts,
                    source="cnn_fg",
                    metric=metric,
                    value=float(value),
                    raw={"rating": entry.get("rating")},
                )
            )
    return points


def fetch_cnn_payload(*, timeout_s: float = 30.0) -> dict[str, Any]:
    """Fetch přes curl_cffi — browser TLS fingerprint (lekce z #277)."""
    from curl_cffi import requests as cffi_requests

    response = cffi_requests.get(
        CNN_URL, headers=CNN_HEADERS, impersonate="chrome", timeout=timeout_s
    )
    response.raise_for_status()  # type: ignore[no-untyped-call]
    payload = json.loads(response.text)
    if not isinstance(payload, dict):
        raise ValueError("F&G payload není objekt")
    return payload


class CnnFearGreedCollector:
    """À 1 h (konfig); `fetch` injektovatelné kvůli testům."""

    name = "cnn_fg"

    def __init__(
        self,
        *,
        interval_s: float,
        fetch: Callable[[], dict[str, Any]] = fetch_cnn_payload,
    ) -> None:
        self.interval_s = interval_s
        self._fetch = fetch

    def collect(self, now: dt.datetime) -> list[CrowdPoint]:
        return parse_cnn_payload(self._fetch())


# ── Reddit ─────────────────────────────────────────────────────────


def parse_reddit_listing(
    payload: dict[str, Any], *, metric: str, now: dt.datetime
) -> list[CrowdPoint]:
    """Hot listing → průměrné skóre top postů + titulky v raw (SPEC Tier C)."""
    children = payload.get("data", {}).get("children")
    if not isinstance(children, list):
        return []
    posts: list[dict[str, Any]] = []
    for child in children:
        data = child.get("data") if isinstance(child, dict) else None
        if not isinstance(data, dict):
            continue
        score = data.get("score")
        if isinstance(score, (int, float)):
            posts.append({"title": str(data.get("title") or ""), "score": float(score)})
    if not posts:
        return []
    average = sum(post["score"] for post in posts) / len(posts)
    top = sorted(posts, key=lambda post: -post["score"])[:RAW_TITLES_LIMIT]
    return [
        CrowdPoint(
            ts=now.replace(second=0, microsecond=0),
            source="reddit",
            metric=metric,
            value=average,
            raw={"posts": len(posts), "top": top},
        )
    ]


class RedditCollector:
    """Hot posty přes application-only OAuth (client_credentials, ADR-0014).

    Token se cachuje do expirace; selhání fetchi jedné subreddity nezahodí
    druhou. Bez credentials se collector vůbec nezakládá (S10).
    """

    name = "reddit"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        interval_s: float,
        http: httpx.Client | None = None,
    ) -> None:
        self.interval_s = interval_s
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=20.0)
        self._token: str | None = None
        self._token_expires: dt.datetime | None = None

    def _access_token(self, now: dt.datetime) -> str:
        if (
            self._token is not None
            and self._token_expires is not None
            and now < self._token_expires
        ):
            return self._token
        response = self._http.post(
            REDDIT_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            headers={"User-Agent": REDDIT_UA},
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload["access_token"])
        expires_s = float(payload.get("expires_in", 3600))
        self._token = token
        # Rezerva minutu před expirací — token nesmí umřít uprostřed fetche
        self._token_expires = now + dt.timedelta(seconds=max(expires_s - 60, 60))
        return token

    def collect(self, now: dt.datetime) -> list[CrowdPoint]:
        token = self._access_token(now)
        points: list[CrowdPoint] = []
        for subreddit, metric in REDDIT_SUBREDDITS.items():
            try:
                response = self._http.get(
                    f"https://oauth.reddit.com/r/{subreddit}/hot",
                    params={"limit": REDDIT_HOT_LIMIT},
                    headers={"Authorization": f"Bearer {token}", "User-Agent": REDDIT_UA},
                )
                response.raise_for_status()
                points.extend(parse_reddit_listing(response.json(), metric=metric, now=now))
            except Exception:
                logger.exception("Reddit r/%s selhal — pokračuji další", subreddit)
        return points


# ── PCR z vlastních dat GEXLens ────────────────────────────────────


def compute_pcr(rows: Sequence[dict[str, Any]]) -> tuple[float, float, float] | None:
    """(pcr, call_volume, put_volume) z řádků jedné minuty; None = nespočitatelné."""
    call_volume = sum(float(r.get("volume") or 0) for r in rows if r.get("right") == "C")
    put_volume = sum(float(r.get("volume") or 0) for r in rows if r.get("right") == "P")
    if call_volume <= 0:
        return None
    return put_volume / call_volume, call_volume, put_volume


class PcrCollector:
    """Put/call ratio z poslední minuty snapshot partice aktivní expirace.

    Volume ve snapshotu je kumulativní denní objem per kontrakt — PCR je tedy
    „denní poměr k této minutě", což je přesně crowd proxy ze SPEC 5.8.
    """

    name = "gexlens_pcr"

    def __init__(self, data_dir: Path, symbols: Sequence[str], *, interval_s: float) -> None:
        self.interval_s = interval_s
        self._snapshots = data_dir / "snapshots"
        self._symbols = list(symbols)

    def _active_expiry(self, symbol: str, today: dt.date) -> Path | None:
        """Nejbližší expirace (≥ dnešek), která má dnešní partici."""
        root = self._snapshots / symbol
        if not root.exists():
            return None
        today_compact = today.strftime("%Y%m%d")
        candidates = sorted(
            path.name for path in root.iterdir() if path.is_dir() and path.name >= today_compact
        )
        for expiry in candidates:
            partition = root / expiry / f"{today.isoformat()}.parquet"
            if partition.exists():
                return partition
        return None

    def collect(self, now: dt.datetime) -> list[CrowdPoint]:
        points: list[CrowdPoint] = []
        for symbol in self._symbols:
            partition = self._active_expiry(symbol, now.date())
            if partition is None:
                continue
            try:
                table = pq.read_table(partition, columns=["ts_min", "right", "volume"])
            except Exception:
                logger.exception("PCR: partice %s nečitelná — přeskakuji", partition)
                continue
            rows = table.to_pylist()
            if not rows:
                continue
            last_minute = max(row["ts_min"] for row in rows)
            result = compute_pcr([row for row in rows if row["ts_min"] == last_minute])
            if result is None:
                continue
            pcr, call_volume, put_volume = result
            ts = last_minute if last_minute.tzinfo else last_minute.replace(tzinfo=dt.UTC)
            points.append(
                CrowdPoint(
                    ts=ts,
                    source="gexlens",
                    metric="pcr_volume",
                    symbol=symbol,
                    value=pcr,
                    raw={
                        "call_volume": call_volume,
                        "put_volume": put_volume,
                        "expiry": partition.parent.name,
                    },
                )
            )
        return points


# ── Runner ─────────────────────────────────────────────────────────


class CrowdCollectorLike:
    """Tvar crowd collectoru pro runner (name, interval_s, collect)."""

    name: str
    interval_s: float

    def collect(self, now: dt.datetime) -> list[CrowdPoint]:  # pragma: no cover - protokol
        raise NotImplementedError


@dataclass
class _SourceState:
    last_run: dt.datetime | None = None
    failures: int = 0


class CrowdRunner:
    """Per-zdroj intervaly a izolace chyb — zrcadlí CollectorRunner (SPEC 3.2)."""

    def __init__(self, collectors: Sequence[Any], writer: CrowdWriter) -> None:
        self._collectors = list(collectors)
        self._writer = writer
        self._states: dict[str, _SourceState] = {c.name: _SourceState() for c in self._collectors}

    def run_due(self, now: dt.datetime) -> int:
        """Spustí zdroje, kterým uplynul interval; vrací počet zapsaných bodů."""
        written = 0
        for collector in self._collectors:
            state = self._states[collector.name]
            if state.last_run is not None:
                elapsed = (now - state.last_run).total_seconds()
                if elapsed < collector.interval_s:
                    continue
            state.last_run = now
            try:
                points = collector.collect(now)
            except Exception:
                state.failures += 1
                logger.exception("Crowd zdroj %s selhal — zkusí se příští interval", collector.name)
                continue
            state.failures = 0
            count = self._writer.write(points)
            if count:
                logger.info("Crowd %s: %d nových bodů", collector.name, count)
            written += count
        return written
