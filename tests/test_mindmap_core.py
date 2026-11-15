"""The Mind-map's core pieces, with no web stack: board diff, ladder check, icons, checkpoints,
the offline auto-design stub, file extraction and retrieval safety."""

from __future__ import annotations

import importlib
import os
import re
import sys
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
    monkeypatch.delenv("OMLI_SEAL_KEY", raising=False)
    return h.resolve()


# --- save: a target board becomes the operations that reach it ---------------------------

def _board(nodes, links=None):
    return {"nodes": nodes, "links": links or {}}


def test_the_diff_reaches_the_target_board_by_replay(home):
    diff, boards = _load("omli.boards.diff"), _load("omli.boards")
    log = boards.BoardLog("diffed")
    first = _board({"a": {"text": "alpha", "x": 0, "y": 0, "color": "red"}, "b": {"text": "bravo"}},
                   {"l1": {"from": "a", "to": "b", "relation": "flow"}})
    log.append_many(diff.ops_for(log.replay(), first))
    assert log.replay() == first
    second = _board({"a": {"text": "alpha two", "x": 5, "y": 7}, "c": {"text": "charlie"}},
                    {"l2": {"from": "a", "to": "c"}})
    ops = diff.ops_for(log.replay(), second)
    assert {o["kind"] for o in ops} == {"node.edit", "node.move", "node.delete", "node.add",
                                        "link.add"}
    log.append_many(ops)
    assert log.replay() == second  # "color" was dropped by an edit to null


def test_an_unchanged_board_makes_no_operations(home):
    diff = _load("omli.boards.diff")
    state = _board({"a": {"text": "alpha"}})
    assert diff.ops_for(state, state) == []


@pytest.mark.parametrize("bad", [
    {"nodes": []}, {"nodes": {"": {}}}, {"nodes": {"a": "text"}},
    {"nodes": {"a": {}}, "links": {"l": {"from": "a"}}},
    {"nodes": {"a": {}}, "links": {"l": {"from": "a", "to": "missing"}}},
])
def test_a_malformed_target_board_is_refused(bad):
    diff = _load("omli.boards.diff")
    with pytest.raises(ValueError):
        diff.ops_for(_board({}), bad)


# --- rag/validate: a pure graph check --------------------------------------------------

def _ladder(cycle=False, bound=True):
    nodes = {
        "s": {"kind": "source", "catalog_ref": {"path": "/somewhere"} if bound else {}},
        "r": {"kind": "retriever", "params": {"k": 3}},
        "a": {"kind": "agent", "catalog_ref": {"model_id": "a-role"}},
        "o": {"kind": "output"},
        "idea": {"text": "not a rung"},
    }
    links = {"1": {"from": "s", "to": "r", "relation": "flow"},
             "2": {"from": "r", "to": "a", "relation": "flow"},
             "3": {"from": "a", "to": "o", "relation": "flow"}}
    if cycle:
        links["4"] = {"from": "o", "to": "s", "relation": "flow"}
    return {"nodes": nodes, "links": links}


def test_a_well_formed_ladder_validates_in_flow_order():
    ladder = _load("omli.boards.ladder")
    out = ladder.validate_ladder(_ladder())
    assert out["ok"] is True and out["errors"] == []
    assert out["order"] == ["s", "r", "a", "o"]


def test_a_cycle_and_an_unbound_source_are_errors():
    ladder = _load("omli.boards.ladder")
    assert "flow has a cycle" in ladder.validate_ladder(_ladder(cycle=True))["errors"]
    assert any("not bound" in e for e in ladder.validate_ladder(_ladder(bound=False))["errors"])
    assert ladder.validate_ladder(_board({}))["ok"] is False


# --- icon: rules from pack data, else a stable procedural seed ---------------------------

def test_icons_come_from_pack_rules_or_a_stable_seed():
    icon = _load("omli.boards.icon")
    assert icon.icon_for("a database of results")["name"] == "database"
    one, two = icon.icon_for("zqxj wvut"), icon.icon_for("zqxj wvut")
    assert one == two and one["kind"] == "seed"
    assert "database" not in Path(icon.__file__).read_text(encoding="utf-8")


# --- checkpoints: CanonicalInput -> Spec -> DecisionRecord, deduplicated ----------------

def test_a_checkpoint_is_a_three_artifact_chain_and_a_log_operation(home):
    boards, cp = _load("omli.boards"), _load("omli.boards.checkpoint")
    log = boards.BoardLog("chained")
    log.append("node.add", op="a", node="n1", data={"text": "alpha"})
    made = cp.create_checkpoint("chained", "first", by="alice")
    assert made["created"] is True
    store = _load("omli.artifacts").ArtifactStore.from_log(home / "var" / "artifacts.jsonl")
    kinds = [a.kind for a in store.all()]
    assert kinds == ["CanonicalInput", "Spec", "DecisionRecord"]
    ci, spec, dec = store.all()
    assert spec.provenance.predecessor_ids == (ci.id,)
    assert dec.provenance.predecessor_ids == (spec.id,)
    assert all(a.provenance.model_ref is None for a in store.all())
    assert log.ops()[-1]["kind"] == "checkpoint" and log.ops()[-1]["artifact"] == ci.id
    again = cp.create_checkpoint("chained", "same board", by="alice")
    assert again["created"] is False and again["checkpoint_id"] == ci.id
    assert len(store.from_log(home / "var" / "artifacts.jsonl").all()) == 3
    listed = cp.list_checkpoints("chained")
    assert [c["checkpoint_id"] for c in listed] == [ci.id]
    assert listed[0]["decision_id"] == dec.id and listed[0]["label"] == "first"


# --- autodesign: offline only ----------------------------------------------------------

def test_the_questions_and_build_are_offline_templates():
    auto = _load("omli.boards.autodesign")
    q = auto.next_question({})
    assert q["done"] is False and q["question"]["id"]
    answers = {}
    while not q["done"]:
        answers[q["question"]["id"]] = "one, two"
        q = auto.next_question(answers)
    board = auto.build_board(answers)
    assert len(board["nodes"]) >= 3
    assert all(ln["from"] in board["nodes"] and ln["to"] in board["nodes"]
               for ln in board["links"].values())
    assert auto.build_board(answers) == board


# --- dump extraction: deterministic, size-limited, an allow-list of types --------------

def test_text_types_are_extracted_and_capped():
    ex = _load("omli.boards.extract")
    out = ex.extract(("word " * 5000).encode(), "notes.md")
    assert out.method == "text" and out.truncated and len(out.text) <= ex.limits()["max_chars"]
    assert ex.extract(b"a,b\n1,2\n", "t.csv").text.startswith("a, b")


def test_a_type_outside_the_allow_list_is_refused():
    ex = _load("omli.boards.extract")
    with pytest.raises(ex.UnsupportedType):
        ex.extract(b"MZ\x90\x00", "tool.exe")
    with pytest.raises(ex.UnsupportedType):
        ex.extract(b"plain", "no-extension")


def test_a_file_over_the_size_limit_is_refused():
    ex = _load("omli.boards.extract")
    with pytest.raises(ex.TooLarge):
        ex.extract(b"x" * (ex.limits()["max_bytes"] + 1), "big.txt")


@pytest.mark.parametrize("name,module,lib", [("a.pdf", "pypdf", "pypdf"),
                                             ("a.docx", "docx", "python-docx"),
                                             ("a.xlsx", "openpyxl", "openpyxl")])
def test_a_missing_document_library_is_named(monkeypatch, name, module, lib):
    ex = _load("omli.boards.extract")
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(ex.MissingLibrary) as err:
        ex.extract(b"%PDF-1.4 not really", name)
    assert lib in str(err.value) and "omli[files]" in str(err.value)


# --- retrieval: only inside the user's project folders ----------------------------------

@pytest.fixture
def folders(tmp_path):
    inside = tmp_path / "Listed Project"
    (inside / "docs").mkdir(parents=True)
    (inside / "docs" / "plan.md").write_text("the launch plan mentions apples", encoding="utf-8")
    (inside / ".git").mkdir()
    (inside / ".git" / "config").write_text("apples in git config", encoding="utf-8")
    outside = tmp_path / "Not Listed"
    outside.mkdir()
    (outside / "secret.md").write_text("apples outside the list", encoding="utf-8")
    return inside.resolve(), outside.resolve()


def test_the_catalog_lists_only_the_project_folders(folders):
    rt = _load("omli.boards.retrieve")
    inside, outside = folders
    cat = rt.catalog([str(inside)])
    assert [s["path"] for s in cat["sources"]] == [str(inside)]


def test_retrieval_reads_inside_a_listed_folder(folders):
    rt = _load("omli.boards.retrieve")
    inside, _ = folders
    hits = rt.retrieve("apples", [rt.source_root(str(inside), [str(inside)])], k=5)
    assert [h["file"] for h in hits] == ["docs/plan.md"]


@pytest.mark.parametrize("how", ["traversal", "absolute", "relative"])
def test_a_source_outside_the_project_list_is_refused(folders, how):
    rt = _load("omli.boards.retrieve")
    inside, outside = folders
    raw = {"traversal": str(inside / ".." / outside.name), "absolute": str(outside),
           "relative": "Listed Project"}[how]
    with pytest.raises(rt.OutsideProjects):
        rt.source_root(raw, [str(inside)])


@pytest.mark.parametrize("target", ["file", "dir"])
def test_a_symlink_that_points_outside_is_never_read(folders, target):
    rt = _load("omli.boards.retrieve")
    inside, outside = folders
    link = inside / "docs" / ("escape.md" if target == "file" else "escape")
    try:
        os.symlink(outside / "secret.md" if target == "file" else outside, link,
                   target_is_directory=(target == "dir"))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system cannot make a symlink here ({type(exc).__name__})")
    hits = rt.retrieve("apples outside", [rt.source_root(str(inside), [str(inside)])], k=10)
    assert all("outside" not in h["snippet"] for h in hits)
    assert all("escape" not in h["file"] for h in hits)
    with pytest.raises(rt.OutsideProjects):
        rt.source_root(str(link), [str(inside)])


@pytest.mark.skipif(sys.platform != "win32", reason="a directory junction is a Windows link")
def test_a_junction_that_points_outside_is_never_entered(folders):
    rt = _load("omli.boards.retrieve")
    import _winapi
    inside, outside = folders
    link = inside / "docs" / "escape"
    _winapi.CreateJunction(str(outside), str(link))
    hits = rt.retrieve("apples outside", [rt.source_root(str(inside), [str(inside)])], k=10)
    assert [h["file"] for h in hits] == ["docs/plan.md"]
    with pytest.raises(rt.OutsideProjects):
        rt.source_root(str(link), [str(inside)])


def test_the_files_extra_holds_the_document_libraries_and_ci_installs_it():
    root = Path(__file__).resolve().parent.parent
    toml = (root / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^files\s*=\s*\[(.*?)\]", toml, re.M | re.S)
    assert block, "no files extra"
    names = {re.split(r"[<>=\[ ]", d, 1)[0].lower() for d in re.findall(r'"([^"]+)"', block.group(1))}
    assert names == {"pypdf", "python-docx", "openpyxl"}
    assert not re.search(r"^docs\s*=", toml, re.M), "the extra is named files, not docs"
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev,sign,ui,files]"' in ci


# --- retrieval never reads or sends: .git, hidden folders and files, secret-looking files,
#     files over the size limit, or the UI's own data folder ----------------------------

@pytest.fixture
def mixed_project(tmp_path):
    root = tmp_path / "Project With Everything"
    files = {
        "notes.md": "apples are allowed here",                       # the one file allowed
        ".git/notes.md": "apples in a git folder",
        ".config/settings.yaml": "apples in a hidden folder",
        ".hidden.md": "apples in a hidden file",
        ".env.json": "apples in an env file",
        "deploy/prod.env.yaml": "apples with an env name",
        "credentials.json": "apples in a credential store",
        "keys/client_secret.json": "apples in a secret file",
        "keys/id_rsa.txt": "apples in a private key",
        "certs/server.pem.txt": "apples in a certificate",
        "omli-data/models/registry.yaml": "apples in the UI data folder",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    big = root / "big.md"
    big.write_text("apples " * 100_000, encoding="utf-8")  # over max_file_bytes
    return root.resolve()


def test_retrieval_reads_only_the_allowed_file(mixed_project):
    rt = _load("omli.boards.retrieve")
    hits = rt.retrieve("apples", [mixed_project], k=50,
                       exclude=[mixed_project / "omli-data"])
    assert [h["file"] for h in hits] == ["notes.md"]


@pytest.mark.parametrize("name", [
    ".env", ".env.local", ".env.production", "prod.env", "server.pem", "server.key",
    "cert.crt", "cert.cer", "store.p12", "store.pfx", "keystore.jks", "app.keystore",
    "vault.kdbx", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa", "credentials",
    "credentials.json", "aws_credentials.txt", "client_secret.json", "secrets.yaml",
    ".git-credentials", ".netrc", ".npmrc", ".pypirc", "server.pem.txt", "prod.env.yaml"])
def test_secret_looking_names_are_excluded_by_the_pack(name):
    rt = _load("omli.boards.retrieve")
    assert rt.excluded_name(name), name


@pytest.mark.parametrize("name", ["notes.md", "README.md", "tokenizer.py", "keyboard.md",
                                  "environment.yaml", "design.md"])
def test_ordinary_names_are_allowed(name):
    rt = _load("omli.boards.retrieve")
    assert not rt.excluded_name(name), name


def test_the_secret_patterns_live_in_the_pack():
    vocab = _load("omli.boards.vocab")
    retrieval = vocab.section("retrieval")
    assert retrieval["skip_hidden"] is True
    assert ".env" in retrieval["skip_names"] and "*.pem" in retrieval["skip_names"]
    src = Path(_load("omli.boards.retrieve").__file__).read_text(encoding="utf-8")
    assert ".env" not in src and ".pem" not in src, "patterns belong in the pack, not in code"


@pytest.mark.parametrize("sub", [".config", ".git", ".git/info", "node_modules", ".venv",
                                 "secrets", "credentials", "docs/.private"])
def test_a_source_inside_a_hidden_skipped_or_secret_folder_is_refused(tmp_path, sub):
    rt = _load("omli.boards.retrieve")
    project = tmp_path / "Project"
    (project / sub).mkdir(parents=True)
    (project / sub / "notes.md").write_text("apples", encoding="utf-8")
    with pytest.raises(rt.OutsideProjects):
        rt.source_root(str(project / sub), [str(project)])


def test_an_ordinary_subfolder_is_still_a_valid_source(tmp_path):
    rt = _load("omli.boards.retrieve")
    project = tmp_path / "Project"
    (project / "docs").mkdir(parents=True)
    assert rt.source_root(str(project / "docs"), [str(project)]) == (project / "docs").resolve()


def test_folders_named_like_secret_stores_are_never_entered(tmp_path):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    for rel in ("notes.md", "secrets/prod.yaml", "credentials/gcp.json", "config/my-secret/a.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("apples", encoding="utf-8")
    hits = rt.retrieve("apples", [root.resolve()], k=50)
    assert [h["file"] for h in hits] == ["notes.md"]


@pytest.mark.skipif(sys.platform != "win32", reason="a directory junction is a Windows link")
@pytest.mark.parametrize("target", [".git", "secrets", ".config"])
def test_a_junction_into_a_blocked_folder_of_the_same_project_is_never_entered(tmp_path,
                                                                                target):
    rt = _load("omli.boards.retrieve")
    import _winapi
    root = tmp_path / "Project"
    (root / target).mkdir(parents=True)
    (root / target / "inside.md").write_text("apples behind a junction", encoding="utf-8")
    (root / "notes.md").write_text("apples in the open", encoding="utf-8")
    (root / "docs").mkdir()
    _winapi.CreateJunction(str(root / target), str(root / "docs" / "plain"))
    hits = rt.retrieve("apples", [rt.source_root(str(root), [str(root)])], k=50)
    assert [h["file"] for h in hits] == ["notes.md"]


@pytest.mark.parametrize("name", ["VENV", "Node_Modules", "__PYCACHE__"])
def test_skipped_folder_names_match_in_any_case(tmp_path, name):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    (root / name).mkdir(parents=True)
    (root / name / "x.md").write_text("apples", encoding="utf-8")
    (root / "notes.md").write_text("apples", encoding="utf-8")
    assert [h["file"] for h in rt.retrieve("apples", [root.resolve()], k=50)] == ["notes.md"]


# --------------------------------------------------------------------------------------------
# Link loops, and the depth and file-count limits
# --------------------------------------------------------------------------------------------

def _folder_link(target, link):
    """A junction on Windows, a symlink on macOS and Linux, from ``link`` to ``target``."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _retrieve_within(rt, query, root, seconds=30.0, **kw):
    """Run retrieval in a thread; fail instead of hanging when the walk does not end."""
    import threading

    out = {}
    t = threading.Thread(target=lambda: out.setdefault("hits", rt.retrieve(query, [root], **kw)),
                         daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "retrieval did not finish"
    return out["hits"]


def test_a_link_loop_returns_each_file_once_and_finishes(tmp_path):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    (root / "sub").mkdir(parents=True)
    (root / "notes.md").write_text("apples at the top", encoding="utf-8")
    (root / "sub" / "more.md").write_text("apples below", encoding="utf-8")
    _folder_link(root, root / "a")          # a loop back to the source itself
    _folder_link(root, root / "sub" / "b")  # and a second one, one level down
    src = rt.source_root(str(root), [str(root)])
    hits = _retrieve_within(rt, "apples", src, k=100)
    assert sorted(h["file"] for h in hits) == ["notes.md", "sub/more.md"]


def test_two_loops_around_files_that_are_never_read_still_finish(tmp_path):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    root.mkdir()
    (root / "data.bin").write_bytes(b"apples")
    _folder_link(root, root / "a")
    _folder_link(root, root / "b")
    assert _retrieve_within(rt, "apples", rt.source_root(str(root), [str(root)]), k=10) == []


def test_a_link_to_another_folder_of_the_source_is_read_once(tmp_path):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "plan.md").write_text("apples in the plan", encoding="utf-8")
    _folder_link(root / "docs", root / "zz-alias")
    hits = _retrieve_within(rt, "apples", rt.source_root(str(root), [str(root)]), k=10)
    assert [h["file"] for h in hits] == ["docs/plan.md"]


# Each released content of the mindmap pack, by version. Changing the pack means a new
# version and a new line here; an old line is never edited.
PACK_CONTENT_BY_VERSION = {
    "0.2.0": "a97d261f3ac181900de9f747b5df0f1601fc8782d5487bfa8b483268243341aa",
}


def test_the_mindmap_pack_content_matches_its_version():
    import hashlib

    pack = Path(_load("omli.boards.vocab").__file__).resolve().parent.parent / "defaults" / "mindmap.yaml"
    name, version = _load("omli.boards.vocab").identity()
    digest = hashlib.sha256(pack.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert name == "mindmap"
    assert PACK_CONTENT_BY_VERSION.get(version) == digest, (
        f"the mindmap pack changed but its version is still {version}: give it a new version "
        "and add that version's content hash to PACK_CONTENT_BY_VERSION")


def test_the_walk_limits_are_in_the_pack():
    retrieval = _load("omli.boards.vocab").section("retrieval")
    assert int(retrieval["max_depth"]) > 0 and int(retrieval["max_files"]) > 0
    assert int(retrieval["max_dirs"]) > 0


def _with_limits(monkeypatch, rt, **changes):
    real = dict(rt.section("retrieval"))
    real.update(changes)
    monkeypatch.setattr(rt, "section", lambda name: real if name == "retrieval" else None)


def test_the_walk_stops_at_the_pack_depth_limit(tmp_path, monkeypatch):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    (root / "d1" / "d2" / "d3").mkdir(parents=True)
    (root / "d1" / "d2" / "near.md").write_text("apples", encoding="utf-8")
    (root / "d1" / "d2" / "d3" / "far.md").write_text("apples", encoding="utf-8")
    _with_limits(monkeypatch, rt, max_depth=2)
    hits = rt.retrieve("apples", [root.resolve()], k=10)
    assert [h["file"] for h in hits] == ["d1/d2/near.md"]


def test_the_file_limit_counts_every_file_looked_at(tmp_path, monkeypatch):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    root.mkdir()
    for n in range(5):
        (root / f"a{n}.bin").write_bytes(b"apples")
    (root / "z.md").write_text("apples", encoding="utf-8")
    _with_limits(monkeypatch, rt, max_files=3)
    assert rt.retrieve("apples", [root.resolve()], k=10) == []


def _project_with_docs(tmp_path):
    root = tmp_path / "Project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "plan.md").write_text("apples apples apples", encoding="utf-8")
    (root / "other.md").write_text("apples", encoding="utf-8")
    return root.resolve()


@pytest.mark.parametrize("order", ["same twice", "folder then subfolder", "subfolder then folder"])
def test_overlapping_sources_read_each_file_once(tmp_path, order):
    rt = _load("omli.boards.retrieve")
    root = _project_with_docs(tmp_path)
    roots = {"same twice": [root, root],
             "folder then subfolder": [root, root / "docs"],
             "subfolder then folder": [root / "docs", root]}[order]
    hits = rt.retrieve("apples", roots, k=10)
    names = sorted((Path(h["root"]) / h["file"]).resolve().as_posix() for h in hits)
    assert names == sorted({(root / "docs" / "plan.md").as_posix(), (root / "other.md").as_posix()})


def test_repeated_sources_do_not_fill_every_snippet_with_one_file(tmp_path):
    rt = _load("omli.boards.retrieve")
    root = _project_with_docs(tmp_path)
    hits = rt.retrieve("apples", [root] * 5, k=5)
    assert [h["file"] for h in hits] == ["docs/plan.md", "other.md"]


def test_the_file_limit_is_shared_by_every_source_of_a_request(tmp_path, monkeypatch):
    rt = _load("omli.boards.retrieve")
    for name in ("A", "B"):
        (tmp_path / name).mkdir()
        for n in (1, 2):
            (tmp_path / name / f"{name.lower()}{n}.md").write_text("apples", encoding="utf-8")
    _with_limits(monkeypatch, rt, max_files=3)
    hits = rt.retrieve("apples", [(tmp_path / "A").resolve(), (tmp_path / "B").resolve()], k=10)
    assert sorted(h["file"] for h in hits) == ["a1.md", "a2.md", "b1.md"]


def test_a_wide_folder_is_examined_only_up_to_the_folder_limit(tmp_path, monkeypatch):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    for n in range(50):
        (root / f"d{n:02}").mkdir(parents=True)
    calls = []
    real_enter = rt._enter
    monkeypatch.setattr(rt, "_enter", lambda *a: calls.append(a[1]) or real_enter(*a))
    _with_limits(monkeypatch, rt, max_dirs=10)
    rt.retrieve("apples", [root.resolve()], k=10)
    assert len(calls) <= 10, f"{len(calls)} folders examined with a limit of 10"


def test_the_walk_stops_at_the_pack_folder_limit(tmp_path, monkeypatch):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    for n in range(10):
        (root / f"d{n}").mkdir(parents=True)
        (root / f"d{n}" / "x.md").write_text("apples", encoding="utf-8")
    _with_limits(monkeypatch, rt, max_dirs=3)
    hits = rt.retrieve("apples", [root.resolve()], k=10)
    assert sorted(h["file"] for h in hits) == ["d0/x.md", "d1/x.md"]


def test_a_link_is_no_shortcut_past_the_depth_limit(tmp_path, monkeypatch):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    deep = root.joinpath(*[f"d{n}" for n in range(5)])
    deep.mkdir(parents=True)
    (deep / "far.md").write_text("apples", encoding="utf-8")
    _folder_link(deep, root / "shortcut")
    _with_limits(monkeypatch, rt, max_depth=2)
    assert _retrieve_within(rt, "apples", rt.source_root(str(root), [str(root)]), k=10) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a folder on Windows cannot hold a FIFO")
def test_a_pipe_named_like_a_text_file_is_never_opened(tmp_path):
    rt = _load("omli.boards.retrieve")
    root = tmp_path / "Project"
    root.mkdir()
    os.mkfifo(root / "notes.md")
    (root / "plan.md").write_text("apples", encoding="utf-8")
    hits = _retrieve_within(rt, "apples", rt.source_root(str(root), [str(root)]), k=10)
    assert [h["file"] for h in hits] == ["plan.md"]
