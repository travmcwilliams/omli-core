"""The Mind-map routes on omli.ui and the board log.

* Every new route refuses a request with no session; every state-changing one refuses a
  foreign Origin. The app registers exactly the routes on an allow-list, nothing else.
* Every model call (expand, analyze, interpret, dump sorting, the retrieval summary) runs
  Agent.run with the public pre-gate and post-gate, the model named by the registry for the
  pack's role. With a fake provider: one call per allowed request; none when the pre-gate
  refuses; nothing written when the post-gate refuses; none and nothing written with no model.
* Save appends with the single-write call and makes no model call.
* Board content tagged local-only is refused for a provider kind that is not local.
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
from starlette.testclient import TestClient  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}
FOREIGN = {"Origin": "http://attacker.example"}
ROLE = "mindmap-assistant"

NEW_ROUTES = [
    ("GET", "/api/mindmap/list"),
    ("GET", "/api/mindmap/boards/b1"),
    ("POST", "/api/mindmap/save"),
    ("GET", "/api/mindmap/boards/b1/attribution"),
    ("GET", "/api/mindmap/boards/b1/checkpoints"),
    ("POST", "/api/mindmap/boards/b1/checkpoints"),
    ("POST", "/api/mindmap/boards/b1/restore"),
    ("POST", "/api/mindmap/expand"),
    ("POST", "/api/mindmap/analyze"),
    ("POST", "/api/mindmap/interpret"),
    ("POST", "/api/mindmap/icon"),
    ("POST", "/api/mindmap/dump"),
    ("GET", "/api/mindmap/dump/config"),
    ("GET", "/api/mindmap/rag/catalog"),
    ("POST", "/api/mindmap/rag/validate"),
    ("POST", "/api/mindmap/rag/run"),
    ("POST", "/api/mindmap/autodesign/question"),
    ("POST", "/api/mindmap/autodesign/build"),
]
# Every (method, path) the app registers. A route not on this list fails the test below,
# so nothing outside this release can be registered without a reviewed edit here.
ALLOWED_ROUTES = {
    ("GET", "/login"), ("GET", "/login.js"), ("POST", "/api/auth/challenge"),
    ("POST", "/api/auth/login"), ("POST", "/api/auth/ticket"), ("POST", "/api/auth/redeem"),
    ("GET", "/api/auth/me"), ("POST", "/api/auth/logout"), ("GET", "/"), ("GET", "/app.js"),
    ("GET", "/api/projects"), ("POST", "/api/projects/add"), ("POST", "/api/projects/scan"),
    ("POST", "/api/projects/remove"), ("WS", "/ws"),
    ("GET", "/mindmap"), ("GET", "/mindmap/{asset:path}"), ("GET", "/api/mindmap/assist/status"),
    *((m, p.replace("/b1", "/{board}")) for m, p in NEW_ROUTES),
}


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


def _id(route):
    return f"{route[0]} {route[1]}"


# --- a fake model provider and a registry that names it ---------------------------------

class FakeProvider:
    def __init__(self, text="first idea\nsecond idea"):
        self.text = text
        self.calls = []

    def invoke(self, entry, request):
        from omli.models.provider import ModelResponse
        self.calls.append((entry.model_id, entry.provider_kind, request))
        return ModelResponse(text=self.text, served_model_id="fake-served")


def _registry_yaml(provider_kind):
    hosted = provider_kind != "local-ollama"
    entry = {
        "model_id": ROLE, "display_name": "Fake model", "family": "fake",
        "provider_kind": provider_kind, "adapter": "fake",
        "endpoint": "https://api.example.invalid/v1" if hosted else "http://localhost:9",
        "pin": {"ref": "fake-2026" if hosted else "sha256:" + "0" * 64,
                "quant": None if hosted else "Q4_K_M"},
        "reproducible": not hosted, "status": "staging",
        "capabilities": {"context_window": 8192, "max_output_tokens": 1024,
                         "native_tool_calling": False, "parallel_tool_calls": False,
                         "structured_output": "none", "thinking_mode": False,
                         "vision": False},
        "params_default": {"temperature": 0.0},
    }
    if hosted:
        entry["api_key_env"] = "FAKE_MODEL_API_KEY"
    else:
        entry["serving_tag"] = "fake:1b"
    return json.dumps({"metadata": {"registry_id": "test-registry", "registry_version": "1.0.0",
                                    "authored_at": "2026-11-11T00:00:00Z",
                                    "authored_by": "test", "status": "staging"},
                       "models": [entry]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "Omli Home"
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "User Dir" / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "mindmap test passphrase")
    monkeypatch.delenv("OMLI_SEAL_KEY", raising=False)
    auth = _load("omli.ui.auth")
    auth.create_login_key()
    return home.resolve(), auth


def _with_model(home, provider_kind="local-ollama"):
    (home / "models").mkdir(parents=True, exist_ok=True)
    (home / "models" / "registry.yaml").write_text(_registry_yaml(provider_kind),
                                                   encoding="utf-8")


@pytest.fixture
def fake():
    return FakeProvider()


@pytest.fixture
def app(env, fake, monkeypatch):
    # The model adapter table is swapped where the assist module looks it up; create_app
    # itself takes no extra parameter.
    assist = _load("omli.boards.assist")
    monkeypatch.setattr(assist, "default_provider_table", lambda: {"fake": fake})
    return _load("omli.ui.server").create_app(port=PORT)


def _client(app):
    return TestClient(app, base_url=BASE)


def _login(c, auth):
    ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    r = c.post("/api/auth/login", headers=OWN,
               json={"challenge": ch, "token": auth.sign_challenge(ch)})
    assert r.status_code == 200, r.text
    return r.json()["pubkey_id"]


def _snapshot(home):
    """Every file under OMLI_HOME that a model route could write, with its bytes."""
    out = {}
    for sub in ("boards", "var"):
        root = home / sub
        if root.exists():
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.name != "index.json" and p.suffix != ".lock":
                    out[str(p.relative_to(home))] = p.read_bytes()
    return out


def _head(c, board):
    r = c.get(f"/api/mindmap/boards/{board}", headers=OWN)
    return r.json()["head"] if r.status_code == 200 else None


def _save(c, board, nodes, links=None, base="current"):
    """Save as the page does: based on a version of the board. By default, the current one."""
    body = {"board": board, "nodes": nodes, "links": links or {},
            "base": _head(c, board) if base == "current" else base}
    return c.post("/api/mindmap/save", headers=OWN, json=body)


# --- route table: registered, sessions, origins, drops ----------------------------------

def test_every_new_route_is_registered(app):
    have = {(m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ())}
    want = {(m, p.replace("/b1", "/{board}")) for m, p in NEW_ROUTES}
    assert want <= have, sorted(want - have)


@pytest.mark.parametrize("route", NEW_ROUTES, ids=_id)
def test_a_new_route_refuses_a_request_with_no_session(app, route):
    method, path = route
    with _client(app) as c:
        r = c.request(method, path, headers=OWN, json={})
    assert r.status_code == 401


@pytest.mark.parametrize("route", [r for r in NEW_ROUTES if r[0] == "POST"], ids=_id)
def test_a_state_changing_route_refuses_a_foreign_origin(app, env, route):
    home, auth = env
    method, path = route
    with _client(app) as c:
        _login(c, auth)
        before = _snapshot(home)
        r = c.request(method, path, headers=FOREIGN, json={"board": "b1", "nodes": {}})
    assert r.status_code == 403
    assert _snapshot(home) == before


def _registered(app):
    out = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            out.add(("WS", route.path))
        else:
            out.update((m, route.path) for m in methods if m != "HEAD")
    return out


def test_the_registered_routes_equal_the_allow_list_exactly(app):
    assert _registered(app) == ALLOWED_ROUTES


def test_a_route_not_on_the_allow_list_fails_the_check(app):
    @app.get("/api/not-in-the-plan")
    async def extra():
        return {}

    assert _registered(app) - ALLOWED_ROUTES == {("GET", "/api/not-in-the-plan")}


# --- boards: load, list, save, attribution, restore, checkpoints ------------------------

def test_save_load_list_and_attribution(app, env, fake):
    home, auth = env
    _with_model(home)
    with _client(app) as c:
        me = _login(c, auth)
        r = _save(c, "plans", {"a": {"text": "alpha apples"}, "b": {"text": "bravo"}},
                  {"l1": {"from": "a", "to": "b", "relation": "flow"}})
        assert r.status_code == 200, r.text
        assert r.json()["written"] == 3
        head = r.json()["head"]
        board = c.get("/api/mindmap/boards/plans", headers=OWN).json()
        assert board["nodes"]["a"]["text"] == "alpha apples" and board["head"] == head
        assert board["relations"]["flow"]
        assert _save(c, "plans", {"a": {"text": "alpha two"}}, base="stale").status_code == 409
        r = _save(c, "plans", {"a": {"text": "alpha two"}}, base=head)
        assert r.status_code == 200 and r.json()["written"] == 2
        assert _save(c, "plans", {"a": {"text": "alpha two"}}).json()["written"] == 0
        listed = c.get("/api/mindmap/list", params={"q": "alpha"}, headers=OWN).json()
        assert [b["board"] for b in listed["boards"]] == ["plans"]
        stats = c.get("/api/mindmap/boards/plans/attribution", headers=OWN).json()
        assert stats["nodes"]["a"]["edits"] == 2 and stats["nodes"]["a"]["last_by"] == me
        assert c.get("/api/mindmap/boards/missing", headers=OWN).status_code == 404
        assert c.get("/api/mindmap/boards/Bad_Id", headers=OWN).status_code == 400
        assert _save(c, "Bad_Id", {}).status_code == 400
        assert _save(c, "plans", {"x": "not an object"}).status_code == 400
    assert fake.calls == []  # a save never calls a model


def test_a_save_appends_with_one_write_and_triggers_no_model_call(app, env, fake, monkeypatch):
    home, auth = env
    _with_model(home)
    log_mod = _load("omli.boards.log")
    writes = []
    real = log_mod.BoardLog._write_line
    monkeypatch.setattr(log_mod.BoardLog, "_write_line",
                        lambda self, data: writes.append(data.count(b"\n")) or real(self, data))
    monkeypatch.setattr(log_mod.BoardLog, "append",
                        lambda *a, **k: pytest.fail("save must use the single-write call"))
    with _client(app) as c:
        _login(c, auth)
        nodes = {f"n{i}": {"text": f"idea {i}"} for i in range(30)}
        assert _save(c, "big", nodes).status_code == 200
    assert writes == [30], "one save is one write holding every operation"
    assert fake.calls == []
    assert not (home / "var" / "artifacts.jsonl").exists()


def test_restore_appends_and_checkpoints_keep_the_chain(app, env, fake):
    home, auth = env
    with _client(app) as c:
        _login(c, auth)
        first = _save(c, "story", {"a": {"text": "one"}}).json()["head"]
        _save(c, "story", {"a": {"text": "two"}, "b": {"text": "new"}})
        lines_before = (home / "boards" / "story.jsonl").read_bytes()
        r = c.post("/api/mindmap/boards/story/restore", headers=OWN,
                   json={"to": first, "base": _head(c, "story")})
        assert r.status_code == 200, r.text
        assert (home / "boards" / "story.jsonl").read_bytes().startswith(lines_before)
        assert c.get("/api/mindmap/boards/story", headers=OWN).json()["nodes"] == \
            {"a": {"text": "one"}}
        assert c.post("/api/mindmap/boards/story/restore", headers=OWN,
                      json={"to": "nope", "base": _head(c, "story")}).status_code == 400
        r = c.post("/api/mindmap/boards/story/checkpoints", headers=OWN, json={"label": "v1"})
        assert r.status_code == 200 and r.json()["created"] is True
        listed = c.get("/api/mindmap/boards/story/checkpoints", headers=OWN).json()
        assert [x["label"] for x in listed["checkpoints"]] == ["v1"]
    store = _load("omli.artifacts").ArtifactStore.from_log(home / "var" / "artifacts.jsonl")
    assert [a.kind for a in store.all()] == ["CanonicalInput", "Spec", "DecisionRecord"]
    assert fake.calls == []


# --- the model routes: gates on both sides, registry-named model ------------------------

def _model_requests(c):
    _save(c, "ideas", {"a": {"text": "grow apples"}, "b": {"text": "sell cider"}},
          {"l": {"from": "a", "to": "b"}})
    return {
        "expand": ("/api/mindmap/expand", {"board": "ideas", "node": "a"}),
        "analyze": ("/api/mindmap/analyze", {"board": "ideas", "node": "a"}),
        "interpret": ("/api/mindmap/interpret", {"board": "ideas"}),
        "dump": ("/api/mindmap/dump", {"filename": "notes.md",
                                       "content": base64.b64encode(b"apples and pears").decode()}),
    }


@pytest.mark.parametrize("action", ["expand", "analyze", "interpret", "dump"])
def test_one_provider_call_per_allowed_request_and_the_chain_is_written(app, env, fake, action):
    home, auth = env
    _with_model(home)
    with _client(app) as c:
        _login(c, auth)
        path, body = _model_requests(c)[action]
        before = _snapshot(home)
        r = c.post(path, headers=OWN, json=body)
    assert r.status_code == 200, r.text
    assert len(fake.calls) == 1 and fake.calls[0][0] == ROLE
    assert r.json()["text"] == "first idea\nsecond idea"
    key = str(Path("boards") / "ideas.jsonl")
    assert _snapshot(home)[key] == before[key]  # a model call never edits the board
    store = _load("omli.artifacts").ArtifactStore.from_log(home / "var" / "artifacts.jsonl")
    ci, out = store.all()
    assert (ci.kind, out.kind) == ("CanonicalInput", "WorkResult")
    assert out.provenance.model_ref.model_id == ROLE
    assert out.provenance.pack_name == f"mindmap-{action}"


def test_the_retrieval_summary_is_one_call_over_listed_folders(app, env, fake, tmp_path):
    home, auth = env
    _with_model(home)
    proj = tmp_path / "My Project"
    proj.mkdir()
    (proj / "notes.md").write_text("orchard plans for apples", encoding="utf-8")
    ladder = {"nodes": {"s": {"kind": "source", "catalog_ref": {"path": str(proj)}},
                        "a": {"kind": "agent", "catalog_ref": {"model_id": ROLE}},
                        "o": {"kind": "output"}},
              "links": {"1": {"from": "s", "to": "a", "relation": "flow"},
                        "2": {"from": "a", "to": "o", "relation": "flow"}}}
    with _client(app) as c:
        _login(c, auth)
        assert c.post("/api/projects/add", headers=OWN, json={"path": str(proj)}).status_code == 200
        cat = c.get("/api/mindmap/rag/catalog", headers=OWN).json()
        assert [s["path"] for s in cat["sources"]] == [str(proj.resolve())]
        assert [a["id"] for a in cat["agents"]] == [ROLE]
        ok = c.post("/api/mindmap/rag/validate", headers=OWN, json=ladder).json()
        assert ok["ok"] is True
        r = c.post("/api/mindmap/rag/run", headers=OWN, json={**ladder, "query": "apples"})
        assert r.status_code == 200, r.text
        assert [h["file"] for h in r.json()["hits"]] == ["notes.md"]
        outside = dict(ladder, nodes={**ladder["nodes"], "s": {
            "kind": "source", "catalog_ref": {"path": str(tmp_path)}}})
        assert c.post("/api/mindmap/rag/run", headers=OWN,
                      json={**outside, "query": "apples"}).status_code == 403
    assert len(fake.calls) == 1


@pytest.mark.parametrize("action", ["expand", "analyze", "interpret"])
def test_no_provider_call_when_the_pre_gate_refuses(app, env, fake, action):
    home, auth = env
    _with_model(home)
    with _client(app) as c:
        _login(c, auth)
        _save(c, "blank", {"a": {}})
        before = _snapshot(home)
        body = {"board": "blank", "node": "a"} if action != "interpret" else {"board": "blank"}
        r = c.post(f"/api/mindmap/{action}", headers=OWN, json=body)
    assert r.status_code == 422 and r.json()["refused"] == "pre-gate"
    assert fake.calls == []
    assert _snapshot(home) == before


@pytest.mark.parametrize("action", ["expand", "analyze", "interpret", "dump"])
def test_nothing_is_written_when_the_post_gate_refuses(app, env, fake, action):
    home, auth = env
    _with_model(home)
    fake.text = ""  # the post-gate requires a non-empty text field
    with _client(app) as c:
        _login(c, auth)
        path, body = _model_requests(c)[action]
        before = _snapshot(home)
        r = c.post(path, headers=OWN, json=body)
    assert r.status_code == 422 and r.json()["refused"] == "post-gate"
    assert len(fake.calls) == 1
    assert _snapshot(home) == before


@pytest.mark.parametrize("registry", ["absent", "no-entry-for-the-role"])
@pytest.mark.parametrize("action", ["expand", "analyze", "interpret", "dump"])
def test_with_no_model_configured_the_refusal_is_clear(app, env, fake, action, registry):
    home, auth = env
    if registry != "absent":
        (home / "models").mkdir(parents=True)
        (home / "models" / "registry.yaml").write_text(
            _registry_yaml("local-ollama").replace(ROLE, "another-role"), encoding="utf-8")
    with _client(app) as c:
        _login(c, auth)
        path, body = _model_requests(c)[action]
        before = _snapshot(home)
        r = c.post(path, headers=OWN, json=body)
    assert r.status_code == 409 and "no model configured" in r.json()["error"]
    assert fake.calls == []
    assert _snapshot(home) == before


# --- sensitivity: local-only content never reaches a provider that is not local ---------

@pytest.mark.parametrize("action", ["expand", "analyze", "interpret"])
def test_local_only_board_content_is_refused_for_a_hosted_provider(app, env, fake, action):
    home, auth = env
    _with_model(home, "openai-compatible")
    with _client(app) as c:
        _login(c, auth)
        _save(c, "private", {"a": {"text": "grow apples", "sensitivity": ["local-only"]},
                             "b": {"text": "public part"}})
        before = _snapshot(home)
        body = {"board": "private", "node": "b"} if action != "interpret" else {"board": "private"}
        r = c.post(f"/api/mindmap/{action}", headers=OWN, json=body)
    assert r.status_code == 422 and r.json()["refused"] == "pre-gate"
    assert "local-only" in r.json()["reason"]
    assert fake.calls == []
    assert _snapshot(home) == before


def test_local_only_content_runs_on_a_local_provider(app, env, fake):
    home, auth = env
    _with_model(home, "local-ollama")
    with _client(app) as c:
        _login(c, auth)
        _save(c, "private", {"a": {"text": "grow apples", "sensitivity": ["local-only"]}})
        r = c.post("/api/mindmap/expand", headers=OWN, json={"board": "private", "node": "a"})
    assert r.status_code == 200 and len(fake.calls) == 1


def test_a_local_only_dropped_file_is_refused_for_a_hosted_provider(app, env, fake):
    home, auth = env
    _with_model(home, "openai-compatible")
    with _client(app) as c:
        _login(c, auth)
        r = c.post("/api/mindmap/dump", headers=OWN, json={
            "filename": "n.txt", "content": base64.b64encode(b"private words").decode(),
            "sensitivity": ["local-only"]})
    assert r.status_code == 422 and fake.calls == []


# --- icon, dump config and the offline auto-design --------------------------------------

def test_icon_dump_config_and_the_offline_auto_design(app, env, fake):
    _, auth = env
    with _client(app) as c:
        _login(c, auth)
        icon = c.post("/api/mindmap/icon", headers=OWN, json={"text": "a database"}).json()
        assert icon["icon"]["name"] == "database"
        cfg = c.get("/api/mindmap/dump/config", headers=OWN).json()
        assert {"entity_types", "sensitivity", "status", "tags", "editable",
                "file_types"} <= set(cfg)
        assert "local-only" in cfg["sensitivity"]
        q = c.post("/api/mindmap/autodesign/question", headers=OWN, json={"answers": {}}).json()
        assert q["done"] is False and q["offline"] is True
        b = c.post("/api/mindmap/autodesign/build", headers=OWN,
                   json={"answers": {"goal": "a garden"}}).json()
        assert b["offline"] is True and b["board"]["nodes"]
        bad = c.post("/api/mindmap/rag/validate", headers=OWN, json={"nodes": []})
        assert bad.status_code == 400
    assert fake.calls == []


def test_dump_refuses_a_type_off_the_list_a_large_file_and_names_a_missing_library(
        app, env, fake, monkeypatch):
    home, auth = env
    _with_model(home)
    with _client(app) as c:
        _login(c, auth)
        exe = c.post("/api/mindmap/dump", headers=OWN, json={
            "filename": "tool.exe", "content": base64.b64encode(b"MZ").decode()})
        assert exe.status_code == 415
        big = c.post("/api/mindmap/dump", headers=OWN, json={
            "filename": "big.txt", "content": base64.b64encode(b"x" * 6_000_000).decode()})
        assert big.status_code == 413
        monkeypatch.setitem(sys.modules, "pypdf", None)
        pdf = c.post("/api/mindmap/dump", headers=OWN, json={
            "filename": "doc.pdf", "content": base64.b64encode(b"%PDF-1.4").decode()})
        assert pdf.status_code == 422 and "pypdf" in pdf.json()["error"]
        assert c.post("/api/mindmap/dump", headers=OWN, json={
            "filename": "n.txt", "content": "%%% not base64"}).status_code == 400
    assert fake.calls == []


# --- a slow model call or folder scan does not stop the server answering others ---------

class BlockingProvider(FakeProvider):
    """Holds a model call open until the test releases it."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def invoke(self, entry, request):
        self.entered.set()
        self.release.wait(20)
        return super().invoke(entry, request)


def _answered_meanwhile(c, start_slow, entered, release):
    """Start the slow request, then ask for /api/auth/me while it is held open."""
    slow, other = {}, {}
    t1 = threading.Thread(target=lambda: slow.update(r=start_slow()))
    t1.start()
    try:
        assert entered.wait(20), "the slow request never started"
        t2 = threading.Thread(target=lambda: other.update(r=c.get("/api/auth/me", headers=OWN)))
        t2.start()
        t2.join(5)
        answered = "r" in other and other["r"].status_code == 200
    finally:
        release.set()
        t1.join(30)
    t2.join(30)
    return answered, slow.get("r")


@pytest.mark.parametrize("action", ["expand", "analyze", "interpret", "dump"])
def test_a_slow_model_call_does_not_hold_up_other_requests(env, monkeypatch, action):
    home, auth = env
    _with_model(home)
    held = BlockingProvider()
    monkeypatch.setattr(_load("omli.boards.assist"), "default_provider_table",
                        lambda: {"fake": held})
    app = _load("omli.ui.server").create_app(port=PORT)
    with _client(app) as c:
        _login(c, auth)
        path, body = _model_requests(c)[action]
        answered, slow = _answered_meanwhile(
            c, lambda: c.post(path, headers=OWN, json=body), held.entered, held.release)
    assert answered, "another request waited for the model call to finish"
    assert slow is not None and slow.status_code == 200


def test_a_slow_folder_scan_does_not_hold_up_other_requests(env, monkeypatch, tmp_path):
    home, auth = env
    projects = _load("omli.ui.projects")
    entered, release = threading.Event(), threading.Event()

    def slow_scan(self, raw, *, by=None):
        entered.set()
        release.wait(20)
        return []

    monkeypatch.setattr(projects.ProjectStore, "scan", slow_scan)
    app = _load("omli.ui.server").create_app(port=PORT)
    with _client(app) as c:
        _login(c, auth)
        answered, slow = _answered_meanwhile(
            c, lambda: c.post("/api/projects/scan", headers=OWN, json={"path": str(tmp_path)}),
            entered, release)
    assert answered, "another request waited for the folder scan to finish"
    assert slow is not None and slow.status_code == 200


# --- two saves based on the same version: one wins, the other is told to reload ----------

def test_two_saves_based_on_the_same_version_cannot_both_land(app, env, monkeypatch):
    home, auth = env
    mindmap = _load("omli.ui.mindmap")
    real_ops_for = mindmap.ops_for
    gate = threading.Barrier(2, timeout=3)

    def ops_for_after_both_arrive(state, board):
        try:
            gate.wait()  # hold both saves inside the window between the check and the write
        except threading.BrokenBarrierError:
            pass
        return real_ops_for(state, board)

    with _client(app) as c:
        _login(c, auth)
        head = _save(c, "race", {"n1": {"text": "start"}}).json()["head"]
        monkeypatch.setattr(mindmap, "ops_for", ops_for_after_both_arrive)
        results = {}

        def save(name, text):
            results[name] = c.post("/api/mindmap/save", headers=OWN, json={
                "board": "race", "base": head, "nodes": {"n1": {"text": text}}, "links": {}})

        threads = [threading.Thread(target=save, args=(n, t)) for n, t in (("a", "A"), ("b", "B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], f"both saves based on {head} landed: {codes}"
    log = _load("omli.boards").BoardLog("race", home)
    assert [r["kind"] for r in log.ops()].count("node.edit") == 1


# --- a save never silently undoes another tab's edit -----------------------------------

@pytest.mark.parametrize("first", ["a", "b"])
def test_two_tabs_on_the_same_version_the_second_save_is_refused(app, env, first):
    home, auth = env
    with _client(app) as c:
        _login(c, auth)
        head = _save(c, "tabs", {"n1": {"text": "start"}}).json()["head"]
        edits = {"a": {"n1": {"text": "edit from tab A"}}, "b": {"n1": {"text": "edit from tab B"}}}
        second = "b" if first == "a" else "a"
        r1 = _save(c, "tabs", edits[first], base=head)
        r2 = _save(c, "tabs", edits[second], base=head)
        assert r1.status_code == 200
        assert r2.status_code == 409 and r2.json()["head"] == r1.json()["head"]
        board = c.get("/api/mindmap/boards/tabs", headers=OWN).json()
    assert board["nodes"]["n1"]["text"] == f"edit from tab {first.upper()}"


def test_a_save_that_names_no_version_is_refused_for_an_existing_board(app, env):
    home, auth = env
    with _client(app) as c:
        _login(c, auth)
        _save(c, "nobase", {"n1": {"text": "start"}})
        r = c.post("/api/mindmap/save", headers=OWN,
                   json={"board": "nobase", "nodes": {}, "links": {}})
        assert r.status_code == 409
        assert c.get("/api/mindmap/boards/nobase", headers=OWN).json()["nodes"]["n1"]


def test_a_save_that_writes_nothing_reports_the_version_it_was_checked_against(
        app, env, monkeypatch):
    """Tab A saves with no changes while tab B's edit lands right after A's check. A must be
    told the version it was checked against, so its next save is refused, not applied over
    B's edit."""
    home, auth = env
    boards = _load("omli.boards")
    with _client(app) as c:
        _login(c, auth)
        head = _save(c, "quiet", {"n1": {"text": "orig"}}).json()["head"]
        real_save, landed = boards.BoardLog.save, []

        def save_then_tab_b_lands(self, make_ops, **kw):
            out = real_save(self, make_ops, **kw)
            if not landed:
                landed.append(real_save(self, lambda s: [{"kind": "node.edit", "node": "n1",
                                                          "data": {"text": "from tab B"}}],
                                        expect_head=head))
            return out

        monkeypatch.setattr(boards.BoardLog, "save", save_then_tab_b_lands)
        a = _save(c, "quiet", {"n1": {"text": "orig"}}, base=head)
        monkeypatch.setattr(boards.BoardLog, "save", real_save)
        assert a.status_code == 200 and a.json()["written"] == 0
        assert a.json()["head"] == head, "tab A was handed a version it never loaded"
        again = _save(c, "quiet", {"n1": {"text": "orig"}, "n2": {"text": "more"}},
                      base=a.json()["head"])
        assert again.status_code == 409
        board = c.get("/api/mindmap/boards/quiet", headers=OWN).json()
    assert board["nodes"]["n1"]["text"] == "from tab B"


def test_retrieval_never_reads_the_ui_data_folder_inside_a_listed_project(app, env, fake,
                                                                         tmp_path):
    home, auth = env  # the data folder (OMLI_HOME) is inside tmp_path
    _with_model(home)
    (tmp_path / "notes.md").write_text("orchard plans for apples", encoding="utf-8")
    (home / "notes-in-data.md").write_text("apples kept in the data folder", encoding="utf-8")
    ladder = {"nodes": {"s": {"kind": "source", "catalog_ref": {"path": str(tmp_path)}},
                        "a": {"kind": "agent", "catalog_ref": {"model_id": ROLE}},
                        "o": {"kind": "output"}},
              "links": {"1": {"from": "s", "to": "a", "relation": "flow"},
                        "2": {"from": "a", "to": "o", "relation": "flow"}}}
    with _client(app) as c:
        _login(c, auth)
        assert c.post("/api/projects/add", headers=OWN,
                      json={"path": str(tmp_path)}).status_code == 200
        r = c.post("/api/mindmap/rag/run", headers=OWN, json={**ladder, "query": "apples"})
        assert r.status_code == 200, r.text
        assert [h["file"] for h in r.json()["hits"]] == ["notes.md"]


# --- a restore is checked against the version it was based on, like a save ---------------

def test_a_restore_from_a_stale_tab_is_refused_and_keeps_the_other_tabs_save(app, env):
    home, auth = env
    with _client(app) as c:
        _login(c, auth)
        h1 = _save(c, "undo", {"n1": {"text": "v1"}}).json()["head"]
        h2 = _save(c, "undo", {"n1": {"text": "v2"}}, base=h1).json()["head"]   # tab A shows h2
        h3 = _save(c, "undo", {"n1": {"text": "v2"}, "n2": {"text": "from tab B"}},
                   base=h2).json()["head"]                                    # tab B saves h3
        stale = c.post("/api/mindmap/boards/undo/restore", headers=OWN,
                       json={"to": h1, "base": h2})
        assert stale.status_code == 409 and stale.json()["head"] == h3
        nobase = c.post("/api/mindmap/boards/undo/restore", headers=OWN, json={"to": h1})
        assert nobase.status_code == 409
        board = c.get("/api/mindmap/boards/undo", headers=OWN).json()
        assert board["nodes"]["n2"]["text"] == "from tab B"
        fresh = c.post("/api/mindmap/boards/undo/restore", headers=OWN,
                       json={"to": h1, "base": h3})
        assert fresh.status_code == 200 and fresh.json()["nodes"] == {"n1": {"text": "v1"}}


def test_the_page_sends_its_version_with_a_restore():
    js = (Path(_load("omli.ui").__file__).parent / "static" / "mindmap" / "mindmap.js"
          ).read_text(encoding="utf-8")
    start = js.index("async function restoreCheckpoint(")
    body = js[start:js.index("\n}\n", start)]
    assert "JSON.stringify({to,base:boardHead||null})" in body


def test_a_checkpoint_does_not_make_the_same_tabs_next_save_or_restore_stale(app, env):
    """The page saves, records a checkpoint, then saves or restores with the version it holds.
    A checkpoint changes nothing on the board, so it must not count as a newer version."""
    home, auth = env
    with _client(app) as c:
        _login(c, auth)
        first = _save(c, "cp", {"n1": {"text": "one"}}).json()["head"]
        held = _save(c, "cp", {"n1": {"text": "two"}}, base=first).json()["head"]
        assert c.post("/api/mindmap/boards/cp/checkpoints", headers=OWN,
                      json={"label": "v2"}).status_code == 200
        assert c.get("/api/mindmap/boards/cp", headers=OWN).json()["head"] == held
        r = _save(c, "cp", {"n1": {"text": "three"}}, base=held)
        assert r.status_code == 200, r.text
        back = c.post("/api/mindmap/boards/cp/restore", headers=OWN,
                      json={"to": first, "base": r.json()["head"]})
        assert back.status_code == 200 and back.json()["nodes"] == {"n1": {"text": "one"}}
