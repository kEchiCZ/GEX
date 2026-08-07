"""Autentizace interních endpointů a kontrola původu WS spojení (#542).

Model hrozby: API nemá uživatele ani role — chrání se dvě různé věci.

1. **Zápis a export** (`/internal/*`, `/backup/postgres`). Volá je jen engine,
   respektive přihlášený uživatel z UI. Sdílené tajemství z `GEXLENS_API_TOKEN`
   stačí a nevyžaduje session vrstvu. Bez nastaveného tokenu se endpointy
   odmítají (503) — otevřený režim by se na serveru tiše přenesl do provozu.
2. **Odposlech WS** (`/ws/live`). Tady token nepomůže: útok vede přes prohlížeč
   uživatele (cross-site WebSocket hijacking), který by tajemství poslal sám.
   Brání se kontrolou hlavičky `Origin` — CORS se na WS handshake nevztahuje.
"""

import os
import re
import secrets
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import Header, HTTPException

TOKEN_HEADER = "X-GEXLens-Token"
TOKEN_ENV = "GEXLENS_API_TOKEN"
ORIGINS_ENV = "GEXLENS_ALLOWED_ORIGINS"

# Vývojové origins: Vite dev server i nginx na hostiteli. Vzdálená stránka sem
# nespadne, takže CSWSH z internetu je zaříznuté i s tímto povolením.
LOCAL_ORIGIN_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")


def load_api_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def load_allowed_origins() -> list[str]:
    """Extra povolené origins z env (čárkou oddělené), např. Tailscale adresa UI."""
    raw = os.environ.get(ORIGINS_ENV, "")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def origin_allowed(origin: str | None, host: str | None, extra: list[str]) -> bool:
    """Smí spojení s tímto `Origin` dostat živá data?

    `origin=None` je klient mimo prohlížeč (engine, curl, testy) — ten se
    hlavičkou stejně neprokazuje a hlavně není pod kontrolou cizí stránky.
    """
    if origin is None:
        return True
    origin = origin.rstrip("/")
    if origin in extra:
        return True
    if LOCAL_ORIGIN_RE.match(origin):
        return True
    # Same-origin za reverzní proxy: nginx přepošle Host stránky, ze které
    # se UI načetlo, takže shoda s netloc originu znamená „vlastní frontend"
    return bool(host) and urlsplit(origin).netloc == host


def build_token_guard(token: str) -> Callable[[str | None], None]:
    """Dependency ověřující sdílené tajemství v hlavičce."""

    def guard(supplied: str | None = Header(default=None, alias=TOKEN_HEADER)) -> None:
        if not token:
            raise HTTPException(
                503,
                f"{TOKEN_ENV} není nastaven — endpoint je vypnutý "
                "(vygeneruj tajemství přes scripts/init-secrets.ps1)",
            )
        if supplied is None or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, f"Chybí nebo nesedí hlavička {TOKEN_HEADER}")

    return guard
