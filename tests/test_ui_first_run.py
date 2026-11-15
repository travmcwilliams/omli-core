"""First run: the start command, the setup page, and where a first run writes.

* ``omli-ui`` is an installed command (a console script) for ``python -m omli.ui``.
* ``omli-ui serve`` prints the address it serves, the Mind-map page and the data folder.
* With no login key, every page leads to the setup page, which names the init command.
* Started from an empty folder with no OMLI_HOME set, a first run writes nothing there: it
  writes only under the per-user data folder (``.omli/data`` in the home folder) and the key.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
from starlette.testclient import TestClient  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Origin": BASE}
ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


def test_the_start_command_is_an_installed_script():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^\[project\.scripts\]\s*\nomli-ui = "omli\.ui\.__main__:main"\s*$',
                     text, re.M), "pyproject.toml has no omli-ui console script"


def test_serve_prints_the_address_the_page_and_the_data_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "home"))
    cli = _load("omli.ui.__main__")
    server = _load("omli.ui.server")
    seen = {}
    monkeypatch.setattr(server, "serve", lambda port, home=None: seen.setdefault("port", port))
    assert cli.main(["serve", "--port", "8799"]) == 0
    out = capsys.readouterr().out
    assert seen == {"port": 8799}
    assert "http://127.0.0.1:8799/" in out
    assert "http://127.0.0.1:8799/mindmap" in out
    assert str((tmp_path / "home").resolve()) in out


def test_with_no_login_key_every_page_leads_to_the_setup_page(tmp_path, monkeypatch):
    monkeypatch.setenv("OMLI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "keys" / "login.key"))
    app = _load("omli.ui.server").create_app(port=PORT)
    with TestClient(app, base_url=BASE) as c:
        for path in ("/", "/mindmap"):
            r = c.get(path)
            assert r.status_code == 200 and r.url.path == "/login", path
            assert "Set up your login key" in r.text
            assert "omli-ui init" in r.text


def test_a_first_run_from_an_empty_folder_writes_only_in_the_data_and_key_folders(
        tmp_path, monkeypatch):
    work = tmp_path / "empty folder"
    keys = tmp_path / "user keys"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("OMLI_HOME", raising=False)
    monkeypatch.setenv("OMLI_UI_KEY", str(keys / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "first run passphrase")
    monkeypatch.delenv("OMLI_SEAL_KEY", raising=False)
    before = {p for p in tmp_path.rglob("*")}
    auth = _load("omli.ui.auth")
    auth.create_login_key()
    app = _load("omli.ui.server").create_app(port=PORT)
    with TestClient(app, base_url=BASE) as c:
        ch = c.post("/api/auth/challenge", headers=OWN).json()["challenge"]
        assert c.post("/api/auth/login", headers=OWN,
                      json={"challenge": ch, "token": auth.sign_challenge(ch)}).status_code == 200
        assert c.get("/mindmap").status_code == 200
        assert c.get("/api/mindmap/boards/how-omli-works").status_code == 200
        assert c.get("/api/mindmap/assist/status").json()["configured"] is False
    assert list(work.rglob("*")) == [], "a first run wrote into the folder it started in"
    data = Path.home() / ".omli" / "data"
    assert (data / "ui" / "keys.jsonl").is_file()
    assert (data / "boards" / "how-omli-works.jsonl").is_file()
    made = [p for p in tmp_path.rglob("*") if p not in before and p.is_file()]
    assert all(keys in p.parents for p in made), "a write landed outside the key folder"


def test_without_the_ui_extra_the_command_names_it(monkeypatch, capsys):
    import sys
    import omli.ui
    cli = _load("omli.ui.__main__")
    real = _load("omli.ui.server")
    monkeypatch.setattr(real, "serve", lambda *a, **k: pytest.fail("a real server started"))
    monkeypatch.delattr(omli.ui, "server", raising=False)
    monkeypatch.setitem(sys.modules, "omli.ui.server", None)  # as if fastapi were missing
    assert cli.main(["serve"]) == 1
    err = capsys.readouterr().err
    assert 'pip install "omli[ui]"' in err
