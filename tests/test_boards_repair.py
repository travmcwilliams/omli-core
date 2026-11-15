"""Crash recovery for a half-written last line: a command a person runs, never automatic.

It handles only an unterminated final line outside the sealed part: the torn bytes go to a
side file and every complete line stays byte for byte. A torn or changed line inside the
sealed part is refused, and verification still fails on it.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

TORN = b'{"by":null,"kind":"node.add","node":"half'


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


def _board(boards, name="crashed"):
    log = boards.BoardLog(name)
    log.append("node.add", op="a", node="n1", data={"text": "alpha"})
    log.append("node.add", op="b", node="n2", data={"text": "bravo"})
    return log


def _tear(log):
    with open(log.path, "ab") as fh:
        fh.write(TORN)


def _repair(argv):
    cli = _load("omli.boards.__main__")
    return cli.main(argv)


def _verify(path):
    cli = importlib.import_module("omli.integrity.__main__")
    return cli.main(["verify", str(path), "--no-anchor", "--no-sign"])


def _side_files(log):
    return sorted(log.path.parent.glob(f"{log.board}.torn-*"))


def test_today_a_torn_last_line_stops_reads_and_writes_and_names_the_line(home):
    boards = _load("omli.boards")
    log = _board(boards)
    _tear(log)
    before = log.path.read_bytes()
    with pytest.raises(boards.BoardLogError, match=r"crashed\.jsonl line 3: the last line is incomplete"):
        log.replay()
    with pytest.raises(boards.BoardLogError, match="line 3"):
        log.append("checkpoint", op="c", label="after")
    assert log.path.read_bytes() == before  # nothing repairs it on its own


def test_repair_moves_only_the_torn_bytes_to_a_side_file(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    complete = log.path.read_bytes()
    _tear(log)
    assert _repair(["repair-tail", "crashed"]) == 0
    assert log.path.read_bytes() == complete
    sides = _side_files(log)
    assert len(sides) == 1 and sides[0].read_bytes() == TORN
    assert set(log.replay()["nodes"]) == {"n1", "n2"}
    out = capsys.readouterr().out
    assert str(len(TORN)) in out and sides[0].name in out


def test_repair_changes_nothing_when_the_last_line_is_complete(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    before = log.path.read_bytes()
    assert _repair(["repair-tail", "crashed"]) == 0
    assert log.path.read_bytes() == before and _side_files(log) == []
    assert "nothing to repair" in capsys.readouterr().out


def test_repair_twice_never_overwrites_an_earlier_side_file(home):
    boards = _load("omli.boards")
    log = _board(boards)
    _tear(log)
    assert _repair(["repair-tail", "crashed"]) == 0
    _tear(log)
    assert _repair(["repair-tail", "crashed"]) == 0
    sides = _side_files(log)
    assert len(sides) == 2 and all(p.read_bytes() == TORN for p in sides)


def test_a_torn_line_after_the_sealed_part_is_repaired_and_the_seal_still_holds(home):
    boards = _load("omli.boards")
    log = _board(boards)
    boards.seal_board("crashed")
    log.append("checkpoint", op="c", label="after the seal")
    complete = log.path.read_bytes()
    _tear(log)
    assert _repair(["repair-tail", "crashed"]) == 0
    assert log.path.read_bytes() == complete
    assert _verify(log.path) == 0


def test_a_torn_line_inside_the_sealed_part_is_refused_and_fails_verification(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    boards.seal_board("crashed")
    data = log.path.read_bytes()
    log.path.write_bytes(data[:-5])  # the last sealed line loses its end
    torn = log.path.read_bytes()
    assert _repair(["repair-tail", "crashed"]) == 1
    assert log.path.read_bytes() == torn and _side_files(log) == []
    assert "sealed" in capsys.readouterr().err
    assert _verify(log.path) == 1


def test_a_torn_line_the_seal_itself_covers_is_refused(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    _tear(log)
    boards.seal_board("crashed")  # the seal now vouches for the torn bytes as they are
    torn = log.path.read_bytes()
    assert _repair(["repair-tail", "crashed"]) == 1
    assert log.path.read_bytes() == torn and _side_files(log) == []
    assert "inside the sealed part" in capsys.readouterr().err


def test_a_changed_sealed_line_is_refused_even_with_a_torn_tail(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    boards.seal_board("crashed")
    data = log.path.read_bytes().replace(b"alpha", b"alphA")
    log.path.write_bytes(data + TORN)
    assert _repair(["repair-tail", "crashed"]) == 1
    assert log.path.read_bytes() == data + TORN and _side_files(log) == []
    assert _verify(log.path) == 1


def test_an_unknown_board_is_an_error_not_a_new_file(home, capsys):
    _load("omli.boards")
    assert _repair(["repair-tail", "never-made"]) == 1
    assert not (home / "boards" / "never-made.jsonl").exists()


def test_nothing_but_the_command_calls_the_repair():
    root = Path(__file__).resolve().parent.parent / "omli"
    _load("omli.boards.repair")
    callers = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.py")
                     if re.search(r"\brepair_tail\b", p.read_text(encoding="utf-8")))
    assert callers == ["boards/__main__.py", "boards/repair.py"]
