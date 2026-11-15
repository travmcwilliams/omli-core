"""The default login-key path is built in code from Path.home(); no tilde literal remains.

The OMLI_UI_KEY override still wins, and a key path under OMLI_HOME is still refused,
whichever way it was reached.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "omli" / "ui"

# Making a key needs the ui extra's ed25519 support; with no extras these tests skip, like
# the other UI test modules, so a plain `pip install .` and `pytest` cannot fail on them.
needs_crypto = pytest.mark.skipif(importlib.util.find_spec("cryptography") is None,
                                  reason="needs the ui extra (cryptography)")


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "Fake Home Dir"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("OMLI_UI_KEY", raising=False)
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "Omli Home"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "keypath test passphrase")
    return home


def test_the_default_key_path_comes_from_path_home(fake_home):
    auth = _load("omli.ui.auth")
    assert auth.key_path() == (fake_home / ".omli" / "ui_login.key").resolve()


def test_no_tilde_path_literal_in_the_ui_package():
    files = sorted(UI_DIR.glob("*.py"))
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "~/" not in text and "~\\" not in text, path.name


@needs_crypto
def test_the_override_wins_over_the_default(fake_home, tmp_path, monkeypatch):
    auth = _load("omli.ui.auth")
    target = tmp_path / "Other Place" / "Login Key.pem"
    monkeypatch.setenv("OMLI_UI_KEY", str(target))
    assert auth.key_path() == target.resolve()
    priv, _ = auth.create_login_key()
    assert priv == target.resolve() and target.exists()
    assert not (fake_home / ".omli").exists()


@needs_crypto
def test_an_override_under_omli_home_is_refused(fake_home, tmp_path, monkeypatch):
    auth = _load("omli.ui.auth")
    inside = tmp_path / "Omli Home" / "nested" / "k.pem"
    monkeypatch.setenv("OMLI_UI_KEY", str(inside))
    with pytest.raises(ValueError):
        auth.create_login_key()
    assert not inside.exists()


@needs_crypto
def test_a_default_path_under_omli_home_is_refused(fake_home, monkeypatch):
    # OMLI_HOME pointed at the user's own .omli folder would put the default key inside it.
    auth = _load("omli.ui.auth")
    monkeypatch.setenv("OMLI_HOME", str(fake_home / ".omli"))
    with pytest.raises(ValueError):
        auth.create_login_key()
    assert not (fake_home / ".omli" / "ui_login.key").exists()
