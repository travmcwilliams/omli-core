"""``omli-ui login``: sign in the browser in one step with a one-time login ticket.

1. Ask the local server for a challenge.
2. Sign it here, with the private key unlocked by the passphrase. Neither leaves this process.
3. Send the signed answer; the server checks the signature and returns a ticket, which works
   once and only for a short time.
4. Write a small hand-off page into a new private temporary folder (only this user can read
   it) and open that file in the browser. The browser is started with the file's path, never
   with the ticket, so the ticket is on no process's command line. The page sends the browser
   to ``/login#ticket=...``. A fragment is never sent to the server, so the ticket appears in
   no request line and no server log. The login page removes it from the address, redeems it
   for the usual session cookie, and goes to ``/``.
5. Once the ticket's lifetime has passed, remove the hand-off file and its folder. If no
   browser opens, they are removed at once.

The ticket is never printed by this command, and is written only to the hand-off file. If no
browser opens, the command says so and points to the copy-and-paste sign-in instead.

The transport talks to 127.0.0.1 directly: proxy settings are ignored, so the signed answer
and the ticket never pass through another program.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from omli.programs import find_program, start_file, start_program

HOST = "127.0.0.1"
# Browser launchers started on macOS and Linux, kept until they finish: dropping the handle of
# a running child makes Python report it as a ResourceWarning.
_STARTED: list = []
Post = Callable[[str, dict], Tuple[int, Dict]]


class LoginError(Exception):
    """The one-step sign-in failed. The message is one line, safe to show the user."""


def http_post(port: int) -> Post:
    """A JSON POST to the local server on ``port``, with this server's own Origin."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = f"http://{HOST}:{int(port)}"

    def post(path: str, body: dict) -> Tuple[int, Dict]:
        req = urllib.request.Request(origin + path, data=json.dumps(body).encode("utf-8"),
                                     method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Origin": origin})
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    return exc.code, json.loads(exc.read() or b"{}")
                except ValueError:
                    return exc.code, {}

    return post


def browser_launcher() -> Optional[str]:
    """The program that opens a file in the default browser on macOS and Linux, found only in
    an absolute PATH folder (never the current folder). Windows needs none."""
    return find_program("open" if sys.platform == "darwin" else "xdg-open")


def open_browser(url: str) -> bool:
    """Open ``url`` in the default browser. Windows hands it to the system's file
    association; macOS and Linux run ``open`` or ``xdg-open`` from PATH as found by
    ``browser_launcher``. Either way the program starts outside the current folder, with a
    PATH holding only absolute folders (see ``omli.programs``). False when no browser can be
    started."""
    if os.name == "nt" and sys.platform == "win32":
        try:
            start_file(url)
            return True
        except OSError:
            return False
    launcher = browser_launcher()
    if launcher is None:
        return False
    try:
        proc = start_program([launcher, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    _STARTED[:] = [p for p in _STARTED if p.poll() is None]  # forget launchers that finished
    _STARTED.append(proc)
    return True


def ticket_url(port: int, ticket: str) -> str:
    return f"http://{HOST}:{int(port)}/login#ticket={ticket}"


def wait(seconds: float) -> None:
    time.sleep(seconds)


_HANDOFF = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="referrer" content="no-referrer">'
            '<meta http-equiv="refresh" content="0;url={url}">'
            '<title>OMLi sign-in</title></head><body>Signing in to OMLi...</body></html>')


def write_handoff(url: str) -> Path:
    """A new private folder holding one page that sends the browser to ``url``. The folder is
    created for this user only (0700 on POSIX; the user's own temporary folder on Windows),
    and the file is created exclusively with 0600."""
    folder = Path(tempfile.mkdtemp(prefix="omli-sign-in-"))
    path = folder / "sign-in.html"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(_HANDOFF.format(url=url).encode("utf-8"))
    return path


def remove_handoff(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


def run_login(port: int, sign: Callable[[str], str], post: Post,
              open_url: Callable[[str], bool],
              on_opened: Callable[[], None] | None = None) -> None:
    """Do the exchange. ``sign`` turns a challenge into a signed answer on this machine."""
    try:
        status, data = post("/api/auth/challenge", {})
    except OSError:
        raise LoginError(f"no OMLi UI server is answering on {HOST}:{int(port)}; "
                         "start it with: omli-ui serve") from None
    if status != 200 or not isinstance(data.get("challenge"), str):
        raise LoginError(str(data.get("error") or f"the server refused a code ({status})"))
    challenge = data["challenge"]
    token = sign(challenge)
    try:
        status, data = post("/api/auth/ticket", {"challenge": challenge, "token": token})
    except OSError:
        raise LoginError("the server stopped answering; try again") from None
    if status != 200 or not isinstance(data.get("ticket"), str):
        raise LoginError(str(data.get("error") or f"the server refused the sign-in ({status})"))
    lifetime = data.get("expires_in")
    lifetime = lifetime if isinstance(lifetime, int) and 0 < lifetime <= 60 else 60
    handoff = write_handoff(ticket_url(port, data["ticket"]))
    try:
        if not open_url(handoff.as_uri()):
            raise LoginError("no browser could be opened; sign in on the login page with "
                             "a code and omli-ui sign instead")
        if on_opened is not None:
            on_opened()
        wait(lifetime)  # the browser reads the file in this time; after it, the ticket is dead
    finally:
        remove_handoff(handoff)


__all__ = ["LoginError", "http_post", "open_browser", "ticket_url", "run_login",
           "write_handoff", "remove_handoff", "wait"]
