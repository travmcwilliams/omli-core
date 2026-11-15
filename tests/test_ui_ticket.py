"""The one-time login ticket: ``omli-ui login`` signs in the browser in one step.

Limits, each tested here:
1. A ticket is made only after the command line proves it owns a registered key: it unlocks
   the key with the passphrase, signs a fresh challenge, and the server checks the signature.
2. A ticket works once and expires within 60 seconds; a used or expired ticket is refused.
3. A ticket is never written to a log. On disk it exists only in the private hand-off file,
   which is removed when the ticket expires; the server holds only its hash.
4. The ticket reaches the browser only in the address fragment, the login page removes it
   from the address before redeeming it and then lands on ``/``, and pages send no Referer.
5. The private key and the passphrase never reach the browser, the server, or any command
   line: the login command has no option for either, and nothing it sends contains them.
"""

from __future__ import annotations

import importlib
import json
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import url2pathname
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}
FOREIGN = {"Origin": "http://attacker.example"}
PASSPHRASE = "ticket test passphrase"


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
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", PASSPHRASE)
    mod = _load("omli.ui.auth")
    mod.create_login_key()
    return mod


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(auth, clock):
    return _load("omli.ui.server").create_app(port=PORT, clock=clock)


def _signed_answer(c, auth):
    ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    return {"challenge": ch, "token": auth.sign_challenge(ch)}


def _ticket(c, auth):
    r = c.post("/api/auth/ticket", headers=OWN, json=_signed_answer(c, auth))
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def _redeem(c, ticket, headers=OWN):
    return c.post("/api/auth/redeem", headers=headers, json={"ticket": ticket})


# --- 1. a ticket needs proof of key ownership --------------------------------------------

def test_a_ticket_is_made_only_for_a_valid_signature(app, auth):
    with TestClient(app, base_url=BASE) as c:
        ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
        bad = c.post("/api/auth/ticket", headers=OWN, json={"challenge": ch, "token": "x"})
        assert bad.status_code == 401 and "ticket" not in bad.json()
        ch2 = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
        wrong = c.post("/api/auth/ticket", headers=OWN,
                       json={"challenge": ch2, "token": auth.sign_challenge(ch)})
        assert wrong.status_code == 401 and "ticket" not in wrong.json()
        assert isinstance(_ticket(c, auth), str)


def test_a_ticket_burns_its_challenge(app, auth):
    with TestClient(app, base_url=BASE) as c:
        answer = _signed_answer(c, auth)
        assert c.post("/api/auth/ticket", headers=OWN, json=answer).status_code == 200
        again = c.post("/api/auth/ticket", headers=OWN, json=answer)
        assert again.status_code == 401 and "ticket" not in again.json()


def test_a_ticket_is_not_made_for_a_revoked_key(app, auth):
    with TestClient(app, base_url=BASE) as c:
        answer = _signed_answer(c, auth)
        auth.create_login_key(replace=True)
        r = c.post("/api/auth/ticket", headers=OWN, json=answer)
        assert r.status_code == 401 and "ticket" not in r.json()


def test_the_ticket_route_refuses_another_origin(app, auth):
    with TestClient(app, base_url=BASE) as c:
        r = c.post("/api/auth/ticket", headers=FOREIGN, json=_signed_answer(c, auth))
        assert r.status_code == 403


# --- 2. once, and within 60 seconds ------------------------------------------------------

def test_the_ticket_lifetime_is_at_most_60_seconds(auth):
    assert isinstance(auth.TICKET_TTL_SECONDS, int) and 0 < auth.TICKET_TTL_SECONDS <= 60


def test_a_ticket_signs_in_once(app, auth):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        first = _redeem(c, ticket)
        assert first.status_code == 200
        cookie = first.headers["set-cookie"].lower()
        assert "omli_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert c.get("/api/auth/me", headers=OWN).status_code == 200
    with TestClient(app, base_url=BASE) as other:
        second = _redeem(other, ticket)
        assert second.status_code == 401 and "set-cookie" not in second.headers


def test_a_ticket_works_until_just_before_it_expires(app, auth, clock):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        clock.t += auth.TICKET_TTL_SECONDS - 1
        assert _redeem(c, ticket).status_code == 200


def test_an_expired_ticket_is_refused(app, auth, clock):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        clock.t += auth.TICKET_TTL_SECONDS
        r = _redeem(c, ticket)
        assert r.status_code == 401 and "set-cookie" not in r.headers
    assert app.state.auth.open_tickets() == 0


def test_an_unknown_ticket_is_refused(app, auth):
    with TestClient(app, base_url=BASE) as c:
        _ticket(c, auth)
        for bad in ("", "x", "A" * 43, None, 5):
            r = c.post("/api/auth/redeem", headers=OWN, json={"ticket": bad})
            assert r.status_code == 401 and "set-cookie" not in r.headers


def test_a_ticket_of_a_key_revoked_before_use_is_refused(app, auth):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        auth.create_login_key(replace=True)
        assert _redeem(c, ticket).status_code == 401


def test_redeeming_refuses_another_origin(app, auth):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        assert _redeem(c, ticket, headers=FOREIGN).status_code == 403


def test_open_tickets_are_capped(app, auth, monkeypatch):
    monkeypatch.setattr(app.state.auth, "_max_tickets", 3)
    with TestClient(app, base_url=BASE) as c:
        for _ in range(3):
            _ticket(c, auth)
        r = c.post("/api/auth/ticket", headers=OWN, json=_signed_answer(c, auth))
        assert r.status_code == 429 and "too many" in r.json()["error"]


# --- 3. never in a log or on disk ---------------------------------------------------------

def test_the_server_holds_only_a_hash_of_the_ticket(app, auth):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
    held = repr(app.state.auth._tickets)
    assert app.state.auth.open_tickets() == 1
    assert ticket not in held


class Recorder:
    """Stands in for the HTTP transport and the browser; records everything sent."""

    def __init__(self, client, opened=True):
        self.client, self.sent, self.urls, self.opened = client, [], [], opened
        self.targets, self.files, self.waits = [], [], []

    def post(self, path, body):
        self.sent.append((path, json.dumps(body)))
        r = self.client.post(path, headers=OWN, json=body)
        return r.status_code, r.json()

    def open(self, url):
        """The browser: given the hand-off file's address, read where the file sends it."""
        self.urls.append(url)
        if url.startswith("file:"):
            path = Path(url2pathname(urlsplit(url).path))
            self.files.append({"path": path, "existed": path.is_file(),
                               "mode": path.stat().st_mode & 0o777,
                               "dir_mode": path.parent.stat().st_mode & 0o777})
            found = re.search(r'url=([^"]+)"', path.read_text(encoding="utf-8"))
            self.targets.append(found.group(1) if found else "")
        return self.opened

    def wait(self, seconds):
        self.waits.append((seconds, [f["path"].is_file() for f in self.files]))


def _cli_login(app, monkeypatch, opened=True, argv=("login", "--port", str(PORT))):
    cli, client = _load("omli.ui.__main__"), _load("omli.ui.client")
    tc = TestClient(app, base_url=BASE)
    rec = Recorder(tc, opened)
    monkeypatch.setattr(client, "http_post", lambda port: rec.post)
    monkeypatch.setattr(client, "open_browser", rec.open)
    monkeypatch.setattr(client, "wait", rec.wait, raising=False)
    calls = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or real_popen(*a, **k))
    code = cli.main(list(argv))
    return code, rec, tc, calls


def _ticket_from(rec):
    assert len(rec.targets) == 1, "the browser was not sent to a hand-off file"
    parts = urlsplit(rec.targets[0])
    assert parts.fragment.startswith("ticket=")
    return parts.fragment[len("ticket="):]


def test_the_login_command_opens_the_browser_with_a_working_ticket(app, auth, monkeypatch):
    code, rec, tc, _ = _cli_login(app, monkeypatch)
    assert code == 0
    assert len(rec.urls) == 1
    with TestClient(app, base_url=BASE) as browser:
        assert _redeem(browser, _ticket_from(rec)).status_code == 200


def test_the_ticket_is_never_logged_printed_or_left_on_disk(app, auth, monkeypatch, caplog,
                                                             capsys, tmp_path):
    caplog.set_level(logging.DEBUG)
    # The hand-off file goes to the temporary folder: point that at tmp_path, so the scan
    # below covers the place the ticket is really written.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    code, rec, tc, _ = _cli_login(app, monkeypatch)
    assert code == 0
    assert tmp_path in rec.files[0]["path"].parents
    ticket = _ticket_from(rec)
    with TestClient(app, base_url=BASE) as browser:
        assert _redeem(browser, ticket).status_code == 200
    out = capsys.readouterr()
    assert ticket not in caplog.text
    assert ticket not in out.out and ticket not in out.err
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert ticket.encode() not in path.read_bytes(), f"ticket written to {path.name}"


def test_with_no_browser_the_ticket_is_not_printed(app, auth, monkeypatch, capsys):
    code, rec, tc, _ = _cli_login(app, monkeypatch, opened=False)
    assert code == 1
    ticket = _ticket_from(rec)
    out = capsys.readouterr()
    assert ticket not in out.out and ticket not in out.err
    assert "omli-ui sign" in out.err
    assert out.err.count("\n") == 1


# --- 4. fragment only, cleared before use, no Referer ------------------------------------

def test_the_ticket_travels_only_in_the_fragment(app, auth, monkeypatch):
    code, rec, tc, _ = _cli_login(app, monkeypatch)
    parts = urlsplit(rec.targets[0])
    assert (parts.scheme, parts.netloc, parts.path, parts.query) == \
        ("http", f"127.0.0.1:{PORT}", "/login", "")
    ticket = _ticket_from(rec)
    assert all(ticket not in path for path, _ in rec.sent), "ticket sent in a request path"


def test_the_login_page_clears_the_ticket_from_the_address_before_redeeming():
    js = _load("omli.ui.pages").LOGIN_JS
    clear = js.find("history.replaceState(null, '', '/login')")
    redeem = js.find("'/api/auth/redeem'")
    land = js.find("location.replace('/')")
    assert clear != -1 and redeem != -1 and land != -1
    assert clear < redeem < land
    assert "body: JSON.stringify({ticket" in js, "the ticket must go in the request body"


def test_redeeming_answers_without_a_redirect(app, auth):
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        r = c.post("/api/auth/redeem", headers=OWN, json={"ticket": ticket},
                   follow_redirects=False)
        assert r.status_code == 200 and "location" not in r.headers
        assert ticket not in r.text


def test_pages_send_no_referer(app, auth):
    pages = _load("omli.ui.pages")
    assert '<meta name="referrer" content="no-referrer">' in pages.login_page(True)
    assert '<meta name="referrer" content="no-referrer">' in pages.login_page(False)
    assert '<meta name="referrer" content="no-referrer">' in pages.app_page()
    with TestClient(app, base_url=BASE) as c:
        ticket = _ticket(c, auth)
        for r in (c.get("/login"), _redeem(c, ticket), c.get("/", headers=OWN)):
            assert r.headers["referrer-policy"] == "no-referrer"


# --- 5. the key and the passphrase stay in the command-line process ----------------------

def test_nothing_the_login_command_sends_holds_the_key_or_passphrase(app, auth, monkeypatch):
    key_text = auth.key_path().read_text(encoding="ascii")
    key_body = "".join(l for l in key_text.splitlines() if not l.startswith("-----"))
    code, rec, tc, calls = _cli_login(app, monkeypatch)
    assert code == 0
    sent = " ".join(body for _, body in rec.sent) + " ".join(rec.urls + rec.targets)
    for secret in (PASSPHRASE, key_body, key_body[:40]):
        assert secret not in sent
    assert str(auth.key_path()) not in sent
    assert calls == [], "the login command started another program"


def test_no_command_takes_a_passphrase_or_key_option():
    cli = _load("omli.ui.__main__")
    parser = cli._parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    assert "login" in sub.choices
    for name, p in sub.choices.items():
        for action in p._actions:
            for opt in action.option_strings:
                assert "pass" not in opt and "key" not in opt, f"{name} {opt}"
    assert [a.dest for a in sub.choices["login"]._actions if a.option_strings
            and a.dest != "help"] == ["port"]
    assert [a.dest for a in sub.choices["serve"]._actions if a.option_strings
            and a.dest != "help"] == ["port"]


def test_a_wrong_passphrase_requests_no_ticket_and_opens_nothing(app, auth, monkeypatch,
                                                                 capsys):
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "not the passphrase")
    code, rec, tc, _ = _cli_login(app, monkeypatch)
    assert code == 1
    assert rec.urls == []
    assert rec.sent == [], "a wrong passphrase must be caught before the server is contacted"
    out = capsys.readouterr()
    assert out.out == "" and out.err.count("\n") == 1 and "wrong passphrase" in out.err


def test_with_no_server_the_command_says_how_to_start_one(auth, monkeypatch, capsys):
    cli, client = _load("omli.ui.__main__"), _load("omli.ui.client")

    def refused(port):
        def post(path, body):
            raise OSError("connection refused")
        return post

    monkeypatch.setattr(client, "http_post", refused)
    monkeypatch.setattr(client, "open_browser", lambda url: pytest.fail("opened a browser"))
    assert cli.main(["login", "--port", "9"]) == 1
    err = capsys.readouterr().err
    assert "omli-ui serve" in err and err.count("\n") == 1


# --- the real transport talks to 127.0.0.1 directly, never through a proxy ----------------

def test_the_transport_ignores_proxy_settings_and_sends_the_own_origin(monkeypatch):
    client = _load("omli.ui.client")
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["origin"] = self.headers.get("Origin")
            seen["host"] = self.headers.get("Host")
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        status, data = client.http_post(port)("/api/auth/challenge", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert (status, data) == (200, {"ok": True})
    assert seen == {"path": "/api/auth/challenge", "origin": f"http://127.0.0.1:{port}",
                    "host": f"127.0.0.1:{port}"}


# --- 6. the ticket is on no command line: the browser opens a private hand-off file --------

def test_the_address_given_to_the_browser_never_holds_the_ticket(app, auth, monkeypatch):
    code, rec, tc, _ = _cli_login(app, monkeypatch)
    assert code == 0
    ticket = _ticket_from(rec)
    assert len(rec.urls) == 1 and rec.urls[0].startswith("file:")
    assert ticket not in rec.urls[0]


def test_the_hand_off_file_is_private_and_removed_after_the_ticket_lifetime(app, auth,
                                                                           monkeypatch):
    code, rec, tc, _ = _cli_login(app, monkeypatch)
    assert code == 0
    f = rec.files[0]
    assert f["existed"]
    if os.name != "nt":
        assert f["mode"] == 0o600 and f["dir_mode"] == 0o700
    assert rec.waits == [(auth.TICKET_TTL_SECONDS, [True])],         "the file must stay until the ticket has expired, and no longer"
    assert not f["path"].exists() and not f["path"].parent.exists()


def test_with_no_browser_the_hand_off_file_is_removed_at_once(app, auth, monkeypatch):
    code, rec, tc, _ = _cli_login(app, monkeypatch, opened=False)
    assert code == 1
    assert rec.waits == []
    assert not rec.files[0]["path"].exists() and not rec.files[0]["path"].parent.exists()
