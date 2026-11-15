"""One save = one locked write of every operation, one fsync, all or nothing."""

from __future__ import annotations

import importlib
import os

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


def _save(n):
    ops = [{"kind": "node.add", "op": f"n{i}", "node": f"node-{i}", "data": {"text": str(i)}}
           for i in range(n)]
    ops.append({"kind": "link.add", "op": "l0", "link": "link-0",
                "from": "node-0", "to": "node-1", "data": {}})
    return ops


def _method(log):
    fn = getattr(log, "append_many", None)
    if fn is None:
        pytest.fail("BoardLog.append_many does not exist yet")
    return fn


def test_a_save_lands_every_operation_in_order(home):
    boards = _load("omli.boards")
    log = boards.BoardLog("batch")
    written = _method(log)(_save(5), by="alice")
    assert [r["op"] for r in written] == ["n0", "n1", "n2", "n3", "n4", "l0"]
    assert [r["op"] for r in log.ops()] == ["n0", "n1", "n2", "n3", "n4", "l0"]
    assert all(r["by"] == "alice" for r in log.ops())
    assert set(log.replay()["nodes"]) == {f"node-{i}" for i in range(5)}


def test_a_save_is_one_write_and_one_fsync(home, monkeypatch):
    boards = _load("omli.boards")
    log_mod = importlib.import_module("omli.boards.log")
    log = boards.BoardLog("batch")
    log.append("checkpoint", op="first", label="start")
    writes, syncs = [], []
    real_write, real_fsync = os.write, os.fsync
    monkeypatch.setattr(log_mod.os, "write", lambda fd, b: writes.append(len(b)) or real_write(fd, b))
    monkeypatch.setattr(log_mod.os, "fsync", lambda fd: syncs.append(fd) or real_fsync(fd))
    _method(log)(_save(30))
    assert len(writes) == 1 and len(syncs) == 1


@pytest.mark.parametrize("bad", ["duplicate-in-save", "duplicate-in-log", "impossible",
                                 "malformed", "reserved"])
def test_one_bad_operation_writes_nothing(home, bad):
    boards = _load("omli.boards")
    log = boards.BoardLog("batch")
    log.append("node.add", op="old", node="kept", data={})
    before = log.path.read_bytes()
    ops = _save(3)
    if bad == "duplicate-in-save":
        ops.append(dict(ops[0], node="other"))
    elif bad == "duplicate-in-log":
        ops.append({"kind": "checkpoint", "op": "old", "label": "x"})
    elif bad == "impossible":
        ops.append({"kind": "node.edit", "op": "e1", "node": "missing", "data": {}})
    elif bad == "malformed":
        ops.append({"kind": "node.move", "op": "m1", "node": "node-0", "x": "left", "y": 0})
    else:
        ops.append({"kind": "checkpoint", "op": "c1", "label": "x", "at": "yesterday"})
    with pytest.raises(ValueError):
        _method(log)(ops)
    assert log.path.read_bytes() == before


def test_an_empty_save_writes_nothing(home):
    boards = _load("omli.boards")
    log = boards.BoardLog("batch")
    assert _method(log)([]) == []
    assert not log.path.exists()
