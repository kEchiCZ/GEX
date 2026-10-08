"""Audit stávajících zdrojů zpráv (E-6.21, #1473) — jen čtení PG.

Po zdroji a podskupině (feed RSS, autor Alpaca, vydavatel Finnhubu, kurátor
Bluesky, provider IBKR): objem za 24 h a 7 dní podle `ts_ingested`, zpoždění
`ts_ingested − ts_event` (medián, p90, podíl nad hodinu, záporná, čas rovný
příjmu), medián délky titulku, poslední příjem; denní mediány zpoždění za 30 dní.
Dál kalendář podle času releasu, uložené sloučené duplicity (`raw.merged_sources`)
a registr `news_sources` proti záznamům.

Okna se počítají od pevného `as_of` (výchozí: začátek běhu). Objem podle
příjmu, ne podle `ts_event`: kalendář se vkládá dopředu a Bluesky nese čas
autora, který může být starý.

Všechny dotazy běží na spojení s `default_transaction_read_only` v jedné
transakci `SET TRANSACTION READ ONLY`, která končí rollbackem (AGENTS.md: data
v PG nejdou znovu pořídit).

Spuštění z hostitele (PG publikované na 55432; URL se nikdy nevypisuje):
    uv run python scripts/measure_news_sources_6_21.py [--as-of 2026-10-08T21:00:00+00:00]
URL z `--db`, `GEXLENS_HOST_DATABASE_URL`, nebo `GEXLENS_PG_PASSWORD`
(uživatel/DB `gexlens`, 127.0.0.1:55432). Výstup je Markdown na stdout.
"""

import argparse
import datetime as dt
import os
from collections.abc import Sequence
from typing import Any

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection

from gexlens_news.feed_watch import feed_label

#: Podskupina uvnitř zdroje — u agregátorů rozhoduje o typu obsahu víc než zdroj.
SUBGROUP_SQL = """
CASE source
    WHEN 'alpaca' THEN CASE
        WHEN raw->>'author' IN ('Benzinga Newsdesk', 'Benzinga Insights') THEN raw->>'author'
        ELSE 'ostatní autoři' END
    WHEN 'finnhub' THEN coalesce(raw->>'source', '')
    WHEN 'bluesky' THEN CASE WHEN raw->>'curated' = 'true' THEN 'kurátorovaní'
        ELSE 'nekurátorovaní' END
    WHEN 'ibkr_brfg' THEN coalesce(raw->>'provider', '')
    WHEN 'ibkr_djnl' THEN coalesce(raw->>'provider', '')
    ELSE coalesce(raw->>'feed', '')
END
"""

SOURCES_SQL = f"""
SELECT source, {SUBGROUP_SQL} AS sub, kind,
       count(*) FILTER (WHERE ts_ingested >= :as_of - interval '24 hours') AS n_24h,
       count(*) AS n_7d,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM ts_ingested - ts_event))
           AS lag_p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM ts_ingested - ts_event))
           AS lag_p90,
       count(*) FILTER (WHERE ts_ingested - ts_event > interval '1 hour') AS over_1h,
       count(*) FILTER (WHERE ts_ingested < ts_event) AS negative,
       count(*) FILTER (WHERE ts_ingested = ts_event) AS no_own_time,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY length(title)) AS title_p50,
       max(ts_ingested) AS last_ingested
FROM news_events
WHERE ts_ingested >= :as_of - interval '7 days' AND ts_ingested < :as_of
GROUP BY 1, 2, 3
ORDER BY 1, 2
"""

# Je zpoždění okna 7 d typické? Denní mediány podle UTC dne příjmu za 30 dní
DAILY_LAG_SQL = """
SELECT source, count(*) AS days, min(p50) AS lo,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY p50) AS mid, max(p50) AS hi
FROM (
    SELECT source, date_trunc('day', ts_ingested, 'UTC') AS day,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM ts_ingested - ts_event))
               AS p50
    FROM news_events
    WHERE kind <> 'scheduled'
      AND ts_ingested >= :as_of - interval '30 days' AND ts_ingested < :as_of
    GROUP BY 1, 2
) daily
GROUP BY 1 ORDER BY 1
"""

# Kalendář se vkládá dopředu (`ts_event` = čas releasu), objem dává smysl podle něj
CALENDAR_SQL = """
SELECT count(*) FILTER (WHERE ts_event >= :as_of - interval '24 hours') AS n_24h,
       count(*) AS n_7d,
       count(*) FILTER (WHERE actual IS NOT NULL) AS with_actual
FROM news_events
WHERE kind = 'scheduled' AND ts_event >= :as_of - interval '7 days' AND ts_event < :as_of
"""

# `raw` je JSON (ne JSONB) — operátor `?` jen přes přetypování
MERGED_SQL = """
SELECT source, count(*) FROM news_events
WHERE (raw::jsonb) ? 'merged_sources' AND ts_ingested < :as_of
GROUP BY 1 ORDER BY 1
"""

REGISTRY_SQL = """
SELECT coalesce(r.source, e.source) AS source, r.tier, r.expected_daily_volume, r.enabled,
       e.n, e.first_ingested, e.last_ingested
FROM news_sources r
FULL OUTER JOIN (
    SELECT source, count(*) AS n, min(ts_ingested) AS first_ingested,
           max(ts_ingested) AS last_ingested
    FROM news_events WHERE ts_ingested < :as_of GROUP BY source
) e ON e.source = r.source
ORDER BY 1
"""


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _seconds(value: float | None) -> str:
    if value is None:
        return "—"
    # Pod 10 s rozhoduje desetina (push zdroje, posun hodin), nad tím celé sekundy
    if abs(value) < 10:
        return f"{value:.1f}".replace(".", ",")
    return f"{value:,.0f}".replace(",", " ")


def _ts(value: dt.datetime | None) -> str:
    return "—" if value is None else f"{value.astimezone(dt.UTC):%Y-%m-%d %H:%M}"


def _sub_label(sub: str) -> str:
    return feed_label(sub) if sub.startswith("http") else sub


def sources(conn: Connection, as_of: dt.datetime) -> str:
    rows: list[list[object]] = []
    for row in conn.execute(text(SOURCES_SQL), {"as_of": as_of}):
        scheduled = row.kind == "scheduled"
        rows.append(
            [
                row.source,
                _sub_label(row.sub) or "—",
                row.n_24h,
                row.n_7d,
                "—" if scheduled else _seconds(row.lag_p50),
                "—" if scheduled else _seconds(row.lag_p90),
                "—" if scheduled else f"{row.over_1h / row.n_7d:.0%}",
                row.negative,
                row.no_own_time,
                f"{row.title_p50:.0f}",
                _ts(row.last_ingested),
            ]
        )
    header = [
        "zdroj",
        "podskupina",
        "n 24 h",
        "n 7 d",
        "zpoždění p50 [s]",
        "p90 [s]",
        "> 1 h",
        "< 0",
        "čas = příjem",
        "titulek p50 [znaků]",
        "poslední příjem (UTC)",
    ]
    return _table(header, rows)


def daily_lag(conn: Connection, as_of: dt.datetime) -> str:
    rows = [
        [r.source, r.days, _seconds(r.lo), _seconds(r.mid), _seconds(r.hi)]
        for r in conn.execute(text(DAILY_LAG_SQL), {"as_of": as_of})
    ]
    header = ["zdroj", "dnů s příjmem", "min [s]", "medián [s]", "max [s]"]
    return _table(header, rows)


def calendar(conn: Connection, as_of: dt.datetime) -> str:
    row = conn.execute(text(CALENDAR_SQL), {"as_of": as_of}).one()
    return _table(
        ["kalendář (`kind = scheduled`)", "24 h", "7 d"],
        [
            ["položek s časem releasu v okně", row.n_24h, row.n_7d],
            ["z toho s vyplněným `actual` (7 d)", "", row.with_actual],
        ],
    )


def merged(conn: Connection, as_of: dt.datetime) -> str:
    rows = [[r.source, r.count] for r in conn.execute(text(MERGED_SQL), {"as_of": as_of})]
    if not rows:
        return "Žádný záznam v celé historii `news_events` nenese `raw.merged_sources`."
    return _table(["zdroj", "záznamů s `merged_sources`"], rows)


def registry(conn: Connection, as_of: dt.datetime) -> str:
    rows = [
        [
            r.source,
            r.tier or "**mimo registr**",
            "—" if r.expected_daily_volume is None else r.expected_daily_volume,
            "—" if r.enabled is None else ("ano" if r.enabled else "ne"),
            r.n or "**0**",
            _ts(r.first_ingested),
            _ts(r.last_ingested),
        ]
        for r in conn.execute(text(REGISTRY_SQL), {"as_of": as_of})
    ]
    header = ["zdroj", "tier", "očekáváno/den", "enabled", "záznamů", "první příjem", "poslední"]
    return _table(header, rows)


def database_url(explicit: str | None) -> str | URL:
    if explicit:
        return explicit
    url = os.environ.get("GEXLENS_HOST_DATABASE_URL")
    if url:
        return url
    password = os.environ.get("GEXLENS_PG_PASSWORD")
    if not password:
        raise SystemExit("Chybí --db, GEXLENS_HOST_DATABASE_URL nebo GEXLENS_PG_PASSWORD")
    # URL.create heslo escapuje — speciální znaky ho nerozbijí
    return URL.create(
        "postgresql+psycopg",
        username="gexlens",
        password=password,
        host="127.0.0.1",
        port=55432,
        database="gexlens",
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", help="SQLAlchemy URL; jinak z prostředí (nevypisuje se)")
    parser.add_argument("--as-of", type=dt.datetime.fromisoformat, help="konec oken, ISO s TZ")
    args = parser.parse_args(argv)
    as_of: dt.datetime = args.as_of or dt.datetime.now(dt.UTC).replace(microsecond=0)
    if as_of.tzinfo is None:
        raise SystemExit("--as-of musí nést časové pásmo (např. +00:00)")

    engine = create_engine(
        database_url(args.db), connect_args={"options": "-c default_transaction_read_only=on"}
    )
    sections: list[tuple[str, Any]] = [
        ("Zdroje a podskupiny (okno 7 d podle `ts_ingested`)", sources),
        ("Denní medián zpoždění za 30 dní (UTC den příjmu)", daily_lag),
        ("Kalendář podle času releasu", calendar),
        ("Uložené sloučené duplicity", merged),
        ("Registr `news_sources` × záznamy v `news_events` (celá historie)", registry),
    ]
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        print(f"`as_of` = {as_of.astimezone(dt.UTC).isoformat()}\n")
        for title, section in sections:
            print(f"### {title}\n\n{section(conn, as_of)}\n")
        conn.rollback()


if __name__ == "__main__":
    main()
