"""The login key must be passphrase-protected.

`init` refuses to make a key without a passphrase (empty or whitespace-only is refused) and
reads it with getpass, twice, unless the scripts-only variable OMLI_UI_KEY_PASSPHRASE is set.
`sign` fails cleanly on a wrong passphrase: exit 1, a one-line error, nothing on stdout, no
traceback. A key without a passphrase (as early development versions made them) is refused, never migrated
silently; `init --replace` makes a new key and revokes the old registration.
"""

from __future__ import annotations

import importlib
import json

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

ENV = "OMLI_UI_KEY_PASSPHRASE"


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "Omli Home"
    key = tmp_path / "User Profile" / ".omli" / "ui_login.key"
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.setenv("OMLI_UI_KEY", str(key))
    monkeypatch.delenv(ENV, raising=False)
    return home.resolve(), key.resolve()


@pytest.fixture
def auth(env):
    return _load("omli.ui.auth")


@pytest.fixture
def cli(env):
    return _load("omli.ui.__main__")


class Prompts:
    """A stand-in for getpass.getpass that answers from a list and records the prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt="Password: ", stream=None):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("getpass was called more times than expected")
        return self.answers.pop(0)


def _no_getpass(prompt="", stream=None):
    raise AssertionError("getpass must not be called when the variable is set")


def _registry(home):
    p = home / "ui" / "keys.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _write_legacy_key(auth, home, key):
    """An early development key: unencrypted PKCS8 on disk, its public half registered."""
    k = Ed25519PrivateKey.generate()
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pem = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    import hashlib
    pubkey_id = hashlib.sha256(raw).hexdigest()[:16]
    (home / "ui").mkdir(parents=True, exist_ok=True)
    with open(home / "ui" / "keys.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "key", "pubkey_id": pubkey_id, "pubkey_pem": pem,
                             "created_at": "2026-11-09T00:00:00+00:00"}) + "\n")
    return k, pubkey_id


# --- the library refuses a key without a passphrase ---------------------------------------

@pytest.mark.parametrize("bad", ["", " ", "   \t\n"])
def test_an_empty_or_blank_passphrase_is_refused(env, auth, bad):
    home, key = env
    # Our own refusal, not cryptography's: it rejects an empty passphrase by itself.
    with pytest.raises(ValueError, match="passphrase is required"):
        auth.create_login_key(passphrase=bad)
    assert not key.exists() and _registry(home) == []


def test_no_passphrase_at_all_is_refused(env, auth):
    home, key = env
    # Our own refusal: a missing passphrase must not fall back to an empty or no passphrase.
    with pytest.raises(ValueError, match="passphrase is required"):
        auth.create_login_key()
    assert not key.exists() and _registry(home) == []


def test_a_blank_passphrase_in_the_variable_is_refused(env, auth, monkeypatch):
    home, key = env
    monkeypatch.setenv(ENV, "  ")
    with pytest.raises(ValueError):
        auth.create_login_key()
    assert not key.exists()


# --- init: getpass twice by default, the variable is an opt-in for scripts -----------------

def test_init_asks_twice_with_getpass_and_encrypts(env, cli, auth, monkeypatch):
    home, key = env
    prompts = Prompts("a good passphrase", "a good passphrase")
    monkeypatch.setattr("getpass.getpass", prompts)
    assert cli.main(["init"]) == 0
    assert len(prompts.prompts) == 2
    assert b"ENCRYPTED PRIVATE KEY" in key.read_bytes()
    token = auth.sign_challenge("c", passphrase="a good passphrase")
    assert auth.verify_token(next(iter(auth.registered_keys().values())), "c", token)


def test_init_refuses_when_the_two_entries_differ(env, cli, monkeypatch, capsys):
    home, key = env
    monkeypatch.setattr("getpass.getpass", Prompts("one passphrase", "another passphrase"))
    assert cli.main(["init"]) == 1
    assert not key.exists() and _registry(home) == []
    err = capsys.readouterr().err
    assert "did not match" in err and "Traceback" not in err


@pytest.mark.parametrize("blank", ["", "   "])
def test_init_refuses_a_blank_passphrase_from_getpass(env, cli, monkeypatch, capsys, blank):
    home, key = env
    monkeypatch.setattr("getpass.getpass", Prompts(blank, blank))
    assert cli.main(["init"]) == 1
    assert not key.exists() and _registry(home) == []
    err = capsys.readouterr().err
    assert "passphrase is required" in err and "Traceback" not in err


def test_init_uses_the_variable_without_prompting(env, cli, monkeypatch):
    home, key = env
    monkeypatch.setenv(ENV, "a scripted passphrase")
    monkeypatch.setattr("getpass.getpass", _no_getpass)
    assert cli.main(["init"]) == 0
    assert b"ENCRYPTED PRIVATE KEY" in key.read_bytes()


def test_the_help_says_the_variable_is_for_scripts_and_stays_in_the_environment(cli, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split()).lower()
    assert ENV.lower() in text
    assert "scripts" in text and "stays in the process environment" in text


# --- sign: a wrong passphrase fails cleanly -----------------------------------------------

def _init(cli, monkeypatch, passphrase="the right passphrase"):
    monkeypatch.setenv(ENV, passphrase)
    assert cli.main(["init"]) == 0
    monkeypatch.delenv(ENV)


def test_sign_reads_the_passphrase_with_getpass(env, cli, auth, monkeypatch, capsys):
    _init(cli, monkeypatch)
    capsys.readouterr()
    prompts = Prompts("the right passphrase")
    monkeypatch.setattr("getpass.getpass", prompts)
    assert cli.main(["sign", "abc"]) == 0
    assert len(prompts.prompts) == 1
    token = capsys.readouterr().out.strip()
    assert auth.verify_token(next(iter(auth.registered_keys().values())), "abc", token)


@pytest.mark.parametrize("how", ["getpass", "variable"])
def test_sign_with_a_wrong_passphrase_fails_cleanly(env, cli, monkeypatch, capsys, how):
    _init(cli, monkeypatch)
    capsys.readouterr()
    if how == "getpass":
        monkeypatch.setattr("getpass.getpass", Prompts("the wrong passphrase"))
    else:
        monkeypatch.setenv(ENV, "the wrong passphrase")
        monkeypatch.setattr("getpass.getpass", _no_getpass)
    assert cli.main(["sign", "abc"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "wrong passphrase" in err.lower()
    assert "Traceback" not in err and len(err.strip().splitlines()) == 1


def test_the_library_raises_a_clean_error_on_a_wrong_passphrase(env, cli, auth, monkeypatch):
    _init(cli, monkeypatch)
    with pytest.raises(auth.LoginKeyError):
        auth.sign_challenge("abc", passphrase="the wrong passphrase")


# --- an early development key without a passphrase ----------------------------------------

def test_an_unencrypted_key_is_refused_with_the_command_to_replace_it(env, cli, auth,
                                                                      monkeypatch, capsys):
    home, key = env
    _write_legacy_key(auth, home, key)
    before = key.read_bytes()
    monkeypatch.setenv(ENV, "anything")
    assert cli.main(["sign", "abc"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "Traceback" not in err
    assert "omli-ui init --replace" in err
    assert key.read_bytes() == before, "the key was migrated silently"
    with pytest.raises(auth.LoginKeyError):
        auth.sign_challenge("abc", passphrase="anything")


def test_a_plain_init_does_not_touch_an_unencrypted_key(env, cli, auth, monkeypatch):
    home, key = env
    _write_legacy_key(auth, home, key)
    before = key.read_bytes()
    monkeypatch.setenv(ENV, "a new passphrase")
    assert cli.main(["init"]) == 1
    assert key.read_bytes() == before and len(_registry(home)) == 1


def test_init_replace_makes_an_encrypted_key_and_revokes_the_old_one(env, cli, auth,
                                                                     monkeypatch):
    home, key = env
    _, old_id = _write_legacy_key(auth, home, key)
    monkeypatch.setenv(ENV, "a new passphrase")
    assert cli.main(["init", "--replace"]) == 0
    assert b"ENCRYPTED PRIVATE KEY" in key.read_bytes()
    keys = auth.registered_keys()
    assert old_id not in keys and len(keys) == 1
    records = _registry(home)
    assert [r["type"] for r in records] == ["key", "revoke", "key"]
    assert records[1]["pubkey_id"] == old_id


def test_a_revoked_key_cannot_log_in(env, cli, auth, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    home, key = env
    old, _ = _write_legacy_key(auth, home, key)
    old_copy = key.parent / "old copy.pem"
    old_copy.write_bytes(old.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(b"copy")))
    monkeypatch.setenv(ENV, "a new passphrase")
    assert cli.main(["init", "--replace"]) == 0
    base = "http://127.0.0.1:8765"
    with TestClient(_load("omli.ui.server").create_app(port=8765), base_url=base) as c:
        ch = c.post("/api/auth/challenge", headers={"Origin": base}).json()["challenge"]
        token = auth.sign_challenge(ch, priv_path=old_copy, passphrase="copy")
        r = c.post("/api/auth/login", headers={"Origin": base},
                   json={"challenge": ch, "token": token})
        assert r.status_code == 401
