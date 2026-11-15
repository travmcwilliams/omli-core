"""A board log is sealed and verified by the existing omli.integrity command, unchanged."""

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
    monkeypatch.delenv("OMLI_SEAL_KEY", raising=False)
    return h.resolve()


def _board(boards):
    log = boards.BoardLog("sealed-board")
    log.append("node.add", op="a", node="n1", data={"text": "alpha"})
    log.append("node.add", op="b", node="n2", data={"text": "bravo"})
    log.append("link.add", op="c", link="l1", **{"from": "n1", "to": "n2"}, data={})
    return log


def _verify(log, capsys):
    cli = _load("omli.integrity.__main__")
    rc = cli.main(["verify", str(log.path), "--no-anchor", "--no-sign"])
    return rc, capsys.readouterr().out


def test_the_existing_verify_command_works_on_a_sealed_board_log(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    result = boards.seal_board("sealed-board")
    assert result.count == 3 and result.seal == home / "boards" / "sealed-board.seal.json"
    rc, out = _verify(log, capsys)
    assert rc == 0 and "integrity: OK" in out
    log.append("node.edit", op="d", node="n1", data={"text": "later"})
    rc, out = _verify(log, capsys)
    assert rc == 0 and "appended" in out


def test_the_existing_seal_command_works_on_a_board_log(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    cli = _load("omli.integrity.__main__")
    assert cli.main(["seal", str(log.path), "--no-anchor", "--no-sign"]) == 0
    capsys.readouterr()
    rc, out = _verify(log, capsys)
    assert rc == 0 and "integrity: OK" in out


def test_a_changed_byte_inside_the_sealed_part_fails_verify_with_exit_1(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    boards.seal_board("sealed-board")
    data = bytearray(log.path.read_bytes())
    at = data.index(b"bravo")
    data[at] = ord("B")  # still valid JSON: only the integrity chain can see it
    log.path.write_bytes(bytes(data))
    rc, out = _verify(log, capsys)
    assert rc == 1
    assert "integrity: TAMPERED" in out and "first altered at record index 1" in out


def test_a_changed_byte_in_the_seal_reads_corrupt_and_exits_1(home, capsys):
    boards = _load("omli.boards")
    log = _board(boards)
    result = boards.seal_board("sealed-board")
    raw = bytearray(result.seal.read_bytes())
    raw[0] = ord("[") if raw[0] != ord("[") else ord("{")  # the seal no longer parses as one
    result.seal.write_bytes(bytes(raw))
    rc, out = _verify(log, capsys)
    assert rc == 1 and "integrity: CORRUPT" in out
