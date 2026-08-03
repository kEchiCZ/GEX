"""Záloha PostgreSQL na jedno kliknutí (issue #439).

Parquety leží na disku uživatele (bind mount `./data`), takže je zálohuje
kdejaká kopie složky projektu. PostgreSQL je ale v Docker volume `gex_pgdata`
uvnitř VM — a přitom v něm sedí data, která se **nedají znovu pořídit**: věčný
OI archiv (R4), setupy s výsledky, signály, tendence, statistiky modelu.
Smazání volume je nevratné.

Endpoint proto pouští `pg_dump -Fc` a výsledek **streamuje do prohlížeče** jako
soubor ke stažení. Zápis na libovolné místo disku z kontejneru nejde (vidí jen
`/app/data`), takže cílovou složku vybírá uživatel v prohlížeči — a rovnou tím
zálohu dostane mimo Docker i mimo repozitář.
"""

import asyncio
import datetime as dt
import logging
import shutil
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

# Formát custom (-Fc): komprimovaný a obnovitelný `pg_restore` i selektivně
DUMP_MEDIA_TYPE = "application/octet-stream"
CHUNK_BYTES = 64 * 1024


def dump_command(database_url: str) -> list[str]:
    """Argumenty `pg_dump` z SQLAlchemy DSN.

    Heslo se do argv NEDÁVÁ (bylo by vidět v `ps`) — předává se přes prostředí
    v `dump_env`. Bez jména databáze nemá záloha co dumpovat → chyba hned tady,
    ne až v podprocesu.
    """
    url = make_url(database_url)
    if not url.database:
        raise ValueError("database_url neobsahuje jméno databáze")
    command = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    if url.host:
        command += ["--host", url.host]
    if url.port:
        command += ["--port", str(url.port)]
    if url.username:
        command += ["--username", url.username]
    command.append(url.database)
    return command


def dump_env(database_url: str) -> dict[str, str]:
    """Prostředí pro pg_dump — heslo přes PGPASSWORD, ne přes argv."""
    url = make_url(database_url)
    return {"PGPASSWORD": url.password} if url.password else {}


def backup_filename(now: dt.datetime) -> str:
    return f"gexlens-{now:%Y-%m-%d_%H%M}.dump"


async def stream_dump(database_url: str) -> AsyncIterator[bytes]:
    """Spustí pg_dump a proudí jeho výstup po blocích.

    Streamuje se schválně: dump má desítky MB a držet ho celý v paměti API
    kvůli jednomu tlačítku nemá důvod.
    """
    import os

    command = dump_command(database_url)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **dump_env(database_url)},
    )
    assert process.stdout is not None
    try:
        while chunk := await process.stdout.read(CHUNK_BYTES):
            yield chunk
    finally:
        stderr = b""
        if process.stderr is not None:
            stderr = await process.stderr.read()
        code = await process.wait()
        if code != 0:
            # Klient už má část dat; chybu nelze poslat jako HTTP status, proto
            # aspoň do logu — poškozený soubor pozná pg_restore
            logger.error("pg_dump skončil s kódem %d: %s", code, stderr.decode(errors="replace"))


def build_backup_router(database_url: str) -> APIRouter:
    router = APIRouter(prefix="/backup", tags=["backup"])

    @router.get("/postgres")
    async def postgres_backup() -> StreamingResponse:
        """Stream `pg_dump -Fc` jako soubor ke stažení."""
        if shutil.which("pg_dump") is None:
            raise HTTPException(
                status_code=503,
                detail="pg_dump není v image k dispozici — přebuduj API kontejner",
            )
        try:
            dump_command(database_url)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        name = backup_filename(dt.datetime.now())
        return StreamingResponse(
            stream_dump(database_url),
            media_type=DUMP_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return router
