"""The Mind-map page: served from package data behind the guard, wired to the routes that
exist, opening the starter board, and honest about a missing model.

* ``/mindmap`` and the files it loads are served only to a signed-in session, only from an
  exact list of shipped files, with the server's Content-Security-Policy.
* Every route the page's scripts call is a route the app registers, so the page cannot reach
  a screen or route this release does not ship.
* Every element id the page script looks up is in the page (or made by the script).
* ``/api/mindmap/assist/status`` says whether a model is configured, and how to add one.
* A fresh OMLI_HOME has the starter board, and the page opens it.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
from starlette.testclient import TestClient  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}
STATIC = "omli.ui.static.mindmap"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "Omli Home"
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "User Dir" / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "mindmap page passphrase")
    monkeypatch.delenv("OMLI_SEAL_KEY", raising=False)
    auth = _load("omli.ui.auth")
    auth.create_login_key()
    return home.resolve(), auth


@pytest.fixture
def app(env):
    return _load("omli.ui.server").create_app(port=PORT)


def _client(app):
    return TestClient(app, base_url=BASE)


def _login(c, auth):
    ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
    r = c.post("/api/auth/login", headers=OWN,
               json={"challenge": ch, "token": auth.sign_challenge(ch)})
    assert r.status_code == 200, r.text


def _shipped():
    """Every file under the page's package folder, as a path relative to it."""
    root = resources.files(STATIC)
    out = []

    def walk(node, prefix):
        for child in node.iterdir():
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                if child.name != "__pycache__":
                    walk(child, rel + "/")
            elif not child.name.endswith((".py", ".pyc")):
                out.append(rel)
    walk(root, "")
    return sorted(out)


def _text(rel):
    return resources.files(STATIC).joinpath(rel).read_text(encoding="utf-8")


def _registered(app):
    out = set()
    for route in app.routes:
        for m in getattr(route, "methods", None) or ():
            if m != "HEAD":
                out.add((m, route.path))
    return out


def _matches(path, template):
    rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
    return re.match(rx, path) is not None


# --- serving ----------------------------------------------------------------------------

def test_the_page_and_its_files_need_a_session(app):
    with _client(app) as c:
        page = c.get("/mindmap", follow_redirects=False)
        script = c.get("/mindmap/mindmap.js", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert script.status_code == 303


def test_the_page_is_served_with_the_strict_policy(app, env):
    _, auth = env
    with _client(app) as c:
        _login(c, auth)
        r = c.get("/mindmap")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == CSP
    assert '<script type="module" src="/mindmap/mindmap.js"></script>' in r.text
    assert "<script>" not in r.text and "<script type=\"module\">" not in r.text


@pytest.mark.parametrize("rel", _shipped())
def test_every_shipped_file_is_served_with_its_type(app, env, rel):
    _, auth = env
    want = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
            ".html": "text/html", "": "text/plain"}
    suffix = "." + rel.rsplit(".", 1)[1] if "." in rel.rsplit("/", 1)[-1] else ""
    if rel.endswith(".LICENSE"):
        suffix = ""
    with _client(app) as c:
        _login(c, auth)
        r = c.get(f"/mindmap/{rel}")
    assert r.status_code == 200, rel
    assert r.headers["content-type"].startswith(want[suffix]), (rel, r.headers["content-type"])
    assert r.headers["content-security-policy"] == CSP


@pytest.mark.parametrize("path", [
    "/mindmap/nope.js", "/mindmap/../server.py", "/mindmap/..%2fserver.py",
    "/mindmap/js/../../server.py", "/mindmap/__init__.py", "/mindmap/js",
    "/mindmap/%2e%2e/auth.py",
])
def test_only_listed_files_are_served(app, env, path):
    _, auth = env
    with _client(app) as c:
        _login(c, auth)
        r = c.get(path)
    assert r.status_code == 404, (path, r.status_code)


# --- the page reaches only what this release ships -------------------------------------

def _page_sources():
    return {rel: _text(rel) for rel in _shipped() if rel.endswith((".js", ".html", ".css"))
            and not rel.startswith("js/vendor/")}


def test_every_api_path_the_page_calls_is_a_registered_route(app):
    routes = [p for _, p in _registered(app)]
    called = set()
    for rel, src in _page_sources().items():
        for m in re.finditer(r"""['"`](/api/[A-Za-z0-9_/.-]*)""", src):
            called.add((rel, m.group(1)))
    assert called, "the page calls no route"
    missing = []
    for rel, path in sorted(called):
        prefix_ok = path.endswith("/") and any(r.startswith(path) for r in routes)
        if not prefix_ok and not any(_matches(path, r) for r in routes):
            missing.append((rel, path))
    assert missing == []


def test_every_page_link_and_import_is_a_shipped_file_or_page(app):
    pages = {"/", "/login", "/mindmap"}
    shipped = {f"/mindmap/{rel}" for rel in _shipped()}
    bad = []
    for rel, src in _page_sources().items():
        for m in re.finditer(r"""(?:from\s+|import\(|src=|href=)['"](/[^'"?#]*)""", src):
            target = m.group(1)
            if target not in pages and target not in shipped:
                bad.append((rel, target))
        for m in re.finditer(r"""location\.href\s*=\s*['"](/[^'"]*)""", src):
            if m.group(1) not in pages:
                bad.append((rel, m.group(1)))
    assert bad == []


def test_every_element_the_script_looks_up_is_on_the_page():
    js, html = _text("mindmap.js"), _text("index.html")
    used = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", js))
    made = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html + js))
    guarded = {"inkdock"}  # looked up with a null check: the ink dock is not in this release
    assert used - made - guarded == set()


def test_every_named_icon_in_the_pack_is_shipped():
    vocab = _load("omli.boards.vocab")
    names = {r["icon"] for r in vocab.section("icons")["rules"]}
    shipped = {rel[len("icons/"):-len(".svg")] for rel in _shipped()
               if rel.startswith("icons/") and rel.endswith(".svg")}
    assert names <= shipped, sorted(names - shipped)


def test_vendored_files_carry_their_licence():
    files = set(_shipped())
    assert {"js/vendor/rough.esm.js", "js/vendor/rough.LICENSE", "icons/LICENSE"} <= files
    assert "Preet Shihn" in _text("js/vendor/rough.LICENSE")
    assert "MIT License" in _text("icons/LICENSE")


# --- first run: model status and the starter board -------------------------------------

def test_with_no_model_the_status_says_how_to_add_one(app, env):
    _, auth = env
    with _client(app) as c:
        _login(c, auth)
        r = c.get("/api/mindmap/assist/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert "models/registry.yaml" in body["reason"] and "mindmap-assistant" in body["reason"]


def test_with_a_model_for_the_role_the_status_is_configured(app, env, monkeypatch):
    home, auth = env
    assist = _load("omli.boards.assist")
    monkeypatch.setattr(assist, "default_provider_table", lambda: {"fake": object()})
    from test_ui_mindmap import _registry_yaml
    (home / "models").mkdir(parents=True, exist_ok=True)
    (home / "models" / "registry.yaml").write_text(_registry_yaml("local-ollama"),
                                                   encoding="utf-8")
    with _client(app) as c:
        _login(c, auth)
        body = c.get("/api/mindmap/assist/status").json()
    assert body == {"configured": True, "reason": ""}


def test_the_status_check_calls_no_model_and_writes_nothing(app, env, monkeypatch):
    home, auth = env
    assist = _load("omli.boards.assist")

    def boom(*a, **k):
        raise AssertionError("the status check ran a model call")
    monkeypatch.setattr(assist.Assistant, "run", boom)
    with _client(app) as c:
        _login(c, auth)
        before = sorted(p.relative_to(home) for p in home.rglob("*"))
        c.get("/api/mindmap/assist/status")
        after = sorted(p.relative_to(home) for p in home.rglob("*"))
    assert before == after


def test_a_fresh_home_has_the_starter_board(app, env):
    _, auth = env
    with _client(app) as c:
        _login(c, auth)
        r = c.get("/api/mindmap/boards/how-omli-works")
    assert r.status_code == 200, r.text
    assert r.json()["nodes"]


def test_the_page_opens_the_starter_board_and_gates_the_model_actions():
    js = _text("mindmap.js")
    assert "const STARTER_BOARD='how-omli-works';" in js
    assert "await openSavedMap(STARTER_BOARD);" in js
    assert "'/api/mindmap/assist/status'" in js
    # the model actions start off and are switched on only by a configured status
    assert "let modelStatus={configured:false" in js
    assert "disabled title=\"No model configured\"" in js


def test_the_package_lists_the_page_files_as_package_data():
    data =resources.files("omli").joinpath("..", "pyproject.toml")
    text = data.read_text(encoding="utf-8") if data.is_file() else ""
    if not text:
        pytest.skip("pyproject.toml is not beside the package (installed from a wheel)")
    assert '"omli.ui.static.mindmap"' in text


def test_board_json_round_trip_names_no_field_the_core_does_not_know():
    # the page's adapter puts ladder bindings where the core's ladder check reads them
    js = _text("mindmap.js")
    assert "if(n.meta.catalog_ref) c.catalog_ref=n.meta.catalog_ref;" in js
    assert "RUNG_TO_SERVER={'context-group':'context'}" in js
    ladder = _load("omli.boards.ladder")
    assert "context" in ladder.RUNG_KINDS and "context-group" not in ladder.RUNG_KINDS
    json.dumps(ladder.RUNG_KINDS)


# --- saving: the name field picks the board, and a save never lands on another board -----

def _function(js, name):
    start = js.index(f"async function {name}(")
    return js[start:js.index("\n}\n", start)]


def test_a_save_goes_to_the_board_named_in_the_name_field():
    save = _function(_text("mindmap.js"), "saveBoard")
    assert "boardIdFrom($('mapName').value)" in save
    assert "wanted!==mapId" in save, "a changed name must save to that board, not the open one"
    assert "boardHead=null" in save, "a new board starts with no base"


def test_every_save_says_which_version_it_was_based_on():
    save = _function(_text("mindmap.js"), "saveBoard")
    assert "base:boardHead||null" in save
    assert "if(boardHead) body.base" not in save


def test_a_save_based_on_nothing_is_refused_for_an_existing_board(app, env):
    home, auth = env
    with _client(app) as c:
        _login(c, auth)
        r = c.post("/api/mindmap/save", headers=OWN,
                   json={"board": "how-omli-works", "base": None, "nodes": {}, "links": {}})
        assert r.status_code == 409
        fresh = c.post("/api/mindmap/save", headers=OWN,
                       json={"board": "a-new-board", "base": None,
                             "nodes": {"n1": {"text": "first", "x": 0, "y": 0}}, "links": {}})
        assert fresh.status_code == 200 and fresh.json()["written"] == 1


# --- board data from the server is cleaned before any renderer sees it --------------------

HOSTILE = {
    "nodes": {
        "ok": {"text": "<b>kept as text</b>", "x": 1, "y": 2, "order": 3, "glyph": "A1",
               "icon": {"kind": "library", "src": "/mindmap/icons/bulb.svg", "hue": 10,
                        "glyph": "Z"},
               "meta": {"photo": "data:image/png;base64,iVBORw0KGgo=", "fillColor": "#ff0000",
                        "params": {"k": 5}, "shapeStyle": "neon"}},
        "evil": {"x": "1px\" onmouseover=\"x", "order": "<meta http-equiv=refresh>",
                 "glyph": "<meta http-equiv=\"refresh\" content=\"0;url=https://other.example/\">",
                 "icon": {"kind": "library", "src": "x') ; background:url('https://other.example/a",
                          "hue": "1);x:url(https://other.example/b", "glyph": "<img src=x>"},
                 "meta": {"photo": "data:image/png;base64,AA\"><meta http-equiv=refresh>",
                          "src": "data:image/svg+xml,<svg onload=alert(1)>",
                          "fillColor": "red\"/><foreignObject>", "fill": "url(https://other.example/c)",
                          "fillOpacity": "1\" onload=\"x", "pattern": "dots\"><x",
                          "shapeStyle": {"stroke": "red\"><x", "fill": "blue", "dash": "1 2\"><x"},
                          "params": {"k": "5\"><meta>"}}},
        "bad\"id": {"text": "dropped"},
        "child": {"parent": "x\"><y", "shape": "rect\"><x"},
    },
    "links": {
        "l1": {"from": "ok", "to": "evil", "color": "red\"><x", "width": "2\"><x"},
        "l2": {"from": "ok", "to": "bad\"id"},
        "bad\"link": {"from": "ok", "to": "evil"},
    },
}


def _node_clean(board):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    src = _text("js/mm-clean.js")
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "mm-clean.mjs"
        mod.write_text(src, encoding="utf-8")
        run = Path(tmp) / "run.mjs"
        run.write_text("import {cleanBoard} from './mm-clean.mjs';\n"
                       "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>"
                       "process.stdout.write(JSON.stringify(cleanBoard(JSON.parse(s)))));\n",
                       encoding="utf-8")
        out = subprocess.run([node, str(run)], input=json.dumps(board), capture_output=True,
                             text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def test_the_cleaner_keeps_safe_board_data():
    b = _node_clean(HOSTILE)
    ok = b["nodes"]["ok"]
    assert ok["text"] == "<b>kept as text</b>"  # text is escaped by the renderers, not here
    assert (ok["x"], ok["y"], ok["order"], ok["glyph"]) == (1, 2, 3, "A1")
    assert ok["icon"] == {"kind": "library", "src": "/mindmap/icons/bulb.svg", "hue": 10,
                          "glyph": "Z"}
    assert ok["meta"]["photo"].startswith("data:image/png;base64,")
    assert ok["meta"]["fillColor"] == "#ff0000" and ok["meta"]["params"] == {"k": 5}


def test_the_cleaner_reduces_every_markup_carrying_field():
    b = _node_clean(HOSTILE)
    assert set(b["nodes"]) == {"ok", "evil", "child"}, "a node with an unsafe id is dropped"
    assert set(b["links"]) == {"l1"}, "links with unsafe ids or ends are dropped"
    evil, child = b["nodes"]["evil"], b["nodes"]["child"]
    assert evil["x"] == 0 and "order" not in evil and "glyph" not in evil
    assert evil["icon"] == {}, "no icon source, colour or glyph survives"
    m = evil["meta"]
    assert m["photo"] == "" and m["src"] == ""
    for k in ("fillColor", "fill", "fillOpacity", "pattern"):
        assert k not in m, k
    assert m["shapeStyle"] == {"fill": "blue"} and m["params"] == {}
    assert "parent" not in child and "shape" not in child
    assert "color" not in b["links"]["l1"] and "width" not in b["links"]["l1"]
    rendered = json.dumps(b)
    for marker in ('"><', "other.example", "onmouseover", "onload"):
        assert marker not in rendered.replace("<b>kept as text</b>", ""), marker


def test_every_board_from_the_server_passes_through_the_cleaner():
    js = _text("mindmap.js")
    assert "import {cleanBoard,cleanImage} from '/mindmap/js/mm-clean.js';" in js
    start = js.index("function fromServer(b){")
    assert js[start:start + 80].count("b=cleanBoard(b);") == 1


def test_a_board_image_must_be_a_strict_data_image():
    js = _text("mindmap.js")
    start = js.index("function localImage(raw){")
    body = js[start:js.index("\n}\n", start)]
    assert "const s=cleanImage(raw);" in body and "String(raw||'')" not in body


def test_the_cleaner_drops_names_that_reach_object_prototypes():
    b = _node_clean({
        "nodes": {"a": {"shape": "constructor", "icon": {"kind": "toString"},
                        "meta": {"fillMode": "pattern", "pattern": "constructor",
                                 "shapeStyle": "hasOwnProperty"}}},
        "links": {"l1": {"from": "a", "to": "constructor"},
                  "l2": {"from": "a", "to": "a", "style": "valueOf"}},
    })
    a = b["nodes"]["a"]
    assert "shape" not in a and a["icon"] == {}
    assert "pattern" not in a["meta"] and "shapeStyle" not in a["meta"]
    assert set(b["links"]) == {"l2"} and "style" not in b["links"]["l2"]


def test_the_cleaner_passes_on_only_nodes_and_links():
    b = _node_clean({"nodes": {}, "links": {}, "background": {"image": "x\"><y"},
                     "attribution": "<b>"})
    assert set(b) == {"nodes", "links"}


def test_the_attribution_count_is_escaped():
    js = _text("mindmap.js")
    assert "'<i>'+escapeHtml(String(a.edit_count))+'</i></div>'" in js
    assert "'<i>'+a.edit_count+'</i>'" not in js
