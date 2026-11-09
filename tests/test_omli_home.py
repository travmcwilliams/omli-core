"""Every default write lands under OMLI_HOME, or the current directory when it is unset."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omli.agents.reflector import Lesson, MemoryStore
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.connections import authoring, secrets
from omli.events.bus import EventBus
from omli.home import omli_home
from omli.integrity import __main__ as cli
from omli.integrity.anchor import DEFAULT_ANCHOR, anchor_head
from omli.integrity.keyring import DEFAULT_KEYRING, KeyRing
from omli.integrity.signer import DEFAULT_PUBKEY, generate_keypair
from omli.integrity.verifier import seal
from omli.packs.promotion import draft_staging_pack

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_PARENT = Path(cli.__file__).resolve().parents[2]


@pytest.fixture(params=["set", "unset"])
def home(request, monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("OMLI_SECRETS_DIR", raising=False)
    if request.param == "set":
        target = tmp_path / "home"
        monkeypatch.setenv("OMLI_HOME", str(target))
        return target.resolve()
    monkeypatch.delenv("OMLI_HOME", raising=False)
    return work.resolve()


def _log(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    prov = Provenance(agent="A", pack_name="p", pack_version="1", source_refs=("doc://1",))
    ArtifactStore(log, bus=EventBus()).write(new_artifact("CanonicalInput", {"q": "x"}, prov))
    return log


def test_home_is_the_variable_or_the_current_directory(home):
    assert omli_home() == home
    assert cli.home_root() == home


def test_anchor_default_lands_under_home(home, tmp_path):
    log = _log(tmp_path)
    anchor_head(seal(log, tmp_path / "s.json"))
    assert (home / DEFAULT_ANCHOR).is_file()


def test_keyring_default_lands_under_home(home):
    from omli.integrity.keyring import KeyRecord

    KeyRing().append_key(KeyRecord("id-1", "pem", "t"))
    assert (home / DEFAULT_KEYRING).is_file()


def test_public_key_default_lands_under_home(home, tmp_path):
    pytest.importorskip("cryptography")
    generate_keypair("pw", priv_path=tmp_path / "seal.key")
    assert (home / DEFAULT_PUBKEY).is_file()


def test_staging_draft_default_lands_under_home(home, tmp_path):
    mem = MemoryStore(tmp_path / "lessons.jsonl")
    lesson = mem.append(Lesson(text="sample lesson text", source_artifact_ids=("a",)))
    mem.approve(lesson.id, actor="test-reviewer")
    out = draft_staging_pack(ROOT / "omli" / "defaults" / "example_pack.yaml", mem, lesson.id, promoted_by="test-reviewer")
    assert out.resolve().parent == home / "packs" / "staging"


def test_connection_draft_default_lands_under_home(home):
    result = authoring.save_draft(authoring.template())
    assert result["ok"]
    assert Path(result["path"]).resolve().parent == home / "var" / "connections" / "drafts"


def test_secret_file_under_home_is_read_only_when_the_variable_is_set(home, monkeypatch):
    monkeypatch.delenv("OMLI_SECRET_CONNECTIONS__SVC__KEY", raising=False)
    target = home / "var" / "secrets" / "connections" / "svc" / "key"
    target.parent.mkdir(parents=True)
    target.write_text("fake-value", encoding="utf-8")
    expected = "fake-value" if os.environ.get("OMLI_HOME") else None
    assert secrets.resolve_secret("secret://connections/svc/key") == expected


def _package_files():
    return {p for p in PACKAGE_PARENT.rglob("*")
            if p.is_file() and not {"__pycache__", ".pytest_cache"} & set(p.parts)}


def test_the_cli_writes_nothing_beside_the_package(home, tmp_path):
    log = home / "var" / "artifacts.jsonl"
    log.parent.mkdir(parents=True)
    log.write_bytes(_log(tmp_path).read_bytes())
    before = _package_files()
    proc = subprocess.run([sys.executable, "-m", "omli.integrity", "--no-sign", "seal"], cwd=Path.cwd(),
                          env=dict(os.environ), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (home / "var" / "artifacts.seal.json").is_file()
    assert (home / DEFAULT_ANCHOR).is_file()
    assert _package_files() == before


def test_integrity_default_paths_start_with_the_integrity_folder(home):
    for default in (DEFAULT_ANCHOR, DEFAULT_KEYRING, DEFAULT_PUBKEY):
        assert default.startswith("integrity/")
