"""The local UI server: FastAPI behind one request guard, bound to 127.0.0.1 only.

The guard runs before every route and every WebSocket, in this order:
1. Host: exactly ``127.0.0.1:<port>`` or ``localhost:<port>``, else 400. This defeats DNS
   rebinding: a page on another name that resolves to this machine still sends its own Host.
2. Origin: a request that is not GET/HEAD, and every WebSocket, must carry this server's own
   origin, else 403. No CORS header is ever sent, so no other origin can read a response.
3. Session: outside the six login routes, a live session cookie whose key is still
   registered is required, else 401 (API), 303 to /login (pages), or a closed WebSocket.

There is no setting, flag or variable in the process's settings that changes the bind address or turns any
of this off; ``create_app`` and ``serve`` take none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect

from omli.ui import mindmap, pages
from omli.ui.auth import (
    CHALLENGE_TTL_SECONDS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    TICKET_TTL_SECONDS,
    AuthState,
    ChallengeLimitError,
    SessionLimitError,
    TicketLimitError,
)
from omli.ui.home import ui_home
from omli.ui.projects import ProjectLogError, ProjectStore

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PUBLIC_PATHS = frozenset({"/login", "/login.js", "/api/auth/challenge", "/api/auth/login",
                          "/api/auth/ticket", "/api/auth/redeem"})
SAFE_METHODS = frozenset({"GET", "HEAD"})
MAX_FIELD = 4096
# Request bodies are bounded by their declared length before anything reads them: small on
# the public login routes, larger behind a session (a whole board, or a dropped file as
# base64). A body with no declared length is refused.
PUBLIC_BODY_LIMIT = 16 * 1024
BODY_LIMIT = 16 * 1024 * 1024
_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
     b"img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
     b"form-action 'self'"),
]


def _header_values(scope, name: bytes):
    return [v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == name]


def _cookie(scope, name: str) -> Optional[str]:
    for raw in _header_values(scope, b"cookie"):
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name and value:
                return value
    return None


def _body_refusal(scope):
    """(status, reason) when the request's body is not acceptable by its declared length."""
    if any(v.strip().lower() != "identity" for v in _header_values(scope, b"transfer-encoding")):
        return 411, "a request body needs a Content-Length"
    lengths = _header_values(scope, b"content-length")
    if not lengths:
        return None  # no body
    if len(lengths) != 1 or not lengths[0].strip().isdigit():
        return 400, "bad Content-Length"
    limit = PUBLIC_BODY_LIMIT if scope["path"] in PUBLIC_PATHS else BODY_LIMIT
    if int(lengths[0]) > limit:
        return 413, f"the request body is over the {limit} byte limit"
    return None


class Guard:
    """Pure ASGI middleware, so it covers HTTP and WebSocket alike."""

    def __init__(self, app, port: int, state: AuthState):
        self.app = app
        self.state = state
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if kind == "http":
            send = self._with_headers(send)

        hosts = _header_values(scope, b"host")
        if len(hosts) != 1 or hosts[0].lower() not in self.hosts:
            await self._refuse(kind, 400, "this server answers on localhost only",
                               scope, receive, send)
            return

        if kind == "websocket" or scope["method"] not in SAFE_METHODS:
            origins = _header_values(scope, b"origin")
            if len(origins) != 1 or origins[0] not in self.origins:
                await self._refuse(kind, 403, "request from another origin",
                                   scope, receive, send)
                return

        if kind == "http" and scope["method"] not in SAFE_METHODS:
            refused = _body_refusal(scope)
            if refused:
                await self._refuse(kind, refused[0], refused[1], scope, receive, send)
                return

        if scope["path"] not in PUBLIC_PATHS:
            token = _cookie(scope, SESSION_COOKIE)
            session = self.state.session(token)
            if session is None:
                await self._refuse(kind, 401, "sign in first", scope, receive, send)
                return
            scope.setdefault("state", {})
            scope["state"]["session"] = session
            scope["state"]["session_token"] = token

        await self.app(scope, receive, send)

    @staticmethod
    def _with_headers(send):
        async def wrapped(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", [])
                        if not k.lower().startswith(b"access-control-")]
                message = {**message, "headers": kept + _HEADERS}
            await send(message)
        return wrapped

    @staticmethod
    async def _refuse(kind, status, reason, scope, receive, send):
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return
        if status == 401 and not scope["path"].startswith("/api/") \
                and scope["method"] in SAFE_METHODS:
            response = RedirectResponse("/login", status_code=303)
        else:
            response = JSONResponse({"error": reason}, status_code=status)
        await response(scope, receive, send)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _field(body: dict, name: str):
    value = body.get(name)
    return value if isinstance(value, str) and len(value) <= MAX_FIELD else None


def create_app(port: int = DEFAULT_PORT, home: str | Path | None = None,
               clock: Optional[Callable[[], float]] = None) -> FastAPI:
    home = ui_home(home)  # OMLI_HOME, else the per-user folder; never the current folder
    state = AuthState(home=home, clock=clock)
    store = ProjectStore(home=home)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = state

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return pages.login_page(state.has_keys())

    @app.get("/login.js")
    async def login_js():
        return Response(pages.LOGIN_JS, media_type="text/javascript")

    @app.post("/api/auth/challenge")
    async def challenge():
        try:
            issued = state.issue_challenge()
        except ChallengeLimitError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        if issued is None:
            return JSONResponse({"error": "no login key is registered; "
                                          "run: omli-ui init"}, status_code=409)
        return {"challenge": issued, "expires_in": CHALLENGE_TTL_SECONDS}

    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await _json_body(request)
        try:
            result = state.login(_field(body, "challenge"), _field(body, "token"))
        except SessionLimitError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        return _signed_in(result)

    def _signed_in(result):
        if result is None:
            return JSONResponse({"error": "sign-in failed"}, status_code=401)
        token, session = result
        response = JSONResponse({"pubkey_id": session.pubkey_id})
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, path="/",
                            httponly=True, samesite="strict")
        return response

    @app.post("/api/auth/ticket")
    async def ticket(request: Request):
        # Called by `omli-ui login`, which has just signed the challenge.
        body = await _json_body(request)
        try:
            issued = state.issue_ticket(_field(body, "challenge"), _field(body, "token"))
        except TicketLimitError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        if issued is None:
            return JSONResponse({"error": "sign-in failed"}, status_code=401)
        return {"ticket": issued, "expires_in": TICKET_TTL_SECONDS}

    @app.post("/api/auth/redeem")
    async def redeem(request: Request):
        # Called by the login page with the ticket from its address fragment. The ticket is
        # read from the body only, and the answer is never a redirect.
        body = await _json_body(request)
        try:
            result = state.redeem_ticket(_field(body, "ticket"))
        except SessionLimitError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        return _signed_in(result)

    @app.get("/api/auth/me")
    async def me(request: Request):
        s = request.state.session
        return {"pubkey_id": s.pubkey_id, "expires_at": s.expires_at}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        state.logout(request.state.session_token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home_page():
        return pages.app_page()

    @app.get("/app.js")
    async def app_js():
        return Response(pages.APP_JS, media_type="text/javascript")

    def _projects_call(fn):
        try:
            return fn()
        except ProjectLogError:
            return JSONResponse({"error": "the project list file cannot be read"},
                                status_code=500)
        except KeyError:
            return JSONResponse({"error": "that folder is not in the project list"},
                                status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/projects")
    async def projects():
        return await mindmap.offload(_projects_call, lambda: {"projects": store.list()})

    @app.post("/api/projects/add")
    async def projects_add(request: Request):
        path = _field(await _json_body(request), "path")
        by = request.state.session.pubkey_id
        return await mindmap.offload(_projects_call, lambda: {"path": store.add(path, by=by)})

    @app.post("/api/projects/scan")
    async def projects_scan(request: Request):
        path = _field(await _json_body(request), "path")
        by = request.state.session.pubkey_id
        return await mindmap.offload(_projects_call, lambda: {"added": store.scan(path, by=by)})

    @app.post("/api/projects/remove")
    async def projects_remove(request: Request):
        path = _field(await _json_body(request), "path")
        by = request.state.session.pubkey_id
        return await mindmap.offload(_projects_call, lambda: {"removed": store.remove(path, by=by)})

    mindmap.register(app, home=home, projects=store)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"type": "hello",
                                   "pubkey_id": websocket.scope["state"]["session"].pubkey_id})
        try:
            while True:
                if await websocket.receive_text() == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            return

    app.add_middleware(Guard, port=port, state=state)
    return app


def serve(port: int = DEFAULT_PORT, home: str | Path | None = None) -> None:
    if not 1 <= int(port) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    uvicorn.run(create_app(port=port, home=home), host=HOST, port=int(port),
                proxy_headers=False, server_header=False, log_level="info")
