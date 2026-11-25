"""Obnova ze zálohy jedním příkazem (F3 #1800).

Jednotkové testy podvrhují PG vrstvu (`pg_has_tables`, `pg_restore`,
`safety_dump`, `swap_database`). Integrační testy se značkou `pg_only` běží
proti skutečnému PostgreSQL (`GEXLENS_TEST_PG_DSN`, CI služba `postgres:16`)
s klientem `pg_dump` / `pg_restore` na PATH.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import tarfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

import gexlens_api.full_backup as fb
import gexlens_api.restore as restore
from gexlens_engine.storage.meta import ensure_meta_schema

NOW = dt.datetime(2026, 11, 25, 14, 30, tzinfo=dt.UTC)
SHA = "0123456789abcdef0123456789abcdef01234567"
DUMP_BYTES = b"PGDMP" + bytes(range(256)) * 40
FAKE_URL = "postgresql+psycopg://gexlens:x@localhost:5432/gexlens"
LIVE = "snapshots/ES/20261218/2026-11-25.parquet"
COLD = "snapshots/ES/20261218/2026-11-20.parquet"
SOURCE_FILES = {
    LIVE: True,
    COLD: False,
    "derived/ES/2026-11-24.parquet": False,
    "reports/tasty-kpi.jsonl": True,
    "quarantine/sync-2026-11-20/x.parquet": False,
}
# Zdroj je má, do balíčku nejdou (ADR-0029, #1847/#1848)
SOURCE_EXCLUDED = ("ticks/ES/2026-11-25.parquet", "logs/engine.log", "local/instance.json")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write(path: Path, body: bytes, mtime: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def _source_data(root: Path) -> Path:
    for rel, live in SOURCE_FILES.items():
        mtime = NOW - (dt.timedelta(minutes=5) if live else dt.timedelta(days=3))
        _write(root / rel, f"obsah {rel}".encode() * 40, mtime)
    for rel in SOURCE_EXCLUDED:
        _write(root / rel, b"jen zdroj", NOW)
    return root


def _source_env() -> dict[str, str]:
    return {
        "GEXLENS_BUILD_SHA": SHA,
        "GEXLENS_API_TOKEN": "tajne-zdroje",
        "GEXLENS_PG_PASSWORD": "tajne-pg",
        "GEXLENS_SYMBOLS": "ES",
    }


def _package(tmp_path: Path, source: Path) -> Path:
    env = _source_env()
    run_dir = fb.new_run_dir(source, NOW)
    (run_dir / "gexlens.dump").write_bytes(DUMP_BYTES)
    package = fb.prepare_full(source, run_dir, fb.source_info(env, NOW, "16.4"), env)
    out = tmp_path / "zaloha.tar"
    with open(out, "wb") as handle:
        for chunk in fb.iter_package(package):
            handle.write(chunk)
    shutil.rmtree(run_dir)
    return out


def _hashes(root: Path) -> dict[str, str]:
    """Obsah `data/` tak, jak ho vidí záloha (bez `local/`, `logs/`, `ticks/`…)."""
    return {
        item.rel: _sha(item.source.read_bytes()) for item in fb.scan_data(root, NOW, live_dir=None)
    }


@dataclass
class FakePg:
    has_tables: bool = False
    restores: list[tuple[str, bytes, bool]] = field(default_factory=list)
    safety: list[str] = field(default_factory=list)
    swaps: list[bytes] = field(default_factory=list)


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> FakePg:
    fake = FakePg()
    monkeypatch.setattr(restore, "_require_tool", lambda name: None)
    monkeypatch.setattr(restore, "pg_has_tables", lambda url: fake.has_tables)

    def fake_restore(url: str, database: str, dump: Path, *, clean: bool) -> None:
        fake.restores.append((database, dump.read_bytes(), clean))

    def fake_safety(url: str, data_dir: Path, stamp: str) -> Path:
        path = data_dir / restore.SAFETY_DIR / f"{stamp}.dump"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PGDMP-pojistka")
        fake.safety.append(stamp)
        return path

    def fake_swap(url: str, dump: Path, stamp: str) -> restore.SwapResult:
        fake.swaps.append(dump.read_bytes())
        return restore.SwapResult(f"gexlens_pred_{stamp}", [f"gexlens_pred_{stamp}"])

    monkeypatch.setattr(restore, "pg_restore", fake_restore)
    monkeypatch.setattr(restore, "safety_dump", fake_safety)
    monkeypatch.setattr(restore, "swap_database", fake_swap)
    return fake


def _restore(source: Path, target: Path, **kwargs: object) -> restore.Report:
    environ = kwargs.pop("environ", {"GEXLENS_BUILD_SHA": SHA})
    assert isinstance(environ, dict)
    return restore.run_restore(
        source,
        database_url=FAKE_URL,
        data_dir=target,
        environ=environ,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _all_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


# ── Balíček do prázdného cíle ─────────────────────────────────────────


def test_balicek_obnovi_data_a_preda_dump(tmp_path: Path, fake_pg: FakePg) -> None:
    source = _source_data(tmp_path / "zdroj")
    package = _package(tmp_path, source)
    target = tmp_path / "cil"

    report = _restore(package, target)

    assert _hashes(target) == _hashes(source)
    assert set(_hashes(target)) == set(SOURCE_FILES)
    assert fake_pg.restores == [("gexlens", DUMP_BYTES, True)]
    assert fake_pg.safety == []  # prázdný cíl nepotřebuje pojistku
    assert report.written == len(SOURCE_FILES)
    assert report.warnings == []
    # mtime ze zálohy: jinak by příští záloha brala celý archiv jako živé soubory
    restored = (target / COLD).stat().st_mtime
    assert abs(restored - (NOW - dt.timedelta(days=3)).timestamp()) < 1
    # pracovní adresář s dumpem je pryč, *.tmp nezůstaly
    assert not list((target / fb.WORK_DIR).glob("*"))
    assert not [path for path in _all_files(target) if path.name.endswith(fb.TMP_SUFFIX)]


def test_zmeneny_bajt_skonci_pred_jakymkoli_zapisem(tmp_path: Path, fake_pg: FakePg) -> None:
    source = _source_data(tmp_path / "zdroj")
    package = _package(tmp_path, source)
    blob = bytearray(package.read_bytes())
    # Poslední datový člen: chyba se pozná až po přečtení všeho předchozího
    index = blob.find(b"obsah snapshots/ES/20261218/2026-11-25.parquet")
    assert index > 0
    blob[index + 3] ^= 0x01
    package.write_bytes(bytes(blob))
    target = tmp_path / "cil"

    with pytest.raises(restore.RestoreError, match="nesouhlasí.*beze změny"):
        _restore(package, target)

    assert fake_pg.restores == []
    assert fake_pg.safety == []
    assert _all_files(target) == []


def test_zmeneny_bajt_dumpu_skonci_pred_obnovou_pg(tmp_path: Path, fake_pg: FakePg) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    blob = bytearray(package.read_bytes())
    blob[blob.find(DUMP_BYTES) + 100] ^= 0x01
    package.write_bytes(bytes(blob))

    with pytest.raises(restore.RestoreError, match="postgres/gexlens.dump: sha256"):
        _restore(package, tmp_path / "cil")
    assert fake_pg.restores == []


@pytest.mark.parametrize("cut", ["0.1", "0.5", "0.9", "manifest"])
def test_useknuty_balicek_je_chyba(tmp_path: Path, fake_pg: FakePg, cut: str) -> None:
    """Useknutí v hlavičce, uprostřed dat členu i v manifestu (přerušené stahování).

    Chybějící koncové nulové bloky taru chybou nejsou: obsah je celý a ověřený.
    """
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    blob = package.read_bytes()
    if cut == "manifest":
        end = blob.rfind(b'"format": "gexlens-zaloha"') + 10
    else:
        end = int(len(blob) * float(cut))
    package.write_bytes(blob[:end])

    with pytest.raises(restore.RestoreError, match="neúplný"):
        _restore(package, tmp_path / "cil")
    assert fake_pg.restores == []


def _crafted(tmp_path: Path, members: dict[str, bytes]) -> Path:
    base = {fb.SECRET_KEYS_NAME: b"# tajemstvi\n", fb.DUMP_NAME: DUMP_BYTES}
    files = {**base, **members}
    document = {
        "format": fb.FORMAT_NAME,
        "format_version": fb.FORMAT_VERSION,
        "mode": "full",
        "created_utc": "2026-11-25T14:30:00Z",
        "source_host": "test",
        "image_sha": SHA,
        "files": [
            {"path": name, "size": len(body), "sha256": _sha(body), "live": False}
            for name, body in files.items()
        ],
    }
    out = tmp_path / "upraveny.tar"
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, body in [*files.items(), (fb.MANIFEST_NAME, json.dumps(document).encode())]:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return out


@pytest.mark.parametrize(
    ("member", "message"),
    [
        ("data/../mimo.txt", "Nebezpečná cesta"),
        ("/etc/passwd", "Nebezpečná cesta"),
        ("data/local/instance_settings.json", "nikdy nebere"),
        ("data/snapshots/x.parquet.tmp", "nikdy nebere"),
        ("skript.sh", "Neznámý soubor"),
    ],
)
def test_cizi_cesty_v_balicku_se_odmitnou(
    tmp_path: Path, fake_pg: FakePg, member: str, message: str
) -> None:
    package = _crafted(tmp_path, {member: b"x"})
    target = tmp_path / "cil"

    with pytest.raises(restore.RestoreError, match=message):
        _restore(package, target)
    assert _all_files(target) == []
    assert not (tmp_path / "mimo.txt").exists()


def test_neznama_verze_formatu_se_odmitne(tmp_path: Path, fake_pg: FakePg) -> None:
    root = tmp_path / "snimek"
    root.mkdir()
    (root / fb.MANIFEST_NAME).write_text(
        json.dumps({"format": fb.FORMAT_NAME, "format_version": 99, "mode": "full", "files": []})
    )
    with pytest.raises(restore.RestoreError, match="verze 99"):
        _restore(root, tmp_path / "cil")


# ── Pojistka cíle ─────────────────────────────────────────────────────


def test_cil_s_daty_bez_overwrite_skonci_jeste_pred_overenim(
    tmp_path: Path, fake_pg: FakePg, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    target = tmp_path / "cil"
    _write(target / "snapshots/ES/20261218/2026-11-26.parquet", b"novejsi", NOW)

    def never(path: Path) -> restore.Verification:
        raise AssertionError("ověření ~14 GB nemá běžet, když chybí --overwrite")

    monkeypatch.setattr(restore, "verify_tar", never)
    with pytest.raises(restore.RestoreError, match="data/ má soubory.*--overwrite"):
        _restore(package, target)
    fake_pg.has_tables = True
    (target / "snapshots/ES/20261218/2026-11-26.parquet").unlink()
    with pytest.raises(restore.RestoreError, match="PG má tabulky.*--overwrite"):
        _restore(package, target)
    assert fake_pg.restores == []


def test_overwrite_udela_pojistny_dump_a_soubory_navic_da_do_karanteny(
    tmp_path: Path, fake_pg: FakePg
) -> None:
    source = _source_data(tmp_path / "zdroj")
    package = _package(tmp_path, source)
    target = tmp_path / "cil"
    extra = "snapshots/ES/20261218/2026-11-26.parquet"
    _write(target / extra, b"novejsi data cile", NOW)
    _write(target / COLD, b"jina verze", NOW)
    # Už v karanténě a části, které záloha nebere: zůstávají, jak jsou
    kept = ("quarantine/sync-2026-11-01/stara.parquet", "logs/engine.log", "ticks/ES/x.parquet")
    for rel in kept:
        _write(target / rel, b"cil", NOW)
    _write(target / restore.INSTANCE_SETTINGS, b'{"ibkr_host": "cil"}', NOW)
    fake_pg.has_tables = True

    report = _restore(package, target, overwrite=True)

    assert report.safety_dump == target / restore.SAFETY_DIR / "20261125_143000.dump"
    assert report.safety_dump.is_file()
    assert fake_pg.restores == [("gexlens", DUMP_BYTES, True)]
    assert (target / COLD).read_bytes() == (source / COLD).read_bytes()
    # Rozhodnuto 19. 11. v #1800: soubor, který zdroj nemá, se nemaže ani nezůstává na místě
    assert report.quarantined == [extra]
    assert report.quarantine_dir == "quarantine/sync-2026-11-25"
    assert not (target / extra).exists()
    moved = target / "quarantine/sync-2026-11-25" / extra
    assert moved.read_bytes() == b"novejsi data cile"
    assert all((target / rel).read_bytes() == b"cil" for rel in kept)
    assert (target / restore.INSTANCE_SETTINGS).read_bytes() == b'{"ibkr_host": "cil"}'
    assert "karanténa: 1 souborů" in restore.render_report(report)


def test_karantena_druhy_beh_tehoz_dne_neprepise_presunuty_soubor(tmp_path: Path) -> None:
    data = tmp_path / "data"
    rel = "derived/ES/2026-11-26.parquet"
    _write(data / "quarantine/sync-2026-11-25" / rel, b"prvni", NOW)
    _write(data / rel, b"druhy", NOW)

    folder, moved = restore.quarantine_missing(data, set(), NOW)

    assert (folder, moved) == ("quarantine/sync-2026-11-25", [rel])
    assert (data / "quarantine/sync-2026-11-25" / rel).read_bytes() == b"prvni"
    second = data / "quarantine/sync-2026-11-25/derived/ES/2026-11-26.parquet.143000"
    assert second.read_bytes() == b"druhy"


def test_dump_za_daty_v_taru_se_odmitne_pred_zapisem(tmp_path: Path, fake_pg: FakePg) -> None:
    body = b"x"
    out = tmp_path / "poradi.tar"
    files = {
        fb.SECRET_KEYS_NAME: b"# t\n",
        "data/snapshots/ES/x.parquet": body,
        fb.DUMP_NAME: DUMP_BYTES,
    }
    document = {
        "format": fb.FORMAT_NAME,
        "format_version": fb.FORMAT_VERSION,
        "mode": "full",
        "image_sha": SHA,
        "files": [
            {"path": name, "size": len(data), "sha256": _sha(data), "live": False}
            for name, data in files.items()
        ],
    }
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in [*files.items(), (fb.MANIFEST_NAME, json.dumps(document).encode())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    with pytest.raises(restore.RestoreError, match="před postgres/gexlens.dump"):
        _restore(out, tmp_path / "cil")
    assert _all_files(tmp_path / "cil") == []


def test_stary_pracovni_adresar_zabite_obnovy_se_uklidi(tmp_path: Path, fake_pg: FakePg) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    target = tmp_path / "cil"
    stale = target / fb.WORK_DIR / "restore-20261123T100000Z-999"
    _write(stale / "gexlens.dump", DUMP_BYTES, NOW)
    foreign = target / fb.WORK_DIR / "cizi-adresar"
    foreign.mkdir(parents=True)

    _restore(package, target)

    assert not stale.exists()
    assert foreign.is_dir()  # cizí adresář se nemaže


def test_shodne_soubory_se_preskoci(tmp_path: Path, fake_pg: FakePg) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    target = tmp_path / "cil"
    _restore(package, target)
    fake_pg.has_tables = True

    report = _restore(package, target, overwrite=True)

    assert report.written == 0
    assert report.skipped == len(SOURCE_FILES)


def test_data_local_zustane_beze_zmeny(tmp_path: Path, fake_pg: FakePg) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    target = tmp_path / "cil"
    settings = target / restore.INSTANCE_SETTINGS
    _write(settings, b'{"ibkr_host": "cil"}', NOW - dt.timedelta(days=1))
    _write(target / "local/instance.json", b'{"role": "viewer"}', NOW - dt.timedelta(days=1))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in _all_files(target)}

    # data/local/ samo cíl s daty nedělá — nový server z bootstrapu ho má
    report = _restore(package, target)

    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}
    assert after == before
    assert not report.instance_settings_missing
    assert not (target / "local/pred-obnovou").exists()


def test_zdroj_v_zalohovanych_datech_cile_se_odmitne(tmp_path: Path, fake_pg: FakePg) -> None:
    target = tmp_path / "cil"
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    inside = target / "snapshots" / "zaloha.tar"
    inside.parent.mkdir(parents=True)
    shutil.copy(package, inside)

    with pytest.raises(restore.RestoreError, match="leží v data/ cíle"):
        _restore(inside, target)


def test_selhani_po_obnove_pg_je_rozpracovany_cil(
    tmp_path: Path, fake_pg: FakePg, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))

    def full_disk(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore._Apply, "write_data", full_disk)
    with pytest.raises(restore.PartialRestoreError, match="PG už je obnovená"):
        _restore(package, tmp_path / "cil")
    assert len(fake_pg.restores) == 1


def test_cli_rozpracovany_cil_vraci_2(
    tmp_path: Path,
    fake_pg: FakePg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    monkeypatch.chdir(tmp_path)  # bez .env
    monkeypatch.setenv("GEXLENS_DATA_DIR", str(tmp_path / "cil"))

    def full_disk(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore._Apply, "write_data", full_disk)
    assert restore.main([str(package)]) == 2
    assert "Obnova nedokončena" in capsys.readouterr().err


# ── Výměna DB (podvržená) ─────────────────────────────────────────────


def test_vymena_db_nedela_pojistny_dump(tmp_path: Path, fake_pg: FakePg) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))
    fake_pg.has_tables = True

    report = _restore(package, tmp_path / "cil", overwrite=True, swap_db=True)

    assert fake_pg.swaps == [DUMP_BYTES]
    assert fake_pg.restores == []
    assert report.safety_dump is None  # pojistkou je přejmenovaná DB
    assert report.swap is not None
    assert "gexlens_pred_20261125_143000" in restore.render_report(report)


# ── Adresář (rozbalený balíček, snímek restic) ────────────────────────


def _meta_dir(tmp_path: Path, source: Path) -> Path:
    """`--meta-dir` + `data/` jako po restic restore (bez vyloučených částí)."""
    env = _source_env()
    root = tmp_path / "snimek"
    dump = root / fb.DUMP_NAME
    dump.parent.mkdir(parents=True)
    dump.write_bytes(DUMP_BYTES)
    files = fb.scan_data(source, NOW, live_dir=None)
    package = fb.build_package(
        data_files=files, dump=dump, info=fb.source_info(env, NOW, "16.4"), environ=env, mode="meta"
    )
    fb.write_meta(package, root)
    shutil.copytree(source, root / "data")
    return root


def test_adresar_meta_zivy_soubor_a_novy_soubor_jsou_upozorneni(
    tmp_path: Path, fake_pg: FakePg
) -> None:
    root = _meta_dir(tmp_path, _source_data(tmp_path / "zdroj"))
    # restic bral data/ až po manifestu: dnešní partice mezitím narostla a přibyl soubor
    with open(root / "data" / LIVE, "ab") as handle:
        handle.write(b"dalsi minuta")
    _write(root / "data/derived/ES/2026-11-25.parquet", b"po manifestu", NOW)
    target = tmp_path / "cil"

    report = _restore(root, target)

    assert any(LIVE in warning and "velikost" in warning for warning in report.warnings)
    assert any("2026-11-25.parquet: není v manifestu" in warning for warning in report.warnings)
    assert (target / LIVE).read_bytes().endswith(b"dalsi minuta")
    assert (target / "derived/ES/2026-11-25.parquet").read_bytes() == b"po manifestu"
    assert fake_pg.restores == [("gexlens", DUMP_BYTES, True)]
    # data/local/ zdroje se do cíle nedostane, ani když ho snímek omylem má
    assert not (target / "local").exists()


def test_adresar_meta_zmeneny_stary_soubor_je_chyba(tmp_path: Path, fake_pg: FakePg) -> None:
    root = _meta_dir(tmp_path, _source_data(tmp_path / "zdroj"))
    (root / "data" / COLD).write_bytes(b"poskozeno")

    with pytest.raises(restore.RestoreError, match=f"data/{COLD}"):
        _restore(root, tmp_path / "cil")
    assert fake_pg.restores == []


def test_adresar_bez_data_je_chyba(tmp_path: Path, fake_pg: FakePg) -> None:
    root = _meta_dir(tmp_path, _source_data(tmp_path / "zdroj"))
    shutil.rmtree(root / "data")

    with pytest.raises(restore.RestoreError, match="chybí data/"):
        _restore(root, tmp_path / "cil")


def test_rozbaleny_balicek_jako_adresar(tmp_path: Path, fake_pg: FakePg) -> None:
    source = _source_data(tmp_path / "zdroj")
    package = _package(tmp_path, source)
    root = tmp_path / "rozbaleny"
    with tarfile.open(package) as archive:
        archive.extractall(root, filter="data")
    target = tmp_path / "cil"

    _restore(root, target)

    assert _hashes(target) == _hashes(source)


# ── Výpis ─────────────────────────────────────────────────────────────


def test_chybejici_tajemstvi_jen_jmeny(tmp_path: Path, fake_pg: FakePg) -> None:
    package = _package(tmp_path, _source_data(tmp_path / "zdroj"))

    report = _restore(
        package, tmp_path / "cil", environ={"GEXLENS_BUILD_SHA": SHA, "GEXLENS_PG_PASSWORD": "c"}
    )

    assert report.missing_secrets == ["GEXLENS_API_TOKEN"]
    rendered = restore.render_report(report)
    assert "GEXLENS_API_TOKEN" in rendered
    assert "tajne-zdroje" not in rendered
    assert "instance_settings.json" in rendered


def test_revize_image() -> None:
    assert restore.image_warning(SHA, {"GEXLENS_BUILD_SHA": SHA}) is None
    older = restore.image_warning(SHA, {"GEXLENS_BUILD_SHA": "f" * 40})
    assert older is not None and "GEXLENS_IMAGE_TAG=sha-0123456" in older
    local = restore.image_warning(SHA, {})
    assert local is not None and "lokální build" in local


def test_cli_chybejici_zdroj(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # bez .env
    monkeypatch.setenv("GEXLENS_DATA_DIR", str(tmp_path / "data"))

    assert restore.main([str(tmp_path / "neni.tar")]) == 1
    assert "neexistuje" in capsys.readouterr().err


# ── Integrace s PostgreSQL ────────────────────────────────────────────

PG_DSN = os.environ.get("GEXLENS_TEST_PG_DSN", "")
pg_only = pytest.mark.skipif(
    not PG_DSN, reason="GEXLENS_TEST_PG_DSN nenastaveno (integrace s reálným PostgreSQL)"
)


@dataclass
class PgDatabases:
    prefix: str
    create: Callable[[str], str]  # jméno → DSN
    names: Callable[[], list[str]]


@pytest.fixture
def pg_dbs() -> Iterator[PgDatabases]:
    # V CI klient nainstaluje job; chybějící klient je chyba, ne přeskočený test
    assert shutil.which("pg_dump") and shutil.which("pg_restore"), "chybí klient PostgreSQL"
    base = make_url(PG_DSN)
    prefix = f"rt{uuid.uuid4().hex[:8]}"
    admin = create_engine(
        base.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )

    def names() -> list[str]:
        with admin.connect() as connection:
            rows = connection.execute(text("SELECT datname FROM pg_database")).scalars()
            return sorted(str(name) for name in rows if str(name).startswith(prefix))

    def create(name: str) -> str:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        return base.set(database=name).render_as_string(hide_password=False)

    try:
        yield PgDatabases(prefix, create, names)
    finally:
        # Jen vlastní DB testu (prefix), nic cizího
        for name in names():
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _seed_database(url: str) -> None:
    engine = create_engine(url, poolclass=NullPool)
    try:
        ensure_meta_schema(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE restore_probe (id bigint GENERATED ALWAYS AS IDENTITY "
                    "PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO restore_probe (payload) "
                    "SELECT jsonb_build_object('i', g) FROM generate_series(1, 500) g"
                )
            )
            connection.execute(text("CREATE INDEX restore_probe_ts ON restore_probe (ts)"))
    finally:
        engine.dispose()


def _counts(url: str) -> dict[str, int]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' "
                    "AND table_schema NOT IN ('pg_catalog', 'information_schema')"
                )
            ).all()
            counts: dict[str, int] = {}
            for schema, name in tables:
                query = text(f'SELECT count(*) FROM "{schema}"."{name}"')
                counts[f"{schema}.{name}"] = int(connection.execute(query).scalar_one())
            return counts
    finally:
        engine.dispose()


def _seed_parquets(root: Path) -> None:
    table = pa.table(
        {"strike": [6000.0 + i for i in range(200)], "gex": [i * 0.5 for i in range(200)]}
    )
    for rel in (
        "snapshots/ES/20261218/2026-11-20.parquet",
        "derived/ES/levels/2026-11-24.parquet",
        "trades/ES/2026-11-24.parquet",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, root / rel)
    _write(root / "reports/tasty-kpi.jsonl", b'{"rate_limited": 0}\n', NOW)
    _write(root / restore.INSTANCE_SETTINGS, b'{"ibkr_host": "zdroj"}', NOW)
    _write(root / "logs/engine.log", b"log zdroje", NOW)


def _backup(
    pg_dbs: PgDatabases, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    """Fixtura → balíček CLI z F2 (`python -m gexlens_api.full_backup --out`)."""
    source_url = pg_dbs.create(f"{pg_dbs.prefix}_src")
    _seed_database(source_url)
    source_data = tmp_path / "zdroj" / "data"
    _seed_parquets(source_data)
    monkeypatch.chdir(tmp_path)  # bez .env
    monkeypatch.setenv("GEXLENS_DATABASE_URL", source_url)
    monkeypatch.setenv("GEXLENS_DATA_DIR", str(source_data))
    monkeypatch.setenv("GEXLENS_BUILD_SHA", SHA)
    monkeypatch.setenv("GEXLENS_API_TOKEN", "tajne-zdroje")
    package = tmp_path / "zaloha.tar"
    assert fb.main(["--out", str(package)]) == 0
    monkeypatch.delenv("GEXLENS_API_TOKEN")
    return source_url, package


@pg_only
def test_obnova_na_cistem_stroji_shodne_pocty_a_sha256(
    pg_dbs: PgDatabases,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_url, package = _backup(pg_dbs, tmp_path, monkeypatch)
    target_url = pg_dbs.create(f"{pg_dbs.prefix}_dst")
    target = tmp_path / "cil" / "data"
    instance = target / "local" / "instance.json"
    _write(instance, b'{"role": "viewer"}', NOW)
    instance_before = (instance.read_bytes(), instance.stat().st_mtime_ns)
    monkeypatch.setenv("GEXLENS_DATABASE_URL", target_url)
    monkeypatch.setenv("GEXLENS_DATA_DIR", str(target))

    # Jeden příkaz, prázdná DB a prázdné data/
    assert restore.main([str(package)]) == 0

    output = capsys.readouterr().out
    source_counts = _counts(source_url)
    assert sum(source_counts.values()) > 500
    assert _counts(target_url) == source_counts
    assert _hashes(target) == _hashes(tmp_path / "zdroj" / "data")
    assert (instance.read_bytes(), instance.stat().st_mtime_ns) == instance_before
    assert not (target / restore.INSTANCE_SETTINGS).exists()  # nastavení stroje zdroje ne
    assert not (target / "logs").exists()
    assert "Obnova hotová" in output
    assert "GEXLENS_API_TOKEN" in output
    assert "tajne-zdroje" not in output

    # Cíl už má data: bez --overwrite konec, s ním pojistný dump a shodný výsledek
    assert restore.main([str(package)]) == 1
    report = restore.run_restore(
        package,
        database_url=target_url,
        data_dir=target,
        environ={"GEXLENS_BUILD_SHA": SHA},
        overwrite=True,
    )
    assert report.safety_dump is not None
    assert report.safety_dump.parent == target / restore.SAFETY_DIR
    restore.check_dump_readable(report.safety_dump, target_url)
    assert report.skipped == len(_hashes(target)) and report.written == 0
    assert _counts(target_url) == source_counts


def _old_content(url: str) -> None:
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE jedinecna_data_cile (x int)"))
            connection.execute(text("INSERT INTO jedinecna_data_cile VALUES (1)"))
    finally:
        engine.dispose()


@pg_only
def test_vymena_db_necha_pojistne_db(
    pg_dbs: PgDatabases, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url, package = _backup(pg_dbs, tmp_path, monkeypatch)
    target = f"{pg_dbs.prefix}_dst"
    target_url = pg_dbs.create(target)
    _old_content(target_url)
    data = tmp_path / "cil" / "data"
    stamps = [NOW + dt.timedelta(minutes=minute) for minute in range(3)]

    reports = [
        restore.run_restore(
            package,
            database_url=target_url,
            data_dir=data,
            environ={"GEXLENS_BUILD_SHA": SHA},
            overwrite=True,
            swap_db=True,
            now=stamp,
        )
        for stamp in stamps
    ]

    assert _counts(target_url) == _counts(source_url)
    first = f"{target}_pred_{stamps[0]:%Y%m%d_%H%M%S}"
    assert reports[0].swap is not None and reports[0].swap.previous == first
    assert reports[0].safety_dump is None
    # Jedinečná data cíle přežila v pojistné DB
    assert _counts(base_with(target_url, first)) == {"public.jedinecna_data_cile": 1}
    # Bez dumpu v repu restic (F7a) se neodstraní žádná, ani starší než 2 poslední
    expected = [f"{target}_pred_{stamp:%Y%m%d_%H%M%S}" for stamp in stamps]
    assert reports[-1].swap is not None and reports[-1].swap.kept == expected
    assert [name for name in pg_dbs.names() if "_sync_" in name] == []


def base_with(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


@pg_only
def test_vymena_prerusena_pred_prejmenovanim_necha_cil_beze_zmeny(
    pg_dbs: PgDatabases, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, package = _backup(pg_dbs, tmp_path, monkeypatch)
    target = f"{pg_dbs.prefix}_dst"
    target_url = pg_dbs.create(target)
    _old_content(target_url)
    data = tmp_path / "cil" / "data"

    def interrupted(*args: object) -> None:
        raise restore.RestoreError("přerušeno před přejmenováním")

    monkeypatch.setattr(restore, "swap_names", interrupted)
    with pytest.raises(restore.RestoreError, match="přerušeno"):
        restore.run_restore(
            package,
            database_url=target_url,
            data_dir=data,
            environ={"GEXLENS_BUILD_SHA": SHA},
            overwrite=True,
            swap_db=True,
            now=NOW,
        )

    assert _counts(target_url) == {"public.jedinecna_data_cile": 1}
    assert pg_dbs.names() == sorted([f"{pg_dbs.prefix}_src", target])
    assert _hashes(data) == {}  # data/ se po selhání PG nezačalo zapisovat
