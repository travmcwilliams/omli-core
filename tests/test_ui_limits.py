"""Open challenges and live sessions are bounded, and expired ones are cleaned up.

At the challenge cap a new challenge is refused with a clear error; a live one is never
evicted to make room. The same holds for sessions at the session cap.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}


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
def auth(tmp_path, monkeypatch):
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "user" / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "limits test passphrase")
    mod = _load("omli.ui.auth")
    mod.create_login_key()
    return mod


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(auth, clock):
    return _load("omli.ui.server").create_app(port=PORT, clock=clock)


def _state(app):
    return app.state.auth


def _login(c, auth):
    ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    return c.post("/api/auth/login", headers=OWN,
                  json={"challenge": ch, "token": auth.sign_challenge(ch)})


@pytest.fixture(autouse=True)
def small_caps(request, monkeypatch):
    """The cap behaviour is tested at small caps so a test never loops over the real default.
    The defaults themselves are checked by test_the_limits_are_named_constants."""
    if request.function.__name__ == "test_the_limits_are_named_constants":
        return
    auth = _load("omli.ui.auth")
    monkeypatch.setattr(auth, "MAX_OPEN_CHALLENGES", 5)
    monkeypatch.setattr(auth, "MAX_SESSIONS", 4)


def test_the_limits_are_named_constants(auth):
    assert isinstance(auth.MAX_OPEN_CHALLENGES, int) and 0 < auth.MAX_OPEN_CHALLENGES <= 1000
    assert isinstance(auth.MAX_SESSIONS, int) and 0 < auth.MAX_SESSIONS <= 1000


def test_a_challenge_flood_stays_bounded_and_is_refused_clearly(app, auth):
    cap = auth.MAX_OPEN_CHALLENGES
    with TestClient(app, base_url=BASE) as c:
        codes = [c.post("/api/auth/challenge", headers=OWN) for _ in range(cap * 10)]
    ok = [r for r in codes if r.status_code == 200]
    refused = [r for r in codes if r.status_code == 429]
    assert len(ok) == cap and len(refused) == cap * 9
    assert "too many" in refused[0].json()["error"]
    assert _state(app).open_challenges() == cap


def test_a_live_challenge_is_not_evicted_by_a_flood(app, auth):
    with TestClient(app, base_url=BASE) as c:
        first = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
        for _ in range(auth.MAX_OPEN_CHALLENGES * 3):
            c.post("/api/auth/challenge", headers=OWN)
        r = c.post("/api/auth/login", headers=OWN,
                   json={"challenge": first, "token": auth.sign_challenge(first)})
        assert r.status_code == 200


def test_expired_challenges_are_cleaned_up_and_free_the_cap(app, auth, clock):
    with TestClient(app, base_url=BASE) as c:
        for _ in range(auth.MAX_OPEN_CHALLENGES):
            assert c.post("/api/auth/challenge", headers=OWN).status_code == 200
        assert c.post("/api/auth/challenge", headers=OWN).status_code == 429
        clock.t += auth.CHALLENGE_TTL_SECONDS + 1
        assert c.post("/api/auth/challenge", headers=OWN).status_code == 200
    assert _state(app).open_challenges() == 1


def test_expired_sessions_are_cleaned_up(app, auth, clock):
    for _ in range(3):
        with TestClient(app, base_url=BASE) as c:
            assert _login(c, auth).status_code == 200
    assert _state(app).live_sessions() == 3
    clock.t += auth.SESSION_TTL_SECONDS + 1
    with TestClient(app, base_url=BASE) as c:
        c.get("/login")
        c.get("/api/auth/me", headers=OWN)
    assert _state(app).live_sessions() == 0
    assert len(_state(app)._sessions) == 0, "expired sessions are still held in memory"


def test_the_session_cap_refuses_a_new_login_and_keeps_the_live_ones(app, auth):
    clients = []
    for _ in range(auth.MAX_SESSIONS):
        c = TestClient(app, base_url=BASE)
        assert _login(c, auth).status_code == 200
        clients.append(c)
    extra = TestClient(app, base_url=BASE)
    r = _login(extra, auth)
    assert r.status_code == 429 and "too many" in r.json()["error"]
    assert "set-cookie" not in r.headers
    assert _state(app).live_sessions() == auth.MAX_SESSIONS
    for c in clients:
        assert c.get("/api/auth/me", headers=OWN).status_code == 200
