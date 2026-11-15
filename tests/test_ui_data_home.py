"""Where the UI keeps its data when OMLI_HOME is not set.

* With OMLI_HOME unset, the ``omli.ui`` server and its commands use one fixed per-user folder,
  ``.omli/data`` in the user's home folder, and never the folder they were started in. A
  folder that already holds a key registry, a project list, a model registry and boards is
  ignored completely: none of them is loaded.
* With OMLI_HOME set, the UI uses it, as before.
* The core library's own rule is unchanged: ``omli.home.omli_home()`` is OMLI_HOME when set,
  else the current folder.
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
ROLE = "mindmap-assistant"


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


def _registry(endpoint="http://localhost:9"):
    return json.dumps({
        "metadata": {"registry_id": "planted", "registry_version": "1.0.0",
                     "authored_at": "2026-11-12T00:00:00Z", "authored_by": "test",
                     "status": "staging"},
        "models": [{
            "model_id": ROLE, "display_name": "Planted model", "family": "fake",
            "provider_kind": "local-ollama", "adapter": "fake", "endpoint": endpoint,
            "pin": {"ref": "sha256:" + "0" * 64, "quant": "Q4_K_M"}, "reproducible": True,
            "status": "staging", "serving_tag": "fake:1b",
            "capabilities": {"context_window": 8192, "max_output_tokens": 1024,
                             "native_tool_calling": False, "parallel_tool_calls": False,
                             "structured_output": "none", "thinking_mode": False,
                             "vision": False},
            "params_default": {"temperature": 0.0}}]})


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A folder someone else prepared: a key registry, a project list, a model registry and
    a board. The UI is then started from inside it with OMLI_HOME unset."""
    folder = tmp_path / "prepared folder"
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "planted test passphrase")
    auth = _load("omli.ui.auth")
    auth.create_login_key(tmp_path / "someone else" / "their.key", home=folder)
    (folder / "ui" / "projects.jsonl").write_text(json.dumps(
        {"type": "project.added", "path": str(tmp_path), "at": "2026-11-12T00:00:00Z",
         "by": "x"}) + "\n", encoding="utf-8")
    (folder / "models").mkdir()
    (folder / "models" / "registry.yaml").write_text(_registry(), encoding="utf-8")
    (folder / "boards").mkdir()
    (folder / "boards" / "planted.jsonl").write_text(json.dumps(
        {"v": 1, "kind": "node.add", "op": "p1", "at": "2026-11-12T00:00:00Z", "by": None,
         "node": "n1", "data": {"text": "planted"}}) + "\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    monkeypatch.delenv("OMLI_HOME", raising=False)
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "my keys" / "login.key"))
    monkeypatch.chdir(folder)
    return folder, before


def test_the_ui_data_folder_is_fixed_per_user(monkeypatch):
    monkeypatch.delenv("OMLI_HOME", raising=False)
    home = _load("omli.ui.home")
    assert home.ui_home() == (Path.home() / ".omli" / "data").resolve()


def test_with_omli_home_unset_nothing_in_the_start_folder_is_loaded(planted):
    folder, before = planted
    app = _load("omli.ui.server").create_app(port=PORT)
    with TestClient(app, base_url=BASE) as c:
        # The planted key registry is not loaded: no key is registered, so no sign-in code.
        assert c.post("/api/auth/challenge", headers=OWN).status_code == 409
    data = Path.home() / ".omli" / "data"
    assert (data / "boards" / "how-omli-works.jsonl").is_file()
    assert not (folder / "boards" / "how-omli-works.jsonl").exists()
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


def test_with_omli_home_unset_the_planted_registry_projects_and_boards_are_ignored(planted):
    folder, _ = planted
    auth = _load("omli.ui.auth")
    auth.create_login_key()  # my own key, registered in the per-user folder
    app = _load("omli.ui.server").create_app(port=PORT)
    with TestClient(app, base_url=BASE) as c:
        ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
        assert c.post("/api/auth/login", headers=OWN,
                      json={"challenge": ch, "token": auth.sign_challenge(ch)}).status_code == 200
        assert c.get("/api/mindmap/assist/status").json()["configured"] is False
        assert c.get("/api/projects").json()["projects"] == []
        boards = [b["board"] for b in c.get("/api/mindmap/list").json()["boards"]]
        assert boards == ["how-omli-works"]
    assert not (folder / "ui" / "keys.jsonl").read_text(encoding="utf-8").count("\n") > 1


def test_init_and_serve_with_omli_home_unset_use_the_per_user_folder(planted, monkeypatch,
                                                                   capsys):
    folder, before = planted
    cli, server = _load("omli.ui.__main__"), _load("omli.ui.server")
    assert cli.main(["init"]) == 0
    data = (Path.home() / ".omli" / "data").resolve()
    assert (data / "ui" / "keys.jsonl").is_file()
    seen = {}
    monkeypatch.setattr(server, "serve", lambda port, home=None: seen.update(home=home))
    capsys.readouterr()
    assert cli.main(["serve", "--port", "8799"]) == 0
    assert str(data) in capsys.readouterr().out
    assert Path(seen["home"]).resolve() == data
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


def test_with_omli_home_set_the_ui_uses_it(tmp_path, monkeypatch):
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "chosen"))
    assert _load("omli.ui.home").ui_home() == (tmp_path / "chosen").resolve()


def test_the_core_rule_for_omli_home_is_unchanged(tmp_path, monkeypatch):
    core = _load("omli.home")
    monkeypatch.delenv("OMLI_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert core.omli_home() == tmp_path.resolve()
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "set"))
    assert core.omli_home() == (tmp_path / "set").resolve()


def test_each_ui_store_alone_defaults_to_the_per_user_folder(planted):
    """Not only create_app: the key registry and the project list, made with no folder,
    also use the per-user folder and never the one they were started in."""
    folder, before = planted
    data = (Path.home() / ".omli" / "data").resolve()
    assert _load("omli.ui.auth").registry_path() == data / "ui" / "keys.jsonl"
    assert _load("omli.ui.projects").ProjectStore().list() == []
    assert _load("omli.ui.projects").ProjectStore().path.parent.parent == data
