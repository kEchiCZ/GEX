"""Sonda kandidátů na zdroje breaking news (E-6.22, #1474) — jen GET, nic nezapisuje.

Pro každého kandidáta jeden GET a jeden GET `robots.txt` stejnou hlavičkou
`User-Agent`, jakou by posílal adaptér: prohlížečová `BROWSER_UA` jako dnešní
fetcher, u SEC a BLS identifikace s kontaktem (bez ní vrací 403 / stránku
překročeného limitu). Výstup: HTTP status, formát, počet položek, podíl položek
s časem, stáří nejnovější položky, medián odstupu položek, validátory pro
conditional GET (ETag / Last-Modified), hlavičky rate limitu a verdikt
robots.txt pro danou cestu.

Jde o **jednorázový snímek** feedu v čase běhu, ne o průměr: odstup a stáří
položek popisují, co feed drží teď. Zpoždění proti skutečné publikaci změří
až provoz (E-6.29).

Prostředí (hodnoty se nevypisují, jen jméno proměnné):
    GEXLENS_NEWS_SEC_CONTACT     kontakt do User-Agent pro SEC a BLS (#1457)
    GEXLENS_NEWS_FINNHUB_API_KEY klíč Finnhubu (collector ho už používá)
Bez proměnné se kandidát přeskočí s důvodem.

Spuštění:
    uv run python scripts/news_candidates_probe.py > report.md
"""

import datetime as dt
import json
import os
import statistics
import sys
import unicodedata
import urllib.robotparser
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

from gexlens_news.collectors.rss import parse_feed_time, parse_items
from gexlens_news.http import BROWSER_UA, strip_secrets

TIMEOUT_S = 20.0
CONTACT_ENV = "GEXLENS_NEWS_SEC_CONTACT"
FINNHUB_ENV = "GEXLENS_NEWS_FINNHUB_API_KEY"
#: Hlavičky, ze kterých jde vyčíst rate limit (názvy se mezi poskytovateli liší)
RATE_HEADERS = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "ratelimit-limit")


@dataclass(frozen=True)
class Candidate:
    name: str
    tier: str
    url: str
    #: "browser" = BROWSER_UA jako dnešní fetcher; "contact" = identifikace s kontaktem
    ua: str = "browser"
    #: Proměnná prostředí s klíčem, který se dosadí za `{key}` v URL
    key_env: str | None = None


CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        "Fed — všechny tiskové zprávy", "1", "https://www.federalreserve.gov/feeds/press_all.xml"
    ),
    Candidate(
        "Fed — měnová politika (dnes)",
        "1",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
    ),
    Candidate("Fed — projevy (dnes)", "1", "https://www.federalreserve.gov/feeds/speeches.xml"),
    Candidate(
        "Fed — svědectví v Kongresu", "1", "https://www.federalreserve.gov/feeds/testimony.xml"
    ),
    Candidate(
        "BLS — nejnovější releasy", "1", "https://www.bls.gov/feed/bls_latest.rss", ua="contact"
    ),
    Candidate("BEA — zprávy (www)", "1", "https://www.bea.gov/news/rss"),
    Candidate("BEA — zprávy (apps)", "1", "https://apps.bea.gov/rss/rss.xml"),
    Candidate(
        "Treasury — tiskové zprávy (GovDelivery)",
        "1",
        "https://public.govdelivery.com/topics/USTREAS_49/feed.rss",
    ),
    Candidate(
        "Treasury — výpis tiskových zpráv (HTML)",
        "1",
        "https://home.treasury.gov/news/press-releases",
    ),
    Candidate("ECB — tiskové zprávy", "1", "https://www.ecb.europa.eu/rss/press.html"),
    Candidate("White House — zprávy", "1", "https://www.whitehouse.gov/news/feed/"),
    Candidate(
        "White House — prezidentské akty",
        "1",
        "https://www.whitehouse.gov/presidential-actions/feed/",
    ),
    Candidate(
        "SEC EDGAR — aktuální 8-K",
        "1",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
        "&owner=include&start=0&count=100&output=atom",
        ua="contact",
    ),
    Candidate("FinancialJuice", "2", "https://www.financialjuice.com/feed.ashx?xy=rss"),
    Candidate(
        "Finnhub general",
        "2",
        "https://finnhub.io/api/v1/news?category=general&token={key}",
        key_env=FINNHUB_ENV,
    ),
    Candidate(
        "Alpha Vantage NEWS_SENTIMENT (demo klíč)",
        "2",
        "https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=AAPL&apikey=demo",
    ),
)


@dataclass
class Probe:
    status: str
    fmt: str = "—"
    items: int = 0
    with_time: int = 0
    newest_age_s: float | None = None
    median_gap_s: float | None = None
    validators: str = "—"
    rate: str = "—"
    robots: str = "—"
    note: str = ""


def _user_agent(kind: str) -> str | None:
    if kind == "browser":
        return BROWSER_UA
    contact = os.environ.get(CONTACT_ENV, "").strip()
    if not contact:
        return None
    # Hlavička HTTP je ASCII; jméno s diakritikou by httpx odmítl (UnicodeEncodeError)
    ascii_contact = unicodedata.normalize("NFKD", contact).encode("ascii", "ignore").decode()
    return f"GEXLens research {ascii_contact}"


def _json_times(payload: object) -> tuple[int, list[dt.datetime]]:
    """Časy položek z JSON odpovědi Finnhubu (pole, `datetime` epoch) a Alpha Vantage."""
    if isinstance(payload, list):
        stamps = [item.get("datetime") for item in payload if isinstance(item, dict)]
        times = [dt.datetime.fromtimestamp(float(s), tz=dt.UTC) for s in stamps if s]
        return len(payload), times
    if isinstance(payload, dict) and isinstance(payload.get("feed"), list):
        feed = payload["feed"]
        times = []
        for item in feed:
            raw = item.get("time_published") if isinstance(item, dict) else None
            if raw:
                # Alpha Vantage pásmo neuvádí; bere se jako UTC (výhrada v reportu)
                times.append(dt.datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=dt.UTC))
        return len(feed), times
    return 0, []


def _parse(body: str, content_type: str) -> tuple[str, int, list[dt.datetime]]:
    text = body.lstrip()
    if "json" in content_type or text.startswith(("{", "[")):
        count, times = _json_times(json.loads(text))
        return "JSON", count, times
    if text.startswith("<?xml") or "xml" in content_type:
        items = parse_items(text)
        root_tag = ElementTree.fromstring(text).tag
        fmt = "Atom" if root_tag.endswith("feed") else "RSS"
        times = [t for t in (parse_feed_time(item["published"]) for item in items) if t]
        return fmt, len(items), times
    return "HTML", 0, []


def _robots(client: httpx.Client, url: str, ua: str) -> str:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        response = client.get(robots_url, headers={"User-Agent": ua})
    except httpx.HTTPError as error:
        return f"nečitelné ({type(error).__name__})"
    parser = urllib.robotparser.RobotFileParser()
    if response.status_code in (401, 403):
        return f"robots.txt {response.status_code} → zakázáno vše"
    if response.status_code >= 400:
        return f"robots.txt {response.status_code} → bez omezení"
    parser.parse(response.text.splitlines())
    return "povoleno" if parser.can_fetch(ua, url) else "**zakázáno**"


def probe(client: httpx.Client, candidate: Candidate, now: dt.datetime) -> Probe:
    ua = _user_agent(candidate.ua)
    if ua is None:
        return Probe(status="přeskočeno", note=f"chybí {CONTACT_ENV}")
    url = candidate.url
    if candidate.key_env:
        key = os.environ.get(candidate.key_env, "").strip()
        if not key:
            return Probe(status="přeskočeno", note=f"chybí {candidate.key_env}")
        url = url.format(key=key)
    try:
        response = client.get(url, headers={"User-Agent": ua})
    except httpx.HTTPError as error:
        return Probe(status=f"chyba sítě ({type(error).__name__})")
    result = Probe(status=str(response.status_code))
    if response.history:
        result.note = f"přesměrováno {len(response.history)}×"
    result.robots = _robots(client, url, ua)
    validators = [h for h in ("etag", "last-modified") if h in response.headers]
    result.validators = ", ".join(validators) or "žádné"
    rate = [f"{h}: {response.headers[h]}" for h in RATE_HEADERS if h in response.headers]
    result.rate = "; ".join(rate) or "—"
    if response.status_code != 200:
        return result
    try:
        fmt, count, times = _parse(response.text, response.headers.get("content-type", ""))
    except (ValueError, ElementTree.ParseError) as error:
        result.fmt = f"nečitelné ({type(error).__name__})"
        return result
    result.fmt, result.items, result.with_time = fmt, count, len(times)
    if times:
        ordered = sorted(times, reverse=True)
        result.newest_age_s = (now - ordered[0]).total_seconds()
        gaps = [(a - b).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False)]
        if gaps:
            result.median_gap_s = statistics.median(gaps)
    return result


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if abs(seconds) < 120:
        return f"{seconds:.0f} s"
    if abs(seconds) < 2 * 3600:
        return f"{seconds / 60:.0f} min"
    if abs(seconds) < 2 * 86400:
        return f"{seconds / 3600:.1f} h".replace(".", ",")
    return f"{seconds / 86400:.1f} d".replace(".", ",")


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def main() -> int:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    rows = []
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
        for candidate in CANDIDATES:
            result = probe(client, candidate, dt.datetime.now(dt.UTC))
            rows.append(
                [
                    candidate.tier,
                    candidate.name,
                    candidate.ua,
                    result.status,
                    result.fmt,
                    result.items,
                    f"{result.with_time}/{result.items}" if result.items else "—",
                    _duration(result.newest_age_s),
                    _duration(result.median_gap_s),
                    result.validators,
                    result.rate,
                    result.robots,
                    strip_secrets(result.note) or "—",
                ]
            )
    header = [
        "tier",
        "kandidát",
        "UA",
        "HTTP",
        "formát",
        "položek",
        "s časem",
        "stáří nejnovější",
        "medián odstupu",
        "conditional GET",
        "rate limit",
        "robots.txt",
        "poznámka",
    ]
    print(f"Snímek `as_of` = {now.isoformat()}\n")
    print(_table(header, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
