"""The UI's key login: each user makes their own ed25519 key; no passwords, no default account.

The private key lives under the user's home folder (never under OMLI_HOME) and never reaches
the browser; the public key is registered under OMLI_HOME as an append-only record. The
session is a random token in an HttpOnly, SameSite=Strict cookie that expires.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}
PASSPHRASE = "login test passphrase"


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "omli home"
    user = tmp_path / "User Profile"
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.delenv("OMLI_UI_KEY", raising=False)
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", PASSPHRASE)
    return home.resolve(), user.resolve()


@pytest.fixture
def auth(env):
    return _load("omli.ui.auth")


@pytest.fixture
def clock():
    return Clock()


def _app(clock):
    return _load("omli.ui.server").create_app(port=PORT, clock=clock)


def _login(c, auth):
    ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    return c.post("/api/auth/login", headers=OWN,
                  json={"challenge": ch, "token": auth.sign_challenge(ch)})


# --- the key ------------------------------------------------------------------------------

def test_first_run_puts_the_private_key_in_the_user_home_and_registers_the_public_key(env, auth):
    home, user = env
    priv, pubkey_id = auth.create_login_key()
    assert priv == (user / ".omli" / "ui_login.key").resolve()
    assert priv.exists()
    assert home not in priv.parents
    registry = home / "ui" / "keys.jsonl"
    lines = registry.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["type"] == "key" and record["pubkey_id"] == pubkey_id
    assert "PUBLIC KEY" in record["pubkey_pem"]
    assert "PRIVATE" not in registry.read_text(encoding="utf-8")
    assert auth.registered_keys() == {pubkey_id: record["pubkey_pem"]}


def test_the_environment_override_moves_the_private_key(env, auth, tmp_path, monkeypatch):
    target = tmp_path / "Elsewhere Dir" / "My Key.pem"
    monkeypatch.setenv("OMLI_UI_KEY", str(target))
    priv, _ = auth.create_login_key()
    assert priv == target.resolve() and target.exists()


def test_a_private_key_under_omli_home_is_refused(env, auth, monkeypatch):
    home, _ = env
    monkeypatch.setenv("OMLI_UI_KEY", str(home / "keys" / "login.key"))
    with pytest.raises(ValueError):
        auth.create_login_key()
    assert not (home / "keys" / "login.key").exists()
    assert auth.registered_keys() == {}


def test_first_run_refuses_to_overwrite_an_existing_key(env, auth):
    priv, _ = auth.create_login_key()
    before = priv.read_bytes()
    with pytest.raises(FileExistsError):
        auth.create_login_key()
    assert priv.read_bytes() == before
    assert len(auth.registered_keys()) == 1


def test_a_passphrase_encrypts_the_key_and_signing_still_works(env, auth, monkeypatch):
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "correct horse")
    priv, pubkey_id = auth.create_login_key()
    assert b"ENCRYPTED" in priv.read_bytes()
    token = auth.sign_challenge("abc")
    assert auth.verify_token(auth.registered_keys()[pubkey_id], "abc", token)


def test_a_fresh_home_has_no_account(env, auth):
    assert auth.registered_keys() == {}


def test_the_cli_init_and_sign(env, auth, capsys):
    cli = _load("omli.ui.__main__")
    assert cli.main(["init"]) == 0
    assert cli.main(["init"]) != 0, "a second init must not replace the key"
    capsys.readouterr()
    assert cli.main(["sign", "the-challenge"]) == 0
    token = capsys.readouterr().out.strip()
    pubkey_id, pem = next(iter(auth.registered_keys().items()))
    assert auth.verify_token(pem, "the-challenge", token)


# --- no key registered --------------------------------------------------------------------

def test_with_no_registered_key_every_route_refuses_and_login_shows_setup(env, auth, clock):
    app = _app(clock)
    with TestClient(app, base_url=BASE) as c:
        page = c.get("/login")
        assert page.status_code == 200
        assert "omli-ui init" in page.text
        assert c.post("/api/auth/challenge", headers=OWN).status_code in (401, 409)
        assert c.post("/api/auth/login", headers=OWN,
                      json={"challenge": "x", "token": "y"}).status_code == 401
        for path in ("/", "/api/auth/me", "/api/projects"):
            assert c.get(path, headers=OWN, follow_redirects=False).status_code in (401, 303)


# --- the session --------------------------------------------------------------------------

def test_the_session_cookie_is_httponly_strict_and_expiring(env, auth, clock):
    auth.create_login_key()
    with TestClient(_app(clock), base_url=BASE) as c:
        r = _login(c, auth)
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        lowered = cookie.lower()
        assert cookie.startswith("omli_session=")
        assert "httponly" in lowered and "samesite=strict" in lowered
        assert "max-age=" in lowered and "path=/" in lowered
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        assert len(token) >= 32


def test_each_login_gets_a_new_random_token(env, auth, clock):
    auth.create_login_key()
    tokens = set()
    for _ in range(3):
        with TestClient(_app(clock), base_url=BASE) as c:
            _login(c, auth)
            tokens.add(c.cookies.get("omli_session"))
    assert len(tokens) == 3


def test_me_and_logout(env, auth, clock):
    _, pubkey_id = auth.create_login_key()
    with TestClient(_app(clock), base_url=BASE) as c:
        _login(c, auth)
        me = c.get("/api/auth/me", headers=OWN)
        assert me.status_code == 200 and me.json()["pubkey_id"] == pubkey_id
        old = c.cookies.get("omli_session")
        assert c.post("/api/auth/logout", headers=OWN).status_code == 200
        assert c.get("/api/auth/me", headers=OWN).status_code == 401
        c.cookies.set("omli_session", old)
        assert c.get("/api/auth/me", headers=OWN).status_code == 401, "a logged-out token came back"


def test_the_session_expires(env, auth, clock):
    auth.create_login_key()
    with TestClient(_app(clock), base_url=BASE) as c:
        _login(c, auth)
        clock.t += auth.SESSION_TTL_SECONDS + 1
        assert c.get("/api/auth/me", headers=OWN).status_code == 401


def test_an_unregistered_key_cannot_log_in(env, auth, clock, tmp_path, monkeypatch):
    auth.create_login_key()
    other = tmp_path / "Other User" / "k.pem"
    with TestClient(_app(clock), base_url=BASE) as c:
        ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
        stranger = auth.sign_challenge(ch, priv_path=_make_stray_key(auth, other))
        r = c.post("/api/auth/login", headers=OWN, json={"challenge": ch, "token": stranger})
        assert r.status_code == 401


def _make_stray_key(auth, path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path.parent.mkdir(parents=True)
    path.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(PASSPHRASE.encode())))
    return path


# --- the private key never reaches the browser; no passwords ------------------------------

def test_no_response_carries_the_private_key(env, auth, clock):
    priv, _ = auth.create_login_key()
    secret_lines = [ln for ln in priv.read_text(encoding="ascii").splitlines()
                    if ln and not ln.startswith("-----")]
    with TestClient(_app(clock), base_url=BASE) as c:
        bodies = [c.get("/login").text, c.get("/login.js").text]
        bodies.append(_login(c, auth).text)
        bodies += [c.get(p, headers=OWN).text for p in ("/", "/app.js", "/api/auth/me",
                                                         "/api/projects")]
    for body in bodies:
        assert "PRIVATE KEY" not in body
        for line in secret_lines:
            assert line not in body


def test_there_is_no_password_anywhere(env, auth, clock):
    auth.create_login_key()
    app = _app(clock)
    assert not [r.path for r in app.routes if "password" in getattr(r, "path", "").lower()]
    with TestClient(app, base_url=BASE) as c:
        page = c.get("/login").text.lower()
        assert 'type="password"' not in page and "password" not in page
