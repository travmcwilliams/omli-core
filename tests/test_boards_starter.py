"""The "How OMLi works" starter board ships as data inside the package and describes only
modules that exist in this repository."""

from __future__ import annotations

import importlib
import importlib.util
import json

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


def _starter_records(boards):
    raw = boards.starter_bytes()
    assert raw.endswith(b"\n")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def test_the_starter_board_is_package_data(home):
    boards = _load("omli.boards")
    assert boards.STARTER_BOARD == "how-omli-works"
    records = _starter_records(boards)
    assert records and all(r["v"] == 1 for r in records)


def test_every_module_on_the_starter_board_exists(home):
    boards = _load("omli.boards")
    named = [r["data"]["module"] for r in _starter_records(boards)
             if r["kind"] == "node.add" and "module" in r.get("data", {})]
    assert len(named) >= 8
    for module in named:
        assert module.startswith("omli"), module
        assert importlib.util.find_spec(module) is not None, f"{module} is not in the package"


def test_installing_the_starter_board_replays_cleanly_and_never_overwrites(home):
    boards = _load("omli.boards")
    log = boards.install_starter()
    assert log.path == home / "boards" / "how-omli-works.jsonl"
    board = log.replay()
    assert len(board["nodes"]) >= 8 and board["links"]
    log.append("node.edit", op="mine", node=next(iter(board["nodes"])), data={"text": "my note"})
    size = log.path.stat().st_size
    again = boards.install_starter()
    assert again.path.stat().st_size == size, "a second install must not touch an existing board"
