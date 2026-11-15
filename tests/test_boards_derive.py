"""Derived from the board log, never stored as truth: last editor and edit counts per node,
and the board list with search. The index is a cache: deleting it loses nothing."""

from __future__ import annotations

import importlib

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


def _two_boards(boards):
    a = boards.BoardLog("garden-plans")
    a.append("node.add", op="1", by="key-a", node="n1", data={"text": "Tomatoes"})
    a.append("node.add", op="2", by="key-b", node="n2", data={"text": "Compost heap"})
    a.append("node.edit", op="3", by="key-b", node="n1", data={"text": "Cherry tomatoes"})
    a.append("node.move", op="4", by="key-c", node="n1", x=5, y=6)
    b = boards.BoardLog("reading-list")
    b.append("node.add", op="1", by="key-a", node="n1", data={"text": "A history of maps"})
    return a, b


def test_last_editor_and_edit_counts_come_from_the_log(boards, home):
    a, _ = _two_boards(boards)
    stats = boards.node_stats(a)
    assert stats["n1"] == {"edits": 3, "last_by": "key-c", "last_op": "4",
                           "last_at": stats["n1"]["last_at"]}
    assert stats["n2"]["edits"] == 1 and stats["n2"]["last_by"] == "key-b"
    a.append("node.delete", op="5", by="key-a", node="n2")
    assert "n2" not in boards.node_stats(a)


def test_stats_follow_a_restore(boards, home):
    a, _ = _two_boards(boards)
    a.restore("2", by="key-d", op="6")
    stats = boards.node_stats(a)
    assert set(stats) == {"n1", "n2"}
    assert stats["n1"]["edits"] == 3  # history is history: the restore rewrote nothing


def test_the_board_list_and_search(boards, home):
    _two_boards(boards)
    listed = boards.list_boards()
    assert [b["board"] for b in listed] == ["garden-plans", "reading-list"]
    garden = listed[0]
    assert garden["nodes"] == 2 and garden["ops"] == 4 and garden["last_by"] == "key-c"
    assert [b["board"] for b in boards.list_boards("TOMATO")] == ["garden-plans"]
    assert [b["board"] for b in boards.list_boards("maps")] == ["reading-list"]
    assert [b["board"] for b in boards.list_boards("reading")] == ["reading-list"]
    assert boards.list_boards("nothing like this") == []


def test_deleting_the_index_loses_nothing(boards, home):
    a, _ = _two_boards(boards)
    first = boards.list_boards()
    index = home / "boards" / "index.json"
    assert index.exists()
    index.unlink()
    assert boards.list_boards() == first
    assert index.exists()
    boards.rebuild_index()
    assert boards.list_boards() == first


def test_a_stale_index_is_never_trusted(boards, home):
    a, _ = _two_boards(boards)
    boards.list_boards()
    a.append("node.add", op="5", by="key-z", node="n9", data={"text": "Sunflowers"})
    listed = {b["board"]: b for b in boards.list_boards()}
    assert listed["garden-plans"]["nodes"] == 3 and listed["garden-plans"]["last_by"] == "key-z"
    assert [b["board"] for b in boards.list_boards("sunflower")] == ["garden-plans"]


def test_a_corrupt_index_is_rebuilt_from_the_logs(boards, home):
    _two_boards(boards)
    first = boards.list_boards()
    (home / "boards" / "index.json").write_text("{not json", encoding="utf-8")
    assert boards.list_boards() == first


def test_a_stray_file_whose_name_is_not_a_usable_board_id_is_left_out(boards, home, monkeypatch):
    """A file in boards/ named like a Windows device (for example one copied from another
    system) is skipped by the board list, not allowed to break it."""
    log = importlib.import_module("omli.boards.log")
    boards.BoardLog("kept").append("node.add", node="n1", data={"text": "kept"})
    monkeypatch.setattr(log, "_DEVICE_NAMES", frozenset({"stray"}))
    (home / "boards" / "stray.jsonl").write_bytes(b"")
    assert [b["board"] for b in boards.list_boards(home=home)] == ["kept"]
