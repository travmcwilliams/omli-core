"""Repair a board log whose last line was half-written by a crash. Run by a person, never
automatically: nothing in OMLi calls this except ``python -m omli.boards repair-tail``.

It handles exactly one case: an unterminated final line that lies outside the sealed part of
the log. The torn bytes are first written to a new side file beside the log
(``<board>.torn-<UTC time>-<random>.bin``, never overwriting one), then cut from the end of the
log. Every complete line stays byte for byte as it was.

Anything else is refused and nothing changes: a log whose last line is complete, a torn line
the seal covers, or a sealed part that no longer verifies (changed, cut short or unreadable).
Those keep failing ``python -m omli.integrity verify``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from omli.boards.log import BoardLog
from omli.integrity.ledger import split_records
from omli.integrity.verifier import verify


class RepairRefused(ValueError):
    """The log is not in the one state this repair handles; nothing was changed."""


@dataclass(frozen=True)
class RepairResult:
    log: Path
    torn_bytes: int               # 0 when there was nothing to repair
    side_file: Optional[Path]     # where the torn bytes were kept


def _fsync_write_new(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def repair_tail(board: str, home: str | Path | None = None) -> RepairResult:
    """Move an unterminated final line outside the sealed part into a side file."""
    log = BoardLog(board, home)
    with log.locked():
        try:
            data = log.path.read_bytes()
        except FileNotFoundError:
            raise RepairRefused(f"there is no board log {log.path.name}") from None
        if not data or data.endswith(b"\n"):
            return RepairResult(log.path, 0, None)

        records = split_records(data)
        torn = records[-1]
        torn_index = len(records) - 1
        report = verify(log.path)
        if report.status != "unsealed":
            if not report.trustworthy:
                raise RepairRefused(
                    f"the sealed part of {log.path.name} does not verify ({report.status}); "
                    "a sealed line is not repaired")
            if torn_index < report.sealed_count:
                raise RepairRefused(
                    f"the incomplete line {torn_index + 1} is inside the sealed part of "
                    f"{log.path.name}; a sealed line is not repaired")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        side = log.path.with_name(f"{board}.torn-{stamp}-{secrets.token_hex(3)}.bin")
        _fsync_write_new(side, torn)
        if side.read_bytes() != torn:
            raise RepairRefused(f"{side.name} did not read back; the log was not changed")
        keep = len(data) - len(torn)
        with open(log.path, "r+b") as fh:
            fh.truncate(keep)
            fh.flush()
            os.fsync(fh.fileno())
        return RepairResult(log.path, len(torn), side)
