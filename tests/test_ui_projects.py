"""The user's project list: add a folder, scan a folder for git repositories, remove an entry.

Stored under OMLI_HOME as append-only events; the list is derived from them. Every path is
resolved on the server and keeps its case. Nothing assumes a drive letter.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _load(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{name} does not exist yet ({exc})")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "Omli Home"
    monkeypatch.setenv("OMLI_HOME", str(h))
    return h.resolve()


@pytest.fixture
def store(home):
    return _load("omli.ui.projects").ProjectStore()


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "Code Root"
    for repo in ("Repo One", "sub Dir/Repo Two", "Deep/A/B/Repo Three"):
        (root / repo / ".git").mkdir(parents=True)
    (root / "WorkTree Copy").mkdir()
    (root / "WorkTree Copy" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (root / "Not A Repo" / "src").mkdir(parents=True)
    (root / ".hidden" / "Secret Repo" / ".git").mkdir(parents=True)
    (root / "Repo One" / "nested" / ".git").mkdir(parents=True)  # inside a repo: not scanned
    return root.resolve()


def _events(home):
    return (home / "ui" / "projects.jsonl").read_text(encoding="utf-8").splitlines()


def test_add_keeps_spaces_and_case(store, tree, home):
    folder = tree / "Repo One"
    entry = store.add(str(folder))
    assert entry == str(folder)
    assert "Code Root" in entry and "Repo One" in entry
    assert store.list() == [str(folder)]
    assert len(_events(home)) == 1


def test_the_list_lives_under_omli_home_as_append_only_events(store, tree, home):
    a, b = tree / "Repo One", tree / "sub Dir" / "Repo Two"
    store.add(str(a))
    first = _events(home)[0]
    store.add(str(b))
    store.remove(str(a))
    lines = _events(home)
    assert lines[0] == first, "an earlier event was rewritten"
    assert [json.loads(x)["type"] for x in lines] == ["project.added", "project.added",
                                                      "project.removed"]
    assert store.list() == [str(b)]
    # A fresh reader derives the same list from the events.
    assert _load("omli.ui.projects").ProjectStore().list() == [str(b)]


def test_adding_twice_lists_once_and_writes_once(store, tree, home):
    folder = str(tree / "Repo One")
    store.add(folder)
    store.add(folder)
    assert store.list() == [folder]
    assert len(_events(home)) == 1


def test_a_path_is_resolved_on_the_server(store, tree):
    messy = str(tree / "sub Dir" / ".." / "Repo One" / ".")
    assert store.add(messy) == str(tree / "Repo One")


def test_a_case_variant_on_a_case_insensitive_disk_is_the_same_entry(store, tree):
    variant = Path(str(tree).swapcase()) / "repo one"
    if not variant.exists():
        pytest.skip("this disk is case-sensitive")
    store.add(str(tree / "Repo One"))
    assert store.add(str(variant)) == str(tree / "Repo One")
    assert store.list() == [str(tree / "Repo One")]


def test_two_spellings_of_one_folder_are_one_entry_by_the_file_system(store, tree, home,
                                                                     monkeypatch):
    # macOS: Path.resolve() leaves the typed case alone. Identity must come from the file
    # system (the same folder), not from comparing two strings.
    variant = Path(str(tree).swapcase()) / "repo one"
    if not variant.exists():
        pytest.skip("this disk is case-sensitive")
    mod = _load("omli.ui.projects")
    monkeypatch.setattr(mod, "resolve_folder", lambda raw: Path(raw))
    store.add(str(tree / "Repo One"))
    assert store.add(str(variant)) == str(tree / "Repo One")
    assert store.list() == [str(tree / "Repo One")]
    assert len(_events(home)) == 1
    assert store.remove(str(variant)) == str(tree / "Repo One")
    assert store.list() == []


def test_on_a_case_sensitive_disk_two_names_stay_two_entries(store, tmp_path):
    upper, lower = tmp_path / "Pair" / "Repo", tmp_path / "Pair" / "repo"
    upper.mkdir(parents=True)
    try:
        lower.mkdir()
    except FileExistsError:
        pytest.skip("this disk is case-insensitive")
    store.add(str(upper))
    store.add(str(lower))
    assert sorted(store.list()) == sorted([str(upper.resolve()), str(lower.resolve())])


# --- the real-case lookup, with a faked directory listing: runs on every platform ----------

def _real_case():
    fn = getattr(_load("omli.ui.projects"), "real_case", None)
    if fn is None:
        pytest.fail("omli.ui.projects.real_case does not exist yet")
    return fn


def _fake_disk(listing, insensitive):
    """``listing`` maps the names below the root to the names a folder lists."""
    def listdir(p):
        parts = Path(p).parts[1:]
        if parts not in listing:
            raise FileNotFoundError(str(p))
        return list(listing[parts])

    def same(a, b):
        x, y = Path(a).parts[1:], Path(b).parts[1:]
        if insensitive:
            return [s.casefold() for s in x] == [s.casefold() for s in y]
        return x == y

    return listdir, same


def test_real_case_takes_each_name_from_the_directory_listing():
    listdir, same = _fake_disk({(): ["Code Root", "other"],
                                ("Code Root",): ["Repo One", "notes"]}, insensitive=True)
    got = _real_case()(Path("/").resolve() / "CODE ROOT" / "repo one", listdir=listdir, same=same)
    assert got.parts[1:] == ("Code Root", "Repo One")


def test_real_case_keeps_an_exact_name_when_two_differ_only_in_case():
    listdir, same = _fake_disk({(): ["Root"], ("Root",): ["Repo", "repo"]}, insensitive=False)
    root = Path("/").resolve()
    assert _real_case()(root / "Root" / "repo", listdir=listdir, same=same).parts[1:] == ("Root", "repo")
    assert _real_case()(root / "Root" / "Repo", listdir=listdir, same=same).parts[1:] == ("Root", "Repo")


def test_real_case_never_takes_the_name_of_another_folder():
    # A name that differs only in case but is a different folder is not this folder's name.
    listdir, same = _fake_disk({(): ["Root"], ("Root",): ["Repo", "repo"]}, insensitive=False)
    got = _real_case()(Path("/").resolve() / "Root" / "REPO", listdir=listdir, same=same)
    assert got.parts[1:] == ("Root", "REPO")


def test_real_case_keeps_the_rest_when_a_folder_cannot_be_listed():
    listdir, same = _fake_disk({(): ["Root"]}, insensitive=True)
    got = _real_case()(Path("/").resolve() / "ROOT" / "Sub" / "leaf", listdir=listdir, same=same)
    assert got.parts[1:] == ("Root", "Sub", "leaf")


@pytest.mark.parametrize("bad,why", [("", "required"), ("relative/Repo One", "absolute"),
                                     ("Repo One", "absolute")])
def test_a_relative_or_empty_path_is_refused(store, bad, why, tree, monkeypatch):
    monkeypatch.chdir(tree)
    with pytest.raises(ValueError, match=why):
        store.add(bad)
    assert store.list() == []


def test_a_missing_folder_or_a_file_is_refused(store, tree):
    with pytest.raises(ValueError):
        store.add(str(tree / "No Such Folder"))
    (tree / "a file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        store.add(str(tree / "a file.txt"))
    assert store.list() == []


def test_removing_an_unlisted_path_is_refused(store, tree, home):
    with pytest.raises(KeyError):
        store.remove(str(tree / "Repo One"))
    assert not (home / "ui" / "projects.jsonl").exists()


def test_scan_finds_git_repositories_and_skips_hidden_and_nested(store, tree):
    added = store.scan(str(tree))
    expected = sorted(str(tree / p) for p in ("Repo One", "sub Dir/Repo Two",
                                              "Deep/A/B/Repo Three", "WorkTree Copy"))
    assert sorted(added) == expected
    assert sorted(store.list()) == expected
    assert store.scan(str(tree)) == [], "a second scan must add nothing"


def test_scan_of_a_missing_folder_is_refused(store, tree):
    with pytest.raises(ValueError):
        store.scan(str(tree / "Nope"))


def test_a_corrupt_event_file_fails_closed(store, tree, home):
    store.add(str(tree / "Repo One"))
    with open(home / "ui" / "projects.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    with pytest.raises(ValueError):
        _load("omli.ui.projects").ProjectStore().list()


# --- over HTTP ----------------------------------------------------------------------------

def test_the_project_api(home, tree, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("cryptography")
    from starlette.testclient import TestClient

    monkeypatch.setenv("OMLI_UI_KEY", str(tmp_path / "User Dir" / "login.key"))
    monkeypatch.setenv("OMLI_UI_KEY_PASSPHRASE", "projects test passphrase")
    auth = _load("omli.ui.auth")
    auth.create_login_key()
    base = "http://127.0.0.1:8765"
    own = {"Origin": base}
    with TestClient(_load("omli.ui.server").create_app(port=8765), base_url=base) as c:
        ch = c.post("/api/auth/challenge", headers=own).json()["challenge"]
        assert c.post("/api/auth/login", headers=own,
                      json={"challenge": ch, "token": auth.sign_challenge(ch)}).status_code == 200
        one = str(tree / "Repo One")
        r = c.post("/api/projects/add", headers=own, json={"path": one})
        assert r.status_code == 200 and r.json()["path"] == one
        r = c.post("/api/projects/scan", headers=own, json={"path": str(tree)})
        assert r.status_code == 200 and len(r.json()["added"]) == 3
        assert len(c.get("/api/projects", headers=own).json()["projects"]) == 4
        assert c.post("/api/projects/remove", headers=own, json={"path": one}).status_code == 200
        assert one not in c.get("/api/projects", headers=own).json()["projects"]
        assert c.post("/api/projects/remove", headers=own, json={"path": one}).status_code == 404
        assert c.post("/api/projects/add", headers=own,
                      json={"path": "relative"}).status_code == 400
        assert c.post("/api/projects/add", headers={"Origin": "http://x.example"},
                      json={"path": one}).status_code == 403
