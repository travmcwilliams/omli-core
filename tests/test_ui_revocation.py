"""A session ends as soon as its key is revoked or replaced.

The key's status is checked on every request, not only at sign-in: a session whose key is no
longer registered is refused and dropped, on pages, API routes and WebSockets alike.
"""

from __future__ import annotations

import importlib
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "user" / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "revocation test passphrase")
    mod = _load("omli.ui.auth")
    mod.create_login_key()
    return mod


@pytest.fixture
def app(auth):
    return _load("omli.ui.server").create_app(port=PORT)


def _signed_in(app, auth):
    c = TestClient(app, base_url=BASE)
    ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    r = c.post("/api/auth/login", headers=OWN,
               json={"challenge": ch, "token": auth.sign_challenge(ch)})
    assert r.status_code == 200, r.text
    assert c.get("/api/auth/me", headers=OWN).status_code == 200
    return c


def _registry(auth):
    return auth.registry_path()


def test_replacing_the_key_ends_the_live_session(app, auth):
    c = _signed_in(app, auth)
    auth.create_login_key(replace=True)
    assert c.get("/api/auth/me", headers=OWN).status_code == 401
    r = c.get("/", headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert app.state.auth.live_sessions() == 0, "the replaced key's session is still held"


def test_a_revocation_record_ends_the_live_session(app, auth):
    c = _signed_in(app, auth)
    pubkey_id = c.get("/api/auth/me", headers=OWN).json()["pubkey_id"]
    with open(_registry(auth), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"type": "revoke", "pubkey_id": pubkey_id,
                             "revoked_at": "2026-11-10T00:00:00+00:00",
                             "reason": "test"}) + "\n")
    assert c.get("/api/auth/me", headers=OWN).status_code == 401
    assert c.post("/api/projects/add", headers=OWN, json={"path": "x"}).status_code == 401


def test_a_websocket_of_a_revoked_key_is_refused(app, auth):
    c = _signed_in(app, auth)
    with c.websocket_connect(WS + "/ws", headers=OWN) as ws:
        assert ws.receive_json()["type"] == "hello"
    auth.create_login_key(replace=True)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + "/ws", headers=OWN) as ws:
            ws.receive_json()


def test_registering_another_key_keeps_the_live_session(app, auth, tmp_path):
    c = _signed_in(app, auth)
    auth.create_login_key(tmp_path / "second-user" / "login.key")
    assert c.get("/api/auth/me", headers=OWN).status_code == 200


def test_an_unreadable_registry_refuses_the_session(app, auth):
    c = _signed_in(app, auth)
    with open(_registry(auth), "a", encoding="utf-8", newline="\n") as fh:
        fh.write("not json\n")
    r = c.get("/api/auth/me", headers=OWN)
    assert r.status_code == 401, "an unreadable key registry must fail closed"
