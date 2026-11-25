"""Testy úplné zálohy jedním balíčkem (#1799, akceptační kritéria)."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import tracemalloc
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import gexlens_api.backup as backup
import gexlens_api.full_backup as fb
from gexlens_api.security import build_token_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 11, 25, 9, 0, tzinfo=dt.UTC)  # středa, Globex otevřený
MARKER = "KONTROLNI-TAJEMSTVI-7f3a"
DUMP_BYTES = b"PGDMP" + bytes(range(256)) * 40

LIVE_FILE = "snapshots/ES/20261218/2026-11-25.parquet"
COLD_FILE = "snapshots/ES/20261218/2026-11-20.parquet"
EXPECTED_DATA = {
    LIVE_FILE,
    COLD_FILE,
    "derived/ES/2026-11-24.parquet",
    "reports/tasty-kpi.jsonl",
    "backup/bars-1232/ES.parquet",
    "quarantine/sync-2026-11-20/x.parquet",
}
EXCLUDED_DATA = {
    "ticks/ES/2026-11-25.parquet",
    "logs/engine.log",
    "local/instance.json",
    "backup/tmp/stary.dump",
    "snapshots/ES/20261218/2026-11-25.parquet.123.tmp",
}


def _env_example_keys() -> list[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE)
    return list(dict.fromkeys(keys))


def _environ() -> dict[str, str]:
    """Prostředí API s tajemstvími nastavenými na kontrolní řetězec."""
    env = {
        key: f"{MARKER}-{key}"
        for key, kind in fb.ENV_KEY_KINDS.items()
        if kind is fb.KeyKind.SECRET
    }
    env |= {
        "GEXLENS_DATABASE_URL": f"postgresql+psycopg://gexlens:{MARKER}@postgres:5432/gexlens",
        "GEXLENS_NEWS_DATABASE_URL": f"postgresql+psycopg://gexlens:{MARKER}@localhost/gexlens",
        "GEXLENS_DATA_DIR": "/app/data",
        "GEXLENS_SYMBOLS": "ES,NQ",
        "GEXLENS_PUSH_QUIET_HOURS": "23:00-06:00",
        "GEXLENS_SETUP_DISABLED_TEMPLATES": "a b",
        "GEXLENS_NEUZNAMY_KLIC": MARKER,  # neklasifikovaný = tajemství
        "GEXLENS_BUILD_SHA": "0123456789abcdef0123456789abcdef01234567",
        "PATH": "/usr/bin",
    }
    return env


def _write(path: Path, body: bytes, mtime: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    live = NOW - dt.timedelta(minutes=5)
    cold = NOW - dt.timedelta(days=3)
    for rel in EXPECTED_DATA | EXCLUDED_DATA:
        is_live = rel in {LIVE_FILE, "reports/tasty-kpi.jsonl"}
        _write(root / rel, f"obsah {rel}".encode() * 50, live if is_live else cold)
    return root


def _prepare(data_dir: Path, environ: dict[str, str] | None = None) -> fb.Package:
    env = _environ() if environ is None else environ
    run_dir = fb.new_run_dir(data_dir, NOW)
    (run_dir / "gexlens.dump").write_bytes(DUMP_BYTES)
    info = fb.source_info(env, NOW, "16.4")
    return fb.prepare_full(data_dir, run_dir, info, env)


def _members(blob: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        result: dict[str, bytes] = {}
        for info in archive.getmembers():
            handle = archive.extractfile(info)
            assert handle is not None
            result[info.name] = handle.read()
        return result


# ── Klasifikace klíčů a tajemství ────────────────────────────────────


def test_kazdy_klic_env_example_je_klasifikovany() -> None:
    keys = _env_example_keys()
    assert len(keys) > 100  # parser opravdu četl .env.example
    missing = [key for key in keys if key not in fb.ENV_KEY_KINDS]
    assert not missing, f"Klíč bez zařazení v ENV_KEY_KINDS: {missing}"


def test_klasifikace_nema_klice_navic() -> None:
    """Klíč, který z .env.example zmizel, nemá v konstantě co dělat (YAGNI)."""
    stale = set(fb.ENV_KEY_KINDS) - set(_env_example_keys())
    assert not stale


def test_hesla_a_url_s_heslem_jsou_tajemstvi_nebo_compose() -> None:
    assert fb.classify("GEXLENS_PG_PASSWORD") is fb.KeyKind.SECRET
    assert fb.classify("GEXLENS_TASTY_REFRESH_TOKEN") is fb.KeyKind.SECRET
    assert fb.classify("GEXLENS_DATABASE_URL") is fb.KeyKind.COMPOSE
    assert fb.classify("GEXLENS_NOVY_NEZNAMY") is fb.KeyKind.SECRET


def test_tajemstvi_se_v_bajtech_balicku_nevyskytuje(data_dir: Path) -> None:
    blob = b"".join(fb.iter_package(_prepare(data_dir)))
    assert MARKER.encode() not in blob
    members = _members(blob)
    template = members[fb.ENV_TEMPLATE_NAME].decode()
    assert "GEXLENS_SYMBOLS=ES,NQ" in template
    assert "GEXLENS_SETUP_DISABLED_TEMPLATES='a b'" in template
    assert "GEXLENS_PG_PASSWORD=\n" in template
    assert "# GEXLENS_DATABASE_URL — nastavuje compose" in template
    assert "GEXLENS_BUILD_SHA" not in template  # z image, ne z .env
    assert "PATH=" not in template
    secrets = members[fb.SECRET_KEYS_NAME].decode().splitlines()
    assert "GEXLENS_PG_PASSWORD" in secrets
    assert "GEXLENS_NEUZNAMY_KLIC" in secrets
    assert "GEXLENS_NEWS_UA_CONTACT" in secrets


def test_heslo_gateway_do_balicku_nejde(data_dir: Path) -> None:
    """`secrets/` leží mimo data/ a balíček z něj nic nebere."""
    (data_dir.parent / "secrets").mkdir()
    (data_dir.parent / "secrets" / "ibkr_password").write_text(MARKER)
    blob = b"".join(fb.iter_package(_prepare(data_dir)))
    assert MARKER.encode() not in blob
    assert not any("secrets" in name for name in _members(blob))


# ── Obsah, manifest, velikost ─────────────────────────────────────────


def test_balicek_obsahuje_presne_ocekavane_cleny(data_dir: Path) -> None:
    blob = b"".join(fb.iter_package(_prepare(data_dir)))
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        names = archive.getnames()
    assert names[0] == fb.RESTORE_NAME
    assert names[-1] == fb.MANIFEST_NAME
    expected = {fb.RESTORE_NAME, fb.ENV_TEMPLATE_NAME, fb.SECRET_KEYS_NAME, fb.DUMP_NAME}
    expected |= {fb.MANIFEST_NAME} | {fb.DATA_PREFIX + rel for rel in EXPECTED_DATA}
    assert set(names) == expected
    assert len(names) == len(expected)


def test_content_length_sedi_na_bajty(data_dir: Path) -> None:
    package = _prepare(data_dir)
    blob = b"".join(fb.iter_package(package))
    assert len(blob) == package.content_length()


def test_sha256_v_manifestu_sedi_na_obsah(data_dir: Path) -> None:
    members = _members(b"".join(fb.iter_package(_prepare(data_dir))))
    manifest = json.loads(members[fb.MANIFEST_NAME])
    assert manifest["format"] == fb.FORMAT_NAME
    assert manifest["format_version"] == fb.FORMAT_VERSION
    assert manifest["mode"] == "full"
    assert manifest["created_utc"] == "2026-11-25T09:00:00Z"
    assert manifest["image_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert manifest["pg_server_version"] == "16.4"
    assert manifest["market_open"] is True
    assert manifest["day_ranges"]["snapshots"] == {"from": "2026-11-20", "to": "2026-11-25"}
    listed = {item["path"]: item for item in manifest["files"]}
    assert set(listed) == set(members) - {fb.MANIFEST_NAME}
    for name, body in members.items():
        if name == fb.MANIFEST_NAME:
            continue
        assert listed[name]["size"] == len(body)
        assert listed[name]["sha256"] == hashlib.sha256(body).hexdigest()
    assert members[fb.DUMP_NAME] == DUMP_BYTES
    assert listed[fb.DATA_PREFIX + LIVE_FILE]["live"] is True
    assert listed[fb.DATA_PREFIX + COLD_FILE]["live"] is False


def test_restore_md_nese_hodnoty_z_manifestu(data_dir: Path) -> None:
    members = _members(b"".join(fb.iter_package(_prepare(data_dir))))
    restore = members[fb.RESTORE_NAME].decode()
    assert "{{" not in restore
    assert "GEXLENS_IMAGE_TAG=sha-0123456" in restore
    assert "`GEXLENS_PG_PASSWORD`" in restore
    assert "otevřeného trhu" in restore
    assert "`data/snapshots/`: 2026-11-20 až 2026-11-25" in restore


def test_zavreny_trh_a_lokalni_build_v_manifestu_i_navodu(data_dir: Path) -> None:
    saturday = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.UTC)
    env = {key: value for key, value in _environ().items() if key != "GEXLENS_BUILD_SHA"}
    run_dir = fb.new_run_dir(data_dir, saturday)
    (run_dir / "gexlens.dump").write_bytes(DUMP_BYTES)
    package = fb.prepare_full(data_dir, run_dir, fb.source_info(env, saturday, "16.4"), env)
    members = _members(b"".join(fb.iter_package(package)))
    manifest = json.loads(members[fb.MANIFEST_NAME])
    assert manifest["market_open"] is False
    assert manifest["image_sha"] == "dev"
    restore = members[fb.RESTORE_NAME].decode()
    assert "zavřeném trhu" in restore
    assert "git checkout dev" not in restore
    assert "pin image nejde" in restore


def test_meta_navod_odkazuje_na_restic(data_dir: Path, tmp_path: Path) -> None:
    dump = tmp_path / "gexlens.dump"
    dump.write_bytes(DUMP_BYTES)
    env = _environ()
    info = fb.source_info(env, NOW, "16.4")
    package = fb.build_package(data_files=[], dump=dump, info=info, environ=env, mode="meta")
    restore = next(m for m in package.members if m.arcname == fb.RESTORE_NAME)
    assert restore.data is not None
    assert "restic restore" in restore.data.decode()


def test_jmeno_balicku_ma_cas_a_sha7() -> None:
    info = fb.source_info(_environ(), NOW, "16.4")
    assert fb.package_filename(info) == "gexlens-zaloha-2026-11-25_0900-0123456.tar"
    local = fb.source_info({}, NOW, "16.4")
    assert local.image_sha == "dev"
    assert fb.package_filename(local).endswith("-dev.tar")


WINDOWS_TAR = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "tar.exe"
TAR_TOOLS = [
    pytest.param("tar", marks=pytest.mark.skipif(shutil.which("tar") is None, reason="tar chybí")),
    pytest.param(
        str(WINDOWS_TAR),
        marks=pytest.mark.skipif(not WINDOWS_TAR.exists(), reason="jen Windows (bsdtar)"),
    ),
]


@pytest.mark.parametrize("tool", TAR_TOOLS)
def test_tar_tf_balicek_precte(data_dir: Path, tmp_path: Path, tool: str) -> None:
    """GNU tar (CI, Git Bash) i Windows `tar.exe` (bsdtar) musí balíček vypsat."""
    target = tmp_path / "balicek.tar"
    target.write_bytes(b"".join(fb.iter_package(_prepare(data_dir))))
    listing = subprocess.run(
        [tool, "-tf", target.name], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    names = listing.stdout.split()
    assert names[-1] == fb.MANIFEST_NAME
    assert fb.DATA_PREFIX + LIVE_FILE in names


# ── Konzistence za otevřeného trhu ────────────────────────────────────


def test_partice_prepsana_behem_streamu_nerozbije_tar(data_dir: Path) -> None:
    """Engine dnešní partici přepíše `os.replace` — člen jde z kopie a velikost sedí."""
    # Velký starší soubor před dnešní particí: první blok streamu odejde
    # dřív, než se k ní čtení dostane
    _write(data_dir / COLD_FILE, os.urandom(3 << 20), NOW - dt.timedelta(days=3))
    package = _prepare(data_dir)
    original = (data_dir / LIVE_FILE).read_bytes()
    chunks = fb.iter_package(package)
    first = next(chunks)
    fresh = data_dir / (LIVE_FILE + ".999.tmp")
    fresh.write_bytes(b"novy obsah partice " * 500)
    os.replace(fresh, data_dir / LIVE_FILE)
    blob = first + b"".join(chunks)
    members = _members(blob)
    assert members[fb.DATA_PREFIX + LIVE_FILE] == original
    assert len(blob) == package.content_length()


def test_stary_soubor_zmeneny_behem_streamu_je_chyba(data_dir: Path) -> None:
    package = _prepare(data_dir)
    _write(data_dir / COLD_FILE, b"prepsano jinak", NOW)
    with pytest.raises(fb.PackageChangedError, match="změnil"):
        b"".join(fb.iter_package(package))


def test_chyba_zapisu_kopie_je_vyjimka_ne_vynechany_soubor(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zdroj existuje, selže zápis kopie (dlouhá cesta, plný disk) → chyba, ne tichá díra."""

    def failing(source: object, target: object) -> None:
        raise FileNotFoundError("cíl kopie nejde založit")

    monkeypatch.setattr(fb.shutil, "copyfile", failing)  # type: ignore[attr-defined]
    with pytest.raises(FileNotFoundError, match="cíl kopie"):
        _prepare(data_dir)


def test_zivy_soubor_ktery_zmizel_se_vynecha(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = shutil.copyfile

    def vanishing(source: Path, target: Path) -> object:
        if Path(source).name == Path(LIVE_FILE).name:
            Path(source).unlink()
            raise FileNotFoundError(str(source))
        return real(source, target)

    monkeypatch.setattr(fb.shutil, "copyfile", vanishing)  # type: ignore[attr-defined]
    names = {member.arcname for member in _prepare(data_dir).members}
    assert fb.DATA_PREFIX + LIVE_FILE not in names
    assert fb.DATA_PREFIX + COLD_FILE in names


def test_zive_soubory_jdou_z_kopie_v_pracovnim_adresari(data_dir: Path) -> None:
    package = _prepare(data_dir)
    sources = {member.arcname: member.source for member in package.members}
    live_source = sources[fb.DATA_PREFIX + LIVE_FILE]
    assert live_source is not None
    assert fb.WORK_DIR.as_posix() in live_source.as_posix()
    assert sources[fb.DATA_PREFIX + COLD_FILE] == data_dir / COLD_FILE


def test_pamet_streamu_neroste_s_velikosti_dat(tmp_path: Path) -> None:
    """Stream, ne buffer: špička alokací ~ bloky po 1 MiB, ne 48 MiB dat."""
    root = tmp_path / "data"
    cold = NOW - dt.timedelta(days=3)
    for index in range(6):
        _write(root / f"snapshots/ES/2026-11-0{index + 1}.parquet", os.urandom(8 << 20), cold)
    package = _prepare(root)
    tracemalloc.start()
    try:
        total = sum(len(chunk) for chunk in fb.iter_package(package))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total == package.content_length() > 48 << 20
    assert peak < 12 << 20, f"špička {peak / 2**20:.1f} MiB"


# ── Pracovní adresář a cíle CLI ───────────────────────────────────────


def test_uklid_starych_pracovnich_adresaru(tmp_path: Path) -> None:
    root = fb.work_root(tmp_path)
    old = root / f"full-{(NOW - dt.timedelta(days=2)):{fb.RUN_STAMP}}-1"
    fresh = root / f"full-{(NOW - dt.timedelta(hours=1)):{fb.RUN_STAMP}}-2"
    foreign = root / "full-cizi"
    for path in (old, fresh, foreign):
        path.mkdir(parents=True)
    fb.cleanup_stale_runs(tmp_path, NOW)
    assert not old.exists()
    assert fresh.exists() and foreign.exists()


def test_cil_v_zalohovanych_datech_se_odmita(tmp_path: Path) -> None:
    data = tmp_path / "data"
    fb.ensure_outside_backup(data / "local" / "zaloha.tar", data)
    fb.ensure_outside_backup(data / "backup" / "tmp" / "zaloha.tar", data)
    fb.ensure_outside_backup(tmp_path / "jinde" / "zaloha.tar", data)
    with pytest.raises(ValueError, match="zálohovaných datech"):
        fb.ensure_outside_backup(data / "backup" / "zaloha.tar", data)


def test_meta_rezim_zapise_jen_meta_a_manifest_s_daty(data_dir: Path, tmp_path: Path) -> None:
    meta = tmp_path / "meta"
    dump = meta / fb.DUMP_NAME
    dump.parent.mkdir(parents=True)
    dump.write_bytes(DUMP_BYTES)
    env = _environ()
    info = fb.source_info(env, NOW, "16.4")
    files = fb.scan_data(data_dir, NOW, live_dir=None)
    package = fb.build_package(data_files=files, dump=dump, info=info, environ=env, mode="meta")
    fb.write_meta(package, meta)
    written = {p.relative_to(meta).as_posix() for p in meta.rglob("*") if p.is_file()}
    assert written == {
        fb.RESTORE_NAME,
        fb.ENV_TEMPLATE_NAME,
        fb.SECRET_KEYS_NAME,
        fb.DUMP_NAME,
        fb.MANIFEST_NAME,
    }
    manifest = json.loads((meta / fb.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["mode"] == "meta"
    listed = {item["path"]: item["sha256"] for item in manifest["files"]}
    body = (data_dir / COLD_FILE).read_bytes()
    assert listed[fb.DATA_PREFIX + COLD_FILE] == hashlib.sha256(body).hexdigest()
    assert listed[fb.DUMP_NAME] == hashlib.sha256(DUMP_BYTES).hexdigest()
    for path in meta.rglob("*"):
        if path.is_file():
            assert MARKER.encode() not in path.read_bytes()


# ── pg_dump do souboru ────────────────────────────────────────────────


def _fake_dump(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    # Poslední prvek = jméno databáze; `--file=` se vkládá před něj
    monkeypatch.setattr(fb, "dump_command", lambda url: [sys.executable, "-c", script, "db"])


async def test_dump_do_souboru(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_dump(
        monkeypatch,
        "import sys; open(sys.argv[1].split('=', 1)[1], 'wb').write(b'PGDMP-ok')",
    )
    target = tmp_path / "gexlens.dump"
    await fb.dump_to_file("postgresql+psycopg://u:p@h/db", target)
    assert target.read_bytes() == b"PGDMP-ok"


async def test_neuspesny_dump_je_chyba_a_soubor_zmizi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_dump(
        monkeypatch,
        "import sys; open(sys.argv[1].split('=', 1)[1], 'wb').write(b'x');"
        "sys.stderr.write('pg_dump: chyba'); sys.exit(3)",
    )
    target = tmp_path / "gexlens.dump"
    with pytest.raises(RuntimeError, match="kódem 3"):
        await fb.dump_to_file("postgresql+psycopg://u:p@h/db", target)
    assert not target.exists()


async def test_odpojeni_klienta_ukonci_pg_dump(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_dump(monkeypatch, "import time; time.sleep(60)")
    processes: list[Any] = []
    original = asyncio.create_subprocess_exec

    async def recording(*args: Any, **kwargs: Any) -> Any:
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    cancel = threading.Event()
    asyncio.get_running_loop().call_later(0.3, cancel.set)
    with pytest.raises(fb.BackupCancelled):
        await asyncio.wait_for(
            fb.dump_to_file("postgresql+psycopg://u:p@h/db", tmp_path / "d", cancel), 20
        )
    assert processes and processes[0].returncode is not None


# ── Endpoint ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dump(url: str, target: Path, cancel: threading.Event | None = None) -> None:
        target.write_bytes(DUMP_BYTES)

    monkeypatch.setattr(fb, "dump_to_file", fake_dump)
    monkeypatch.setattr(fb, "pg_server_version", lambda url: "16.4")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pg_dump")


def _app(data_dir: Path, token: str | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    guard = build_token_guard(token) if token is not None else None
    app.include_router(
        fb.build_full_backup_router("postgresql+psycopg://u:p@h/db", data_dir, guard, _environ())
    )
    return app


@pytest.mark.usefixtures("fake_sources")
def test_endpoint_streamuje_tar_bez_gzip(data_dir: Path) -> None:
    with TestClient(_app(data_dir)) as client:
        response = client.get("/backup/full", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "identity"
        assert int(response.headers["content-length"]) == len(response.content)
        disposition = response.headers["content-disposition"]
        assert re.search(r'gexlens-zaloha-\d{4}-\d{2}-\d{2}_\d{4}-0123456\.tar"', disposition)
        members = _members(response.content)
        assert fb.DATA_PREFIX + COLD_FILE in members
        assert MARKER.encode() not in response.content
        # Úklid a uvolnění zámku: další záloha jde hned a pracovní adresář zmizel
        assert client.get("/backup/full").status_code == 200
    assert not any(fb.work_root(data_dir).glob(f"{fb.RUN_PREFIX}*"))


@pytest.mark.usefixtures("fake_sources")
def test_soubezna_zaloha_dostane_409(data_dir: Path) -> None:
    assert fb._RUN_LOCK.acquire(blocking=False)
    try:
        with TestClient(_app(data_dir)) as client:
            assert client.get("/backup/full").status_code == 409
    finally:
        fb._RUN_LOCK.release()


@pytest.mark.usefixtures("fake_sources")
def test_endpoint_za_tokenem(data_dir: Path) -> None:
    with TestClient(_app(data_dir, token="tajne")) as client:
        assert client.get("/backup/full").status_code == 401
        ok = client.get("/backup/full", headers={"X-GEXLens-Token": "tajne"})
        assert ok.status_code == 200


@pytest.mark.usefixtures("fake_sources")
def test_selhani_pripravy_uvolni_zamek_a_uklidi(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> fb.Package:
        raise OSError("disk plný")

    monkeypatch.setattr(fb, "prepare_full", broken)
    with TestClient(_app(data_dir), raise_server_exceptions=False) as client:
        response = client.get("/backup/full")
        assert response.status_code == 500
        assert "disk" not in response.text  # detail jen v logu
    assert fb._RUN_LOCK.acquire(blocking=False)
    fb._RUN_LOCK.release()
    assert not any(fb.work_root(data_dir).glob(f"{fb.RUN_PREFIX}*"))


async def test_odpojeni_pri_hlavickach_uklidi_i_bez_spusteni_tela() -> None:
    """Spadne-li spojení už na hlavičkách, Starlette tělo nečte — úklid musí proběhnout."""
    cleaned: list[bool] = []
    started: list[bool] = []

    async def body() -> AsyncGenerator[bytes, None]:
        started.append(True)
        yield b"x"

    async def cleanup() -> None:
        cleaned.append(True)

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        raise OSError("spojení spadlo")

    response = fb._BackupResponse(body(), cleanup, media_type=fb.TAR_MEDIA_TYPE)
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)  # type: ignore[arg-type]
    assert cleaned == [True]
    assert started == []


def test_zaloha_postgres_bez_gzip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dump -Fc je komprimovaný; GZipMiddleware ho nemá balit znovu (#1799)."""

    async def fake_stream(url: str) -> AsyncGenerator[bytes, None]:
        yield b"PGDMP" * 1000

    monkeypatch.setattr(backup, "stream_dump", fake_stream)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pg_dump")
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.include_router(backup.build_backup_router("postgresql+psycopg://u:p@h/db"))
    with TestClient(app) as client:
        response = client.get("/backup/postgres", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "identity"
    assert response.content == b"PGDMP" * 1000


# ── CLI ───────────────────────────────────────────────────────────────


@pytest.fixture
def cli_settings(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    import gexlens_engine.config as config

    settings = SimpleNamespace(data_dir=data_dir, database_url="postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    for key, value in _environ().items():
        if key != "PATH":
            monkeypatch.setenv(key, value)


@pytest.mark.usefixtures("fake_sources", "cli_settings")
def test_cli_out_zapise_balicek(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = data_dir / "local" / "zaloha.tar"
    assert fb.main(["--out", str(out)]) == 0
    members = _members(out.read_bytes())
    assert fb.MANIFEST_NAME in members and fb.DATA_PREFIX + LIVE_FILE in members
    assert MARKER.encode() not in out.read_bytes()
    assert not any(fb.work_root(data_dir).glob(f"{fb.RUN_PREFIX}*"))
    assert not out.with_name(out.name + fb.TMP_SUFFIX).exists()


@pytest.mark.usefixtures("fake_sources", "cli_settings")
def test_cli_out_zalozi_chybejici_adresar(data_dir: Path) -> None:
    """Na PC `data/local/` zatím neexistuje — CLI ho založí, ne až po celém dumpu spadne."""
    shutil.rmtree(data_dir / "local")
    out = data_dir / "local" / "zaloha.tar"
    assert fb.main(["--out", str(out)]) == 0
    assert fb.MANIFEST_NAME in _members(out.read_bytes())


@pytest.mark.usefixtures("fake_sources", "cli_settings")
def test_cli_meta_dir(data_dir: Path, tmp_path: Path) -> None:
    meta = tmp_path / "meta"
    assert fb.main(["--meta-dir", str(meta)]) == 0
    assert (meta / fb.DUMP_NAME).read_bytes() == DUMP_BYTES
    assert json.loads((meta / fb.MANIFEST_NAME).read_text(encoding="utf-8"))["mode"] == "meta"
    assert not (meta / "data").exists()


@pytest.mark.usefixtures("fake_sources", "cli_settings")
def test_cli_odmitne_cil_v_zalohovanych_datech(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fb.main(["--out", str(data_dir / "backup" / "zaloha.tar")]) == 1
    assert "zálohovaných datech" in capsys.readouterr().err
