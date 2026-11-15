"""Contract for the local UI server (omli.ui): written before the server, and it must stay green.

Every route and WebSocket needs a session; the server binds 127.0.0.1 only and nothing turns
login off or moves the bind; a foreign Host header is refused; a state-changing request or a
WebSocket from a foreign Origin is refused and no CORS header is ever sent; a login challenge
works once and expires.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402
from starlette.routing import WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
# The test client sends Host: testserver for a relative WebSocket URL, so name the server.
WS = f"ws://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}
FOREIGN = {"Origin": "http://attacker.example"}
UI_DIR = Path(__file__).resolve().parent.parent / "omli" / "ui"
# The only routes a request with no session may reach.
PUBLIC_PATHS = {"/login", "/login.js", "/api/auth/challenge", "/api/auth/login",
                "/api/auth/ticket", "/api/auth/redeem"}
TOGGLES = ("HOST", "OMLI_UI_HOST", "OMLI_HOST", "UVICORN_HOST", "OMLI_NO_AUTH",
           "OMLI_UI_NO_AUTH", "OMLI_UI_AUTH", "OMLI_AUTH")


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
    home = tmp_path / "home"
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "user" / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "contract test passphrase")
    for name in TOGGLES:
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def auth(env):
    mod = _load("omli.ui.auth")
    mod.create_login_key()
    return mod


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(auth, clock):
    return _load("omli.ui.server").create_app(port=PORT, clock=clock)


def _client(app, base=BASE):
    return TestClient(app, base_url=base)


def _login(client, auth):
    challenge = client.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    r = client.post("/api/auth/login", headers=OWN,
                    json={"challenge": challenge, "token": auth.sign_challenge(challenge)})
    assert r.status_code == 200, r.text
    return client


def _no_cors(response):
    leaked = [h for h in response.headers if h.lower().startswith("access-control-")]
    assert not leaked, f"CORS headers sent: {leaked}"


# --- 1. every route and WebSocket refuses a request with no session ---------------------

def test_every_route_and_websocket_refuses_a_request_with_no_session(app):
    checked, skipped = 0, []
    with _client(app) as c:
        for route in app.routes:
            path = getattr(route, "path", None)
            if path in PUBLIC_PATHS:
                continue
            # FastAPI registers a WebSocket as APIWebSocketRoute, a subclass of this one.
            is_ws = isinstance(route, WebSocketRoute)
            if not (getattr(route, "methods", None) or is_ws):
                skipped.append(path)
            if is_ws:
                with pytest.raises(WebSocketDisconnect):
                    with c.websocket_connect(WS + path, headers=OWN) as ws:
                        ws.receive_json()
                checked += 1
                continue
            for method in sorted(getattr(route, "methods", None) or ()):
                r = c.request(method, path, headers=OWN, follow_redirects=False)
                assert r.status_code in (401, 303), f"{method} {path} -> {r.status_code}"
                if r.status_code == 303:
                    assert r.headers["location"] == "/login"
                checked += 1
    assert not skipped, f"routes this test did not call: {skipped}"
    assert checked >= 5, "expected the skeleton's routes to be checked"


def test_only_the_login_routes_are_public(app):
    with _client(app) as c:
        # Probe paths that a framework might add on its own.
        for path in ("/", "/docs", "/redoc", "/openapi.json", "/api/auth/me", "/api/projects",
                     "/static/x", "/does-not-exist"):
            r = c.get(path, headers=OWN, follow_redirects=False)
            assert r.status_code in (401, 303), f"GET {path} -> {r.status_code}"


# --- 2. binds 127.0.0.1 only; nothing turns login off or moves the bind -----------------

def test_serve_binds_loopback_only_whatever_the_environment_says(env, monkeypatch):
    server = _load("omli.ui.server")
    for name in TOGGLES:
        monkeypatch.setenv(name, "0.0.0.0" if "HOST" in name else "1")
    seen = {}
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: seen.update(kw))
    server.serve(port=PORT)
    assert seen["host"] == "127.0.0.1"
    assert seen.get("proxy_headers") is False, "proxy headers must not rewrite the client or host"
    assert server.HOST == "127.0.0.1"


def test_no_flag_sets_the_host_or_turns_login_off(env, monkeypatch):
    cli = _load("omli.ui.__main__")
    server = _load("omli.ui.server")
    # If a flag were accepted, serve() must return instead of running a real server.
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: None)
    for argv in (["serve", "--host", "0.0.0.0"], ["serve", "--no-auth"],
                 ["serve", "--bind", "0.0.0.0"], ["serve", "--insecure"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code != 0, f"{argv} was accepted"


def test_create_app_takes_no_host_or_auth_switch(env):
    server = _load("omli.ui.server")
    assert set(inspect.signature(server.create_app).parameters) <= {"port", "home", "clock"}
    assert set(inspect.signature(server.serve).parameters) <= {"port", "home"}


def test_login_toggles_in_the_environment_do_nothing(auth, clock, monkeypatch):
    for name in TOGGLES:
        monkeypatch.setenv(name, "1")
    app = _load("omli.ui.server").create_app(port=PORT, clock=clock)
    with _client(app) as c:
        assert c.get("/api/auth/me", headers=OWN).status_code == 401


def test_the_ui_reads_no_environment_variable_outside_the_allowlist():
    allowed = {"OMLI_UI_KEY", "OMLI_UI_KEY_PASSPHRASE"}
    files = sorted(UI_DIR.glob("*.py"))
    assert files, "omli/ui does not exist yet"
    pattern = re.compile(r"""(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Z0-9_]+)["']""")
    for path in files:
        text = path.read_text(encoding="utf-8")
        names = set(pattern.findall(text))
        assert names <= allowed, f"{path.name} reads {sorted(names - allowed)}"
        # Every environment access in the code must be one the pattern can see.
        accesses = re.findall(r"\bos\.environ\b|\bgetenv\s*\(", text)
        assert len(accesses) == len(pattern.findall(text)), path.name
        assert not re.search(r"from\s+os\s+import\s+.*\b(environ|getenv)\b", text), path.name


# --- 3. a Host header other than localhost / 127.0.0.1 with the server's port is refused --

@pytest.mark.parametrize("host", ["attacker.example:8765", "127.0.0.1:9999", "localhost",
                                  "127.0.0.1", "0.0.0.0:8765", "[::1]:8765",
                                  "127.0.0.1.attacker.example:8765", "127.0.0.1:8765.evil",
                                  ""])
def test_a_foreign_host_header_is_refused(app, host):
    with _client(app) as c:
        for path in ("/login", "/api/auth/me"):
            r = c.get(path, headers={"Host": host})
            assert r.status_code == 400, f"Host {host!r} {path} -> {r.status_code}"
            _no_cors(r)


def test_a_websocket_with_a_foreign_host_is_refused(app, auth):
    with _client(app) as c:
        _login(c, auth)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("ws://attacker.example:8765/ws",
                                     headers={**OWN, "Cookie": "omli_session="
                                              + c.cookies.get("omli_session")}) as ws:
                ws.receive_json()


def test_a_foreign_host_is_refused_even_with_a_valid_session(app, auth):
    with _client(app) as c:
        _login(c, auth)
        cookie = c.cookies.get("omli_session")
        assert cookie
    with _client(app, "http://attacker.example:8765") as evil:
        r = evil.get("/api/auth/me", headers={"Cookie": f"omli_session={cookie}"})
        assert r.status_code == 400


def test_both_loopback_names_with_the_port_are_accepted(app):
    for base in (BASE, f"http://localhost:{PORT}"):
        with _client(app, base) as c:
            assert c.get("/login").status_code == 200


# --- 4. a foreign Origin on a state change or WebSocket is refused; no CORS, ever --------

@pytest.mark.parametrize("headers", [FOREIGN, {}, {"Origin": "null"},
                                     {"Origin": f"http://127.0.0.1:{PORT + 1}"},
                                     {"Origin": f"https://127.0.0.1:{PORT}"}])
def test_a_state_change_from_another_origin_is_refused(app, auth, headers):
    with _client(app) as c:
        _login(c, auth)
        r = c.post("/api/auth/logout", headers=headers)
        assert r.status_code == 403, r.status_code
        _no_cors(r)
        assert c.get("/api/auth/me", headers=OWN).status_code == 200, "logout must not have run"


def test_a_state_change_from_the_own_origin_is_allowed(app, auth):
    with _client(app) as c:
        _login(c, auth)
        r = c.post("/api/auth/logout", headers=OWN)
        assert r.status_code == 200
        _no_cors(r)


@pytest.mark.parametrize("headers", [FOREIGN, {}])
def test_a_websocket_from_another_origin_is_refused(app, auth, headers):
    with _client(app) as c:
        _login(c, auth)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(WS + "/ws", headers=headers) as ws:
                ws.receive_json()


def test_a_websocket_from_the_own_origin_with_a_session_connects(app, auth):
    with _client(app) as c:
        _login(c, auth)
        with c.websocket_connect(WS + "/ws", headers=OWN) as ws:
            assert ws.receive_json()["type"] == "hello"


def test_no_response_ever_carries_a_cors_header(app, auth):
    with _client(app) as c:
        preflight = {"Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"}
        for headers in (FOREIGN, OWN):
            _no_cors(c.options("/api/auth/login", headers={**headers, **preflight}))
            _no_cors(c.get("/login", headers=headers))
            _no_cors(c.post("/api/auth/challenge", headers=headers))
        _login(c, auth)
        for headers in (FOREIGN, OWN):
            _no_cors(c.get("/api/auth/me", headers=headers))
            _no_cors(c.options("/api/auth/me", headers={**headers, **preflight}))


# --- 4b. request bodies are bounded before anything reads them ---------------------------

def test_a_large_body_on_a_public_route_is_refused_before_it_is_read(app):
    server = _load("omli.ui.server")
    with _client(app) as c:
        for path in ("/api/auth/login", "/api/auth/ticket", "/api/auth/redeem"):
            r = c.post(path, headers={**OWN, "Content-Type": "application/json"},
                       content=b" " * (server.PUBLIC_BODY_LIMIT + 1))
            assert r.status_code == 413, f"{path} -> {r.status_code}"


def test_a_large_body_on_a_signed_in_route_is_refused(app, auth):
    server = _load("omli.ui.server")
    with _client(app) as c:
        _login(c, auth)
        r = c.post("/api/mindmap/save", headers={**OWN, "Content-Type": "application/json"},
                   content=b" " * (server.BODY_LIMIT + 1))
        assert r.status_code == 413


def test_a_body_with_no_declared_length_is_refused(app):
    def chunks():
        yield b'{"challenge": "x",'
        yield b' "token": "y"}'
    with _client(app) as c:
        r = c.post("/api/auth/login", headers={**OWN, "Content-Type": "application/json"},
                   content=chunks())
        assert r.status_code == 411


def test_the_body_limits_are_named_and_small_enough(app):
    server = _load("omli.ui.server")
    assert 1024 <= server.PUBLIC_BODY_LIMIT <= 64 * 1024
    assert server.PUBLIC_BODY_LIMIT < server.BODY_LIMIT <= 32 * 1024 * 1024


# --- 5. a login challenge works once, expires; replayed or expired ones are refused ------

def _challenge(c):
    r = c.post("/api/auth/challenge", headers=OWN)
    assert r.status_code == 200
    return r.json()["challenge"]


def _try(c, challenge, token):
    return c.post("/api/auth/login", headers=OWN, json={"challenge": challenge, "token": token})


def test_a_challenge_logs_in_once(app, auth):
    with _client(app) as c:
        ch = _challenge(c)
        assert _try(c, ch, auth.sign_challenge(ch)).status_code == 200
        assert _try(c, ch, auth.sign_challenge(ch)).status_code == 401


def test_an_expired_challenge_is_refused(app, auth, clock):
    with _client(app) as c:
        ch = _challenge(c)
        clock.t += auth.CHALLENGE_TTL_SECONDS + 1
        assert _try(c, ch, auth.sign_challenge(ch)).status_code == 401


def test_a_token_signed_for_another_challenge_is_refused(app, auth):
    with _client(app) as c:
        first, second = _challenge(c), _challenge(c)
        assert _try(c, second, auth.sign_challenge(first)).status_code == 401


def test_an_unissued_challenge_is_refused(app, auth):
    with _client(app) as c:
        made_up = "A" * 43
        assert _try(c, made_up, auth.sign_challenge(made_up)).status_code == 401


def test_a_failed_attempt_burns_the_challenge(app, auth):
    with _client(app) as c:
        ch = _challenge(c)
        assert _try(c, ch, "not-a-token").status_code == 401
        assert _try(c, ch, auth.sign_challenge(ch)).status_code == 401


def test_a_failed_login_sets_no_session(app, auth):
    with _client(app) as c:
        ch = _challenge(c)
        r = _try(c, ch, "not-a-token")
        assert "set-cookie" not in r.headers
        assert c.get("/api/auth/me", headers=OWN).status_code == 401
