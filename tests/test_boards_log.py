"""The board edit log: one append-only file per board under OMLI_HOME.

Replaying the log gives the board; replaying up to an operation gives the board at that
point. Nothing already written is ever rewritten: restore appends a new operation.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import random
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
def boards():
    return _load("omli.boards")


def _lines(path):
    return path.read_bytes().decode("utf-8").splitlines()


# --- the reference model: the same rules, written independently of the module -------------

def _apply(state, op, history):
    kind = op["kind"]
    nodes, links = state["nodes"], state["links"]
    if kind == "node.add":
        nodes[op["node"]] = dict(op.get("data") or {})
    elif kind == "node.edit":
        for k, v in (op.get("data") or {}).items():
            if v is None:
                nodes[op["node"]].pop(k, None)
            else:
                nodes[op["node"]][k] = v
    elif kind == "node.move":
        nodes[op["node"]]["x"], nodes[op["node"]]["y"] = op["x"], op["y"]
        if "parent" in op:
            nodes[op["node"]]["parent"] = op["parent"]
    elif kind == "node.delete":
        del nodes[op["node"]]
        for lid in [l for l, v in links.items() if op["node"] in (v["from"], v["to"])]:
            del links[lid]
    elif kind == "link.add":
        links[op["link"]] = {"from": op["from"], "to": op["to"], **(op.get("data") or {})}
    elif kind == "link.delete":
        del links[op["link"]]
    elif kind == "restore":
        state = copy.deepcopy(history[op["to"]])
    return state


def _random_ops(rng, n):
    """A valid random sequence, with the expected board after each operation."""
    state, history, ops, expected, ids = {"nodes": {}, "links": {}}, {}, [], [], 0
    for i in range(n):
        nodes, links = state["nodes"], state["links"]
        choices = ["node.add", "checkpoint"]
        if nodes:
            choices += ["node.edit", "node.move", "node.delete", "node.add"]
        if len(nodes) >= 2:
            choices += ["link.add", "link.add"]
        if links:
            choices.append("link.delete")
        if ops:
            choices.append("restore")
        kind = rng.choice(choices)
        op = {"kind": kind, "op": f"op-{i}"}
        if kind == "node.add":
            ids += 1
            op.update(node=f"n{ids}", data={"text": f"idea {ids}", "x": rng.randint(0, 99)})
        elif kind == "node.edit":
            target = rng.choice(sorted(nodes))
            data = {"text": f"edited {i}"}
            if rng.random() < 0.3:
                data["x"] = None
            op.update(node=target, data=data)
        elif kind == "node.move":
            op.update(node=rng.choice(sorted(nodes)), x=rng.randint(-50, 50), y=rng.randint(-50, 50))
            if rng.random() < 0.5:
                op["parent"] = rng.choice(sorted(nodes))
        elif kind == "node.delete":
            op.update(node=rng.choice(sorted(nodes)))
        elif kind == "link.add":
            a, b = rng.sample(sorted(nodes), 2)
            ids += 1
            op.update(link=f"l{ids}", **{"from": a, "to": b}, data={"label": f"rel {i}"})
        elif kind == "link.delete":
            op.update(link=rng.choice(sorted(links)))
        elif kind == "restore":
            op.update(to=rng.choice(ops)["op"])
        elif kind == "checkpoint":
            op.update(label=f"mark {i}")
        state = _apply(state, op, history)
        history[op["op"]] = copy.deepcopy(state)
        ops.append(op)
        expected.append(copy.deepcopy(state))
    return ops, expected


def _write(log, op):
    fields = {k: v for k, v in op.items() if k not in ("kind", "op")}
    return log.append(op["kind"], op=op["op"], by="key-a", **fields)


# --- replay equals the board built in memory ------------------------------------------------

@pytest.mark.parametrize("seed", range(12))
def test_replaying_random_operations_gives_the_board_built_in_memory(boards, home, seed):
    rng = random.Random(seed)
    ops, expected = _random_ops(rng, 40)
    log = boards.BoardLog(f"board-{seed}")
    for op in ops:
        _write(log, op)
    assert log.replay() == expected[-1]
    for i in rng.sample(range(len(ops)), 8):
        assert log.replay(upto=ops[i]["op"]) == expected[i], f"board at {ops[i]['op']}"
    # A fresh reader of the same file agrees.
    assert boards.BoardLog(f"board-{seed}").replay() == expected[-1]


# --- restore never rewrites a byte already written ------------------------------------------

def test_restore_appends_and_leaves_the_earlier_bytes_unchanged(boards, home):
    log = boards.BoardLog("restore-demo")
    log.append("node.add", op="a", node="n1", data={"text": "first"})
    log.append("checkpoint", op="c1", label="before the edits")
    log.append("node.edit", op="b", node="n1", data={"text": "second"})
    log.append("node.add", op="c", node="n2", data={"text": "other"})
    before = log.path.read_bytes()
    prefix_hash = hashlib.sha256(before).hexdigest()

    written = log.restore("c1", by="key-a")

    after = log.path.read_bytes()
    assert hashlib.sha256(after[:len(before)]).hexdigest() == prefix_hash
    assert len(after) > len(before)
    assert written["kind"] == "restore" and written["to"] == "c1"
    assert log.replay() == {"nodes": {"n1": {"text": "first"}}, "links": {}}
    assert len(_lines(log.path)) == 5


def test_restore_to_an_unknown_or_later_point_is_refused(boards, home):
    log = boards.BoardLog("restore-refused")
    log.append("node.add", op="a", node="n1", data={})
    size = log.path.stat().st_size
    with pytest.raises(ValueError):
        log.restore("no-such-op")
    assert log.path.stat().st_size == size


# --- append only ----------------------------------------------------------------------------

def test_the_log_has_no_update_or_delete_call(boards):
    names = {n.lower() for n, _ in inspect.getmembers(boards.BoardLog)}
    for word in ("update", "delete", "remove", "rewrite", "truncate", "replace", "set", "clear",
                 "edit", "pop", "compact"):
        assert not any(word in n for n in names if not n.startswith("__")), word
    src = inspect.getsource(_load("omli.boards.log"))
    for forbidden in ("O_TRUNC", "write_text(", "write_bytes(", '"w"', "'w'", '"wb"', "'wb'",
                      ".truncate(", "os.replace(", "os.remove(", "unlink("):
        assert forbidden not in src, forbidden


def test_a_duplicate_operation_id_is_refused(boards, home):
    log = boards.BoardLog("dupes")
    log.append("node.add", op="same", node="n1", data={})
    size = log.path.stat().st_size
    with pytest.raises(ValueError, match="same"):
        log.append("node.add", op="same", node="n2", data={})
    assert log.path.stat().st_size == size


def test_a_malformed_line_raises_an_error_that_names_the_line(boards, home):
    log = boards.BoardLog("broken")
    log.append("node.add", op="a", node="n1", data={})
    log.append("node.add", op="b", node="n2", data={})
    with open(log.path, "ab") as fh:
        fh.write(b'{"kind": "node.add", "op": "c"\n')
    with pytest.raises(boards.BoardLogError, match="line 3"):
        boards.BoardLog("broken").replay()
    with pytest.raises(boards.BoardLogError, match="line 3"):
        log.append("node.add", op="d", node="n3", data={})


def test_an_operation_on_a_missing_node_is_refused_and_named(boards, home):
    log = boards.BoardLog("bad-op")
    with pytest.raises(ValueError):
        log.append("node.edit", op="a", node="ghost", data={"text": "x"})
    assert not log.path.exists() or log.path.read_bytes() == b""
    log.append("node.add", op="b", node="n1", data={})
    with open(log.path, "ab") as fh:
        fh.write((json.dumps({"v": 1, "kind": "node.move", "op": "c", "node": "ghost",
                              "x": 1, "y": 2, "at": "2026-01-01T00:00:00+00:00",
                              "by": None}) + "\n").encode())
    with pytest.raises(boards.BoardLogError, match="line 2"):
        log.replay()


def test_a_torn_last_line_fails_closed(boards, home):
    log = boards.BoardLog("torn")
    log.append("node.add", op="a", node="n1", data={})
    with open(log.path, "ab") as fh:
        fh.write(b'{"kind": "node.add"')
    with pytest.raises(boards.BoardLogError, match="line 2"):
        log.replay()


def test_records_are_one_json_object_per_line_with_who_and_when(boards, home):
    log = boards.BoardLog("shape")
    log.append("node.add", op="a", by="key-a", node="n1", data={"text": "x"})
    rec = json.loads(_lines(log.path)[0])
    assert rec["v"] == 1 and rec["op"] == "a" and rec["by"] == "key-a" and rec["at"]
    assert log.path.read_bytes().endswith(b"\n")
    generated = log.append("checkpoint", label="no id given")
    assert generated["op"] and generated["op"] != "a"


@pytest.mark.parametrize("bad", ["", "Has Caps", "../escape", "a/b", "a" * 65, "-lead", "x.jsonl",
                                 "abc\n", "con", "prn", "aux", "nul", "com1", "com9", "lpt1",
                                 "lpt9"])
def test_a_bad_board_id_is_refused(boards, home, bad):
    with pytest.raises(ValueError):
        boards.BoardLog(bad)


def test_an_operation_id_with_a_trailing_newline_is_refused(boards, home):
    log = boards.BoardLog("ids")
    with pytest.raises(ValueError):
        log.append("node.add", op="a\n", node="n1", data={})
    assert not log.path.exists() or log.ops() == []


def test_a_node_id_with_a_trailing_newline_is_refused_on_save(home):
    diff = _load("omli.boards.diff")
    with pytest.raises(ValueError):
        diff.check_board({"nodes": {"n1\n": {"x": 0, "y": 0}}, "links": {}})


# --- the log path depends on OMLI_HOME, never on the working directory ----------------------

def test_the_log_path_comes_from_omli_home_not_the_working_directory(boards, tmp_path, monkeypatch):
    home = tmp_path / "Home A"
    elsewhere, later = tmp_path / "Somewhere Else", tmp_path / "Later Dir"
    elsewhere.mkdir()
    later.mkdir()
    monkeypatch.setenv("OMLI_HOME", str(home))
    monkeypatch.chdir(elsewhere)
    log = boards.BoardLog("where")
    monkeypatch.chdir(later)
    log.append("node.add", op="a", node="n1", data={})
    expected = home.resolve() / "boards" / "where.jsonl"
    assert log.path == expected and expected.exists()
    assert boards.board_path("where") == expected
    assert not (elsewhere / "boards").exists() and not (later / "boards").exists()
