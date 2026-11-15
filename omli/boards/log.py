"""One append-only edit log per board: ``OMLI_HOME/boards/<board>.jsonl``.

Each line is one JSON object (sorted keys, ``\\n``-terminated) recording one operation:

  node.add    node, data            a new node; ``data`` holds its fields
  node.edit   node, data            merge fields into a node; a null value removes the field
  node.move   node, x, y[, parent]  place a node
  node.delete node                  remove a node and every link touching it
  link.add    link, from, to, data  a new link between two existing nodes
  link.delete link                  remove a link
  restore     to                    the board becomes the board as of the earlier op ``to``
  checkpoint  label                 a named point; changes nothing

and on every record ``v`` (format), ``op`` (unique within the board), ``at`` (UTC) and ``by``.

The board is never stored: it is replayed from the log, and replaying up to an operation gives
the board at that point (Primitive 3). Nothing already written is rewritten: there is no update
or delete call, and a restore is a new operation pointing at an earlier one (Primitive 4).
Every write runs under a cross-process lock and lands as one append of whole lines (a save is one
write and one fsync: ``append_many``), so two writers cannot interleave half-lines. A malformed
or torn line fails closed with an error that names the line; only a person repairs a torn last
line (``python -m omli.boards repair-tail``). The file is plain JSONL, so ``omli.integrity``
seals and verifies it unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from omli.home import omli_home

FORMAT = 1
BOARDS_DIR = "boards"
KINDS = ("node.add", "node.edit", "node.move", "node.delete", "link.add", "link.delete",
         "restore", "checkpoint")
_BOARD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESERVED = {"v", "kind", "op", "at", "by"}
# Names Windows keeps for devices, whatever the extension: a board file with one of these
# names would open the device instead of a file.
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)}
                          | {f"lpt{i}" for i in range(1, 10)})
LOCK_TIMEOUT_SECONDS = 60.0

_THREAD_LOCK = threading.Lock()

Board = Dict[str, Dict[str, Dict[str, Any]]]


class BoardLogError(ValueError):
    """The log cannot be read as a board: a malformed, torn or impossible line."""


class BoardConflict(ValueError):
    """A save was based on an earlier version of the board. ``head`` is the current one."""

    def __init__(self, head: Optional[str]):
        super().__init__("the board changed since it was loaded; reload it")
        self.head = head


_ANY_HEAD = object()  # BoardLog.save without a version check


def content_head(records: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The board's version: the id of its last operation that changes the board. A
    ``checkpoint`` changes nothing, so it does not make the board a newer version."""
    for rec in reversed(records):
        if rec.get("kind") != "checkpoint":
            return rec["op"]
    return None


def _records(ops: Sequence[Mapping[str, Any]], by: Optional[str]) -> List[dict]:
    """Full records for ``ops``, each checked for shape. Raises ValueError, writing nothing."""
    at = datetime.now(timezone.utc).isoformat()
    recs = []
    for item in ops:
        fields = dict(item)
        kind, op = fields.pop("kind", None), fields.pop("op", None)
        clash = _RESERVED.intersection(fields)
        if clash:
            raise ValueError(f"reserved field(s): {sorted(clash)}")
        rec = {"v": FORMAT, "kind": kind, "op": op or secrets.token_hex(8), "by": by,
               "at": at, **fields}
        why = _check_shape(rec)
        if why:
            raise ValueError(why)
        recs.append(rec)
    return recs


def _empty() -> Board:
    return {"nodes": {}, "links": {}}


def board_path(board: str, home: str | Path | None = None) -> Path:
    """Where a board's log lives: under ``home``, else OMLI_HOME (resolved now)."""
    if not isinstance(board, str) or not _BOARD_ID.fullmatch(board) or board in _DEVICE_NAMES:
        raise ValueError("a board id is 1-64 lowercase letters, digits or '-', "
                         "starting with a letter or digit, and not a device name such as "
                         "'con' or 'nul'")
    root = Path(home).resolve() if home is not None else omli_home()
    return root / BOARDS_DIR / f"{board}.jsonl"


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """An exclusive lock shared by every process that writes this board."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while True:
                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"{lock_path.name} stayed locked") from None
                        time.sleep(0.002)
                try:
                    yield
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _check_shape(rec: object) -> str:
    """Why ``rec`` is not a well-formed operation, or "" when it is."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    if rec.get("v") != FORMAT:
        return "unknown format version"
    kind = rec.get("kind")
    if kind not in KINDS:
        return "unknown operation kind"
    if not isinstance(rec.get("op"), str) or not _OP_ID.fullmatch(rec["op"]):
        return "missing or bad operation id"
    if not isinstance(rec.get("at"), str):
        return "missing time"
    if rec.get("by") is not None and not isinstance(rec.get("by"), str):
        return "bad author"
    need = {"node.add": ("node",), "node.edit": ("node",), "node.move": ("node",),
            "node.delete": ("node",), "link.add": ("link", "from", "to"),
            "link.delete": ("link",), "restore": ("to",), "checkpoint": ("label",)}[kind]
    for field in need:
        if not isinstance(rec.get(field), str) or not rec[field]:
            return f"missing {field}"
    if "data" in rec and not isinstance(rec["data"], dict):
        return "data is not an object"
    if kind == "node.move":
        for axis in ("x", "y"):
            if not isinstance(rec.get(axis), (int, float)) or isinstance(rec.get(axis), bool):
                return f"missing {axis}"
        if "parent" in rec and rec["parent"] is not None and not isinstance(rec["parent"], str):
            return "bad parent"
    return ""


def _apply(state: Board, rec: dict, snapshots: Dict[str, Board]) -> Board:
    """The board after ``rec``. Raises ValueError when the operation is impossible here."""
    kind = rec["kind"]
    nodes, links = state["nodes"], state["links"]
    if kind == "node.add":
        if rec["node"] in nodes:
            raise ValueError(f"node {rec['node']!r} already exists")
        nodes[rec["node"]] = dict(rec.get("data") or {})
    elif kind in ("node.edit", "node.move", "node.delete"):
        if rec["node"] not in nodes:
            raise ValueError(f"no node {rec['node']!r}")
        node = nodes[rec["node"]]
        if kind == "node.edit":
            for key, value in (rec.get("data") or {}).items():
                if value is None:
                    node.pop(key, None)
                else:
                    node[key] = value
        elif kind == "node.move":
            node["x"], node["y"] = rec["x"], rec["y"]
            if "parent" in rec:
                node["parent"] = rec["parent"]
        else:
            del nodes[rec["node"]]
            for lid in [lid for lid, ln in links.items() if rec["node"] in (ln["from"], ln["to"])]:
                del links[lid]
    elif kind == "link.add":
        if rec["link"] in links:
            raise ValueError(f"link {rec['link']!r} already exists")
        for end in ("from", "to"):
            if rec[end] not in nodes:
                raise ValueError(f"no node {rec[end]!r}")
        links[rec["link"]] = {"from": rec["from"], "to": rec["to"], **(rec.get("data") or {})}
    elif kind == "link.delete":
        if rec["link"] not in links:
            raise ValueError(f"no link {rec['link']!r}")
        del links[rec["link"]]
    elif kind == "restore":
        if rec["to"] not in snapshots:
            raise ValueError(f"no earlier operation {rec['to']!r}")
        state = copy.deepcopy(snapshots[rec["to"]])
    return state


def _parse(data: bytes, name: str) -> List[dict]:
    """Every record, checked for shape and unique ids. A torn last line is an error."""
    if data and not data.endswith(b"\n"):
        torn = data.count(b"\n") + 1
        raise BoardLogError(f"{name} line {torn}: the last line is incomplete")
    records, ids = [], set()
    for n, line in enumerate(data.split(b"\n")[:-1] if data else [], 1):
        try:
            rec = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BoardLogError(f"{name} line {n}: not a JSON object") from None
        why = _check_shape(rec)
        if why:
            raise BoardLogError(f"{name} line {n}: {why}")
        if rec["op"] in ids:
            raise BoardLogError(f"{name} line {n}: operation id {rec['op']!r} is used twice")
        ids.add(rec["op"])
        records.append(rec)
    return records


def _fold(records: List[dict], name: str, upto: Optional[str] = None) -> Board:
    """Replay ``records`` (through ``upto`` when given) into a board."""
    targets = {r["to"] for r in records if r["kind"] == "restore"}
    snapshots: Dict[str, Board] = {}
    state = _empty()
    for n, rec in enumerate(records, 1):
        try:
            state = _apply(state, rec, snapshots)
        except ValueError as exc:
            raise BoardLogError(f"{name} line {n}: {exc}") from None
        if rec["op"] in targets:
            snapshots[rec["op"]] = copy.deepcopy(state)
        if upto is not None and rec["op"] == upto:
            return state
    if upto is not None:
        raise KeyError(f"no operation {upto!r} in {name}")
    return state


class BoardLog:
    """A board's append-only edit log. Reads replay it; ``append`` adds one operation."""

    def __init__(self, board: str, home: str | Path | None = None):
        self.board = board
        self.path = board_path(board, home)
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    def ops(self) -> List[dict]:
        """Every operation, in order."""
        return _parse(self._read(), self.path.name)

    def replay(self, upto: Optional[str] = None) -> Board:
        """The board, or the board as of operation ``upto``."""
        return _fold(self.ops(), self.path.name, upto)

    def append(self, kind: str, *, op: Optional[str] = None, by: Optional[str] = None,
               **fields: Any) -> dict:
        """Write one operation and return the record written. An impossible operation, a
        duplicate id or an unreadable log raises, and nothing is written."""
        return self.append_many([{"kind": kind, "op": op, **fields}], by=by)[0]

    def append_many(self, ops: Sequence[Mapping[str, Any]], *,
                    by: Optional[str] = None) -> List[dict]:
        """Write every operation of one save in a single locked write with a single fsync.

        Each item is ``{"kind": ..., "op"?: ..., **fields}``. All or nothing: a malformed or
        impossible operation, or an id already in the log or used twice in ``ops``, raises
        and nothing is written. Returns the records written, in order."""
        recs = _records(ops, by)
        if not recs:
            return []
        with _locked(self.lock_path):
            self._commit(_parse(self._read(), self.path.name), recs)
        return recs

    def save(self, make_ops: Callable[[Board], Sequence[Mapping[str, Any]]], *,
             by: Optional[str] = None, expect_head: Any = _ANY_HEAD) -> Tuple[List[dict], Optional[str]]:
        """One save, all under the board's lock: check that the board is still at
        ``expect_head`` (the id of its last operation, or None for no board yet), compute the
        operations from the board as it is now with ``make_ops``, and write them in one append.
        A board that moved on raises BoardConflict and nothing is written. Returns the records
        written and the board's head after the save, as seen under the lock."""
        with _locked(self.lock_path):
            records = _parse(self._read(), self.path.name)
            head = content_head(records)
            if expect_head is not _ANY_HEAD and expect_head != head:
                raise BoardConflict(head)
            recs = _records(make_ops(_fold(records, self.path.name)), by)
            if recs:
                self._commit(records, recs)
            return recs, (recs[-1]["op"] if recs else head)

    def head(self) -> Optional[str]:
        """The board's version (see ``content_head``)."""
        return content_head(self.ops())

    def _commit(self, records: List[dict], recs: List[dict]) -> None:
        # Caller holds the lock.
        seen = {r["op"] for r in records}
        for rec in recs:
            if rec["op"] in seen:
                raise ValueError(f"operation id {rec['op']!r} is already in "
                                 f"{self.path.name} or used twice in this save")
            seen.add(rec["op"])
        try:
            _fold(records + recs, self.path.name)  # refuses an impossible operation
        except BoardLogError as exc:
            _fold(records, self.path.name)  # a broken log stays a BoardLogError
            raise ValueError(f"refused: {exc}") from None
        self._write_line(b"".join((json.dumps(r, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False) + "\n").encode("utf-8")
                                  for r in recs))

    def _write_line(self, line: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(str(self.path), flags, 0o600)
        try:
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)

    def restore(self, to: str, *, op: Optional[str] = None, by: Optional[str] = None) -> dict:
        """Append an operation that returns the board to how it was at ``to``."""
        return self.append("restore", op=op, by=by, to=to)

    def checkpoint(self, label: str, *, op: Optional[str] = None,
                   by: Optional[str] = None) -> dict:
        """Append a named point to restore to later."""
        return self.append("checkpoint", op=op, by=by, label=label)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the board's write lock (for sealing a stable prefix)."""
        with _locked(self.lock_path):
            yield
