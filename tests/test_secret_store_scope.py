"""File-based secret:// resolution needs OMLI_HOME or OMLI_SECRETS_DIR; the environment always works."""

from __future__ import annotations

from pathlib import Path

import pytest

from omli.connections import secrets

REF = "secret://connections/svc/key"
ENV = "OMLI_SECRET_CONNECTIONS__SVC__KEY"


def _plant(root: Path, value: str = "planted-value") -> Path:
    target = root / "var" / "secrets" / "connections" / "svc" / "key"
    target.parent.mkdir(parents=True)
    target.write_text(value, encoding="utf-8")
    return target


@pytest.fixture
def no_store(monkeypatch, tmp_path):
    for name in ("OMLI_HOME", "OMLI_SECRETS_DIR", ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(secrets, "_SECRETS_DIR", None)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_environment_variable_resolves_with_no_store(no_store, monkeypatch):
    monkeypatch.setenv(ENV, "from-env")
    assert secrets.resolve_secret(REF) == "from-env"
    assert secrets.secret_status(REF).source == "env"


def test_planted_current_directory_file_is_not_read_without_either_variable(no_store, monkeypatch):
    _plant(no_store)
    read = []
    real_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: read.append(self) or real_read_text(self, *a, **k))
    assert secrets.resolve_secret(REF) is None
    status = secrets.secret_status(REF)
    assert (status.present, status.source) == (False, "missing")
    assert read == []


def test_required_lookup_without_a_store_raises_and_names_both_variables(no_store):
    _plant(no_store)
    with pytest.raises(secrets.SecretNotFound) as info:
        secrets.resolve_secret(REF, required=True)
    assert "OMLI_SECRETS_DIR" in str(info.value)
    assert "OMLI_HOME" in str(info.value)
    assert "planted-value" not in str(info.value)


def test_file_resolves_under_omli_home(no_store, monkeypatch, tmp_path):
    home = tmp_path / "home"
    _plant(home, "home-value")
    monkeypatch.setenv("OMLI_HOME", str(home))
    assert secrets.resolve_secret(REF) == "home-value"
    assert secrets.secret_status(REF).source == "file"


def test_file_resolves_under_omli_secrets_dir(no_store, monkeypatch, tmp_path):
    store = tmp_path / "store"
    (store / "connections" / "svc").mkdir(parents=True)
    (store / "connections" / "svc" / "key").write_text("store-value", encoding="utf-8")
    monkeypatch.setenv("OMLI_SECRETS_DIR", str(store))
    assert secrets.resolve_secret(REF) == "store-value"


def test_secrets_dir_takes_precedence_over_home(no_store, monkeypatch, tmp_path):
    home = tmp_path / "home"
    _plant(home, "home-value")
    store = tmp_path / "store"
    (store / "connections" / "svc").mkdir(parents=True)
    (store / "connections" / "svc" / "key").write_text("store-value", encoding="utf-8")
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.setenv("OMLI_SECRETS_DIR", str(store))
    assert secrets.resolve_secret(REF) == "store-value"


def test_unsafe_reference_is_refused_with_no_store(no_store):
    with pytest.raises(ValueError):
        secrets.resolve_secret("secret://../outside")
