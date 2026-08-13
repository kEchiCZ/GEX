"""OAuth2 sezení tastytrade (#613, #620, ADR-0025) — výhradně scope `read`.

Nikdy `/sessions` (přihlášení heslem je zakázané i pro vývoj), nikdy scope
`trade`. Access token platí 15 minut — správce ho obnovuje s předstihem
a refresh token s client secretem drží jen v paměti z env (S10). Tokeny se
NIKDY nesmí objevit v logu ani v repr — doloženo testem (precedens #553).
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.tastytrade.com"
# Obnova s předstihem: token platí 900 s, obnovuje se po 12 minutách
_REFRESH_MARGIN_S = 180.0


def redact(value: str) -> str:
    """Bezpečná reprezentace tajemství do logu: délka + zlomek, nikdy obsah."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…({len(value)} znaků)"


@dataclass
class TastyCredentials:
    """Přihlašovací údaje z env; repr nikdy neukazuje obsah (#620)."""

    client_secret: str
    refresh_token: str

    def __repr__(self) -> str:  # pragma: no cover - triviální
        return (
            f"TastyCredentials(client_secret={redact(self.client_secret)}, "
            f"refresh_token={redact(self.refresh_token)})"
        )


class TastySession:
    """Držák access tokenu s automatickou obnovou (platnost 15 min).

    `access_token()` vrací platný token; obnovu serializuje zámek, aby
    souběžné požadavky neposlaly refresh dvakrát.
    """

    def __init__(
        self, credentials: TastyCredentials, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self._credentials = credentials
        self._http = http or httpx.AsyncClient(base_url=API_BASE, timeout=15)
        self._token: str | None = None
        self._expires_at = dt.datetime.min.replace(tzinfo=dt.UTC)
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self._lock:
            now = dt.datetime.now(dt.UTC)
            if self._token is not None and now < self._expires_at:
                return self._token
            response = await self._http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._credentials.refresh_token,
                    "client_secret": self._credentials.client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
            self._token = str(payload["access_token"])
            lifetime = float(payload.get("expires_in", 900.0))
            self._expires_at = now + dt.timedelta(seconds=max(60.0, lifetime - _REFRESH_MARGIN_S))
            logger.info(
                "tasty OAuth token obnoven (platnost %.0f s, token %s)",
                lifetime,
                redact(self._token),
            )
            return self._token

    async def get_json(self, path: str) -> dict[str, Any]:
        """GET s Bearer tokenem; vrací JSON obálku tastytrade API."""
        token = await self.access_token()
        response = await self._http.get(path, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return dict(response.json())

    async def quote_token(self) -> tuple[str, str]:
        """(dxlink_url, quote_token) z /api-quote-tokens — vstup pro stream."""
        payload = await self.get_json("/api-quote-tokens")
        data = payload["data"]
        return str(data["dxlink-url"]), str(data["token"])

    async def close(self) -> None:
        await self._http.aclose()
