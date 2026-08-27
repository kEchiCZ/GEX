"""Uživatelsky editovatelné seznamy zdrojů (#578 rozšíření, 27. 8. 2026).

Záložka News → „Zdroje zpráv" edituje seznamy v tabulce `settings` (meta
schéma) přes API; news-engine je odsud čte:

- ``news_bluesky_authors`` — kurátorovaní Bluesky autoři (handle NEBO did:…);
  hot-reload za běhu (bluesky_loop je znovu načítá à ~10 min),
- ``news_reddit_subreddits`` — subreddity pro nativní RSS; čte se při startu,
- ``news_rss_extra`` — vlastní RSS feedy (URL); čte se při startu.

Seed je insert-if-missing: defaulty se doplní jen když klíč chybí. Jakmile
uživatel seznam uloží (včetně SMAZÁNÍ defaultních položek), jeho verze platí
a nikdy se nepřepisuje.
"""

import json
import logging
from collections.abc import Sequence
from urllib.parse import quote

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.meta import settings_table
from gexlens_engine.storage.sentiment import news_sources
from gexlens_news.http import Fetcher

logger = logging.getLogger(__name__)

SETTING_BLUESKY_AUTHORS = "news_bluesky_authors"
SETTING_REDDIT_SUBREDDITS = "news_reddit_subreddits"
SETTING_RSS_EXTRA = "news_rss_extra"

#: Defaultní kurátoři (US market sentiment) — handly, DID se resolvuje za běhu
#: (handle je čitelný a uživatel ho umí smazat/doplnit; DID by byl šum).
#: Výběr 27. 8. 2026: finanční novináři a analytici aktivní na Bluesky
#: + oficiální účty agentur; všechny handly ověřeny přes resolveHandle.
DEFAULT_BLUESKY_AUTHORS: tuple[str, ...] = (
    "tradersclub.bsky.social",
    "carlquintanilla.bsky.social",
    "thestalwart.bsky.social",
    "neilirwin.bsky.social",
    "heatherlong.bsky.social",
    "lizannsonders.bsky.social",
    "marketwatch.com",
    "cnbc.com",
    "bloomberg.com",
    "reuters.com",
)

DEFAULT_REDDIT_SUBREDDITS: tuple[str, ...] = ("wallstreetbets", "stocks")

RESOLVE_HANDLE_URL = (
    "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={handle}"
)


def seed_user_source_settings(engine: Engine) -> int:
    """Doplní chybějící klíče seznamů; existující (uživatelovy) nechává."""
    defaults: dict[str, list[str]] = {
        SETTING_BLUESKY_AUTHORS: list(DEFAULT_BLUESKY_AUTHORS),
        SETTING_REDDIT_SUBREDDITS: list(DEFAULT_REDDIT_SUBREDDITS),
        SETTING_RSS_EXTRA: [],
    }
    seeded = 0
    with engine.begin() as conn:
        existing = {
            row.key
            for row in conn.execute(
                select(settings_table.c.key).where(settings_table.c.key.in_(defaults))
            )
        }
        for key, value in defaults.items():
            if key not in existing:
                conn.execute(insert(settings_table).values(key=key, value=value))
                seeded += 1
    return seeded


def read_list_setting(engine: Engine, key: str) -> list[str]:
    """AKTIVNÍ položky seznamu z `settings`; chybějící klíč / cizí tvar → prázdno.

    Prefix ``#`` značí VYPNUTOU položku (#918): UI jím umožňuje položku
    (i defaultní) dočasně vypnout bez mazání — tady se přeskakuje. Cizí tvar
    se loguje — tichý fallback by vypadal jako „uživatel vše smazal".
    """
    with engine.connect() as conn:
        row = conn.execute(
            select(settings_table.c.value).where(settings_table.c.key == key)
        ).first()
    if row is None:
        return []
    value = row.value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        stripped = [item.strip() for item in value if item.strip()]
        return [item for item in stripped if not item.startswith("#")]
    logger.warning("Klíč %s nemá tvar seznamu řetězců (%r) — ignoruje se", key, type(value))
    return []


def source_enabled_map(engine: Engine) -> dict[str, bool]:
    """`enabled` z registru zdrojů (#578): vypnutý zdroj se nespouští.

    Zdroj mimo registr je implicitně zapnutý (get s defaultem True u volajícího).
    """
    with engine.connect() as conn:
        rows = conn.execute(select(news_sources.c.source, news_sources.c.enabled))
        return {row.source: bool(row.enabled) for row in rows}


def reddit_rss_urls(subreddits: Sequence[str]) -> tuple[str, ...]:
    """RSS URL per subreddit; hot řadí komunita, limit drží zátěž nízko."""
    return tuple(
        f"https://www.reddit.com/r/{quote(sub.removeprefix('r/'), safe='')}/hot/.rss?limit=25"
        for sub in subreddits
    )


class BlueskyAuthorResolver:
    """handle → DID přes veřejné resolveHandle API, s pamětí na selhání.

    Jetstream zprávy nesou DID, uživatel ale zadává čitelný handle. Výsledky
    se cachují (handle se mění zřídka); neúspěch se cachuje taky, aby se
    rozbitý handle nezkoušel při každém reloadu — po restartu se zkusí znovu.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._cache: dict[str, str | None] = {}

    async def resolve(self, authors: Sequence[str]) -> frozenset[str]:
        dids: set[str] = set()
        for author in authors:
            entry = author.strip().lstrip("@")
            if not entry:
                continue
            if entry.startswith("did:"):
                dids.add(entry)
                continue
            if entry not in self._cache:
                self._cache[entry] = await self._resolve_one(entry)
            did = self._cache[entry]
            if did is not None:
                dids.add(did)
        return frozenset(dids)

    async def _resolve_one(self, handle: str) -> str | None:
        try:
            response = await self._fetcher.get(RESOLVE_HANDLE_URL.format(handle=quote(handle)))
            if response.status != 200:
                logger.warning(
                    "Bluesky handle %r nejde přeložit (HTTP %d)", handle, response.status
                )
                return None
            did = json.loads(response.text).get("did")
            if not isinstance(did, str) or not did.startswith("did:"):
                logger.warning("Bluesky handle %r: odpověď bez DID", handle)
                return None
            logger.info("Bluesky kurátor %s → %s", handle, did)
            return did
        except Exception as exc:  # noqa: BLE001 — rozbitý handle nesmí shodit loop
            logger.warning("Bluesky handle %r: resolve selhal (%r)", handle, exc)
            return None
