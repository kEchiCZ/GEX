"""Historický backfill headlines z Alpaca News (#744).

Skutečný news korpus vznikl teprve v červenci 2026 (#740 fáze 1) — RSS historii
z principu nedává. Alpaca ji má (změřeno: minimálně do 2016), takže limitem je
až náš archiv barů, bez kterého by se stejně nedaly spočítat reakce.

**Filtr relevance je jádro věci.** Alpaca posílá ~900 zpráv denně, ale 85 % jsou
small caps, které indexem nehnou. Bez filtru by backfill za dva roky znamenal
~670 tis. zpráv a ~2 GB textu — z valné většiny šum, který by model spíš zhoršil
(a `GEXLENS_DISK_LIMIT_GB` je 5). S filtrem zbude ~145 zpráv denně, tedy zhruba
109 tis. za dva roky.

Normalizace je záměrně **tatáž funkce jako u živého streamu** (`normalize_message`):
REST položka nese stejné klíče, jen bez `"T": "n"`, které se dopisuje. Dvě
implementace téhož by dřív nebo později začaly ukládat odlišná data.

Idempotentní — `NewsWriter` zapisuje `ON CONFLICT DO NOTHING` nad `dedup_hash`,
takže opakované spuštění nebo překryv s živým korpusem duplicity nevyrobí.

Spuštění:
    python scripts/alpaca_news_backfill.py --dry-run          # jen změří objem
    python scripts/alpaca_news_backfill.py --from 2024-07-28  # ostrý běh
Prostředí: GEXLENS_NEWS_ALPACA_KEY_ID, _SECRET, GEXLENS_NEWS_DATABASE_URL.
"""

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine

for _sub in ("news-engine", "engine"):
    _path = Path(__file__).resolve().parents[1] / _sub / "src"
    if _path.is_dir():
        sys.path.insert(0, str(_path))

from gexlens_engine.storage.sentiment import ensure_sentiment_schema  # noqa: E402
from gexlens_news.collectors.alpaca import normalize_message  # noqa: E402
from gexlens_news.store import NewsWriter  # noqa: E402

URL = "https://data.alpaca.markets/v1beta1/news"

#: Začátek archivu barů (ES i NQ, 643 dnů) — dřív reakce nespočítáme, takže
#: starší zprávy by ležely v DB bez užitku.
DEFAULT_FROM = dt.date(2024, 7, 28)

#: Indexové ETF sledují tentýž podklad jako naše futures: SPY ≈ ES (S&P 500),
#: QQQ ≈ NQ (Nasdaq 100). Zpráva o nich je zpráva o indexu.
INDEX_ETF = frozenset({"SPY", "QQQ", "DIA", "IWM", "VOO", "IVV", "SPX", "NDX"})

#: Největší složky indexů — jejich zprávy indexem reálně hýbou (NVDA, AAPL).
#: Drží se odděleně od ETF, aby šlo později změřit, jestli přispívají, nebo jen
#: přidávají šum; rozlišit je jde zpětně přes uložené `symbols`.
MEGA_CAP = frozenset(
    {
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO",
        "BRK.B", "LLY", "JPM", "V", "XOM", "UNH", "MA", "COST", "HD", "PG",
        "NFLX", "JNJ", "ORCL", "WMT", "AMD", "CRM", "BAC", "KO", "PEP", "TMUS",
    }
)  # fmt: skip

PAGE_LIMIT = 50
#: Alpaca free tier ~200 req/min; 0,35 s mezi requesty drží rezervu i při
#: souběhu s živým streamem téhož účtu.
SLEEP_S = 0.35


def is_relevant(symbols: set[str]) -> bool:
    """Zpráva pro ES/NQ: index, jeho velká složka, nebo makro bez tickeru.

    Prázdné `symbols` = makro/obecná zpráva (Fed, CPI, geopolitika) — ty jsou
    pro index nejrelevantnější ze všech, i když žádný ticker nenesou.
    """
    if not symbols:
        return True
    return bool(symbols & INDEX_ETF) or bool(symbols & MEGA_CAP)


def fetch_day(client: httpx.Client, day: dt.date, headers: dict[str, str]) -> list[dict]:
    """Všechny zprávy dne přes stránkování; vrací jen relevantní."""
    params: dict[str, object] = {
        "start": f"{day.isoformat()}T00:00:00Z",
        "end": f"{day.isoformat()}T23:59:59Z",
        "limit": PAGE_LIMIT,
        "include_content": "true",
    }
    out: list[dict] = []
    token: str | None = None
    while True:
        if token:
            params["page_token"] = token
        response = client.get(URL, params=params, headers=headers, timeout=30)
        if response.status_code == 429:
            time.sleep(5)
            continue
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("news", []):
            if is_relevant(set(item.get("symbols") or [])):
                out.append(item)
        token = payload.get("next_page_token")
        if not token:
            return out
        time.sleep(SLEEP_S)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", default=DEFAULT_FROM.isoformat())
    parser.add_argument("--to", dest="date_to", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Nic nezapisuje, jen změří objem")
    args = parser.parse_args()

    key = os.environ.get("GEXLENS_NEWS_ALPACA_KEY_ID")
    secret = os.environ.get("GEXLENS_NEWS_ALPACA_SECRET")
    if not key or not secret:
        raise SystemExit("Chybí GEXLENS_NEWS_ALPACA_KEY_ID / _SECRET")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    start = dt.date.fromisoformat(args.date_from)
    end = dt.date.fromisoformat(args.date_to) if args.date_to else dt.date.today()

    writer = None
    if not args.dry_run:
        url = os.environ.get("GEXLENS_NEWS_DATABASE_URL") or os.environ.get("GEXLENS_DATABASE_URL")
        if not url:
            raise SystemExit("Chybí GEXLENS_NEWS_DATABASE_URL")
        engine = create_engine(url)
        ensure_sentiment_schema(engine)
        writer = NewsWriter(engine)

    now = dt.datetime.now(dt.UTC)
    total = written = text_bytes = with_body = 0
    day = start
    days = 0
    with httpx.Client() as client:
        while day <= end:
            items = fetch_day(client, day, headers)
            events = []
            for item in items:
                event = normalize_message({**item, "T": "n"}, now)
                if event is None:
                    continue
                events.append(event)
                text_bytes += len((event.title or "").encode()) + len((event.body or "").encode())
                if event.body:
                    with_body += 1
            total += len(events)
            if writer is not None and events:
                written += writer.write(events)
            days += 1
            if days % 30 == 0 or day == end:
                print(
                    f"  {day}: celkem {total} zpráv, zapsáno {written}, "
                    f"s plným textem {with_body}, text {text_bytes / 1_048_576:.1f} MB",
                    flush=True,
                )
            day += dt.timedelta(days=1)
            time.sleep(SLEEP_S)

    print(f"\n=== {'DRY-RUN' if args.dry_run else 'HOTOVO'} {start} – {end} ({days} dnů) ===")
    print(f"relevantních zpráv: {total}")
    print(f"z toho s plným textem: {with_body}")
    print(f"objem textu: {text_bytes / 1_048_576:.1f} MB")
    if writer is not None:
        print(f"nově zapsáno (po dedupu): {written}")


if __name__ == "__main__":
    main()
