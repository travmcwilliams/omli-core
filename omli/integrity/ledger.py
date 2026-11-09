"""Hash-chain primitives over the artifact log (the at-rest half of Primitive 4).

In-process, immutability is structural: artifacts are frozen and `ArtifactStore.write`
rejects a re-used id. On disk, `var/artifacts.jsonl` is a plain file that can be hand-edited.

These are the pure functions that make such an edit detectable. The log is split into
physical records at each ``\\n`` byte, and each record is hashed as its exact bytes, terminator
included. Every byte of the file is therefore inside the chain: a changed line ending
(``\\n`` to ``\\r\\n``), an inserted blank line, a form feed, a byte order mark or trailing
spaces all change the chain. Each record commits to the hash of the record before it, so
editing, reordering, inserting or deleting any record changes every hash after it. stdlib
only; no domain knowledge.

Trust model, stated plainly:

* The chain alone is corruption detection, NOT tamper-evidence. The seal sidecar sits beside
  the log, and an actor who can edit the log can also re-seal it.
* A seal covers the records that existed when it was made (the sealed prefix). Records added
  after it are reported as ``appended``; nothing vouches for them until the next seal.
* Tamper-evidence comes from the layers above: `anchor.py` (git-anchored heads), which
  catches a rollback only when the anchor log is in use and pinned, and `signer.py`
  (ed25519 head signature), which a re-sealer cannot forge only while the keyring and the
  public key are pinned where that re-sealer cannot change them.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import List, Sequence, Union

# The chain format. It is part of every seal and every signed message, so a seal written
# by any other format never verifies as ok.
ALGO = "sha256-raw-records-v2"
# Head of an empty log. A record's hash folds in the previous head, seeding from this.
GENESIS = ""

Record = Union[str, bytes]


def _as_bytes(record: Record) -> bytes:
    return record if isinstance(record, bytes) else record.encode("utf-8")


def link(prev_hash: str, record: Record) -> str:
    """The chain step: commit to the previous head AND this record's exact bytes."""
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\n")
    h.update(_as_bytes(record))
    return h.hexdigest()


def compute_chain(records: Sequence[Record]) -> List[str]:
    """Rolling cumulative hashes: `chain[i]` commits to records `0..i`."""
    chain: List[str] = []
    prev = GENESIS
    for record in records:
        prev = link(prev, record)
        chain.append(prev)
    return chain


def head(chain: Sequence[str]) -> str:
    """The single value that fingerprints the whole log (GENESIS when empty)."""
    return chain[-1] if chain else GENESIS


def content_id(line: Record) -> str:
    """A stable per-line content id (sha256 of the exact stored line).

    Independent of chain position — the natural key for caching anything derived from an
    artifact.
    """
    return hashlib.sha256(_as_bytes(line)).hexdigest()


def split_records(data: bytes) -> List[bytes]:
    """Split raw log bytes at each ``\\n``, keeping the terminator on every record.

    Nothing is dropped or normalised: blank lines are records, ``\\r`` stays inside its
    record, and a final record without a terminator is kept as it is. Joining the result
    gives back ``data`` exactly.
    """
    records: List[bytes] = []
    start = 0
    while start < len(data):
        end = data.find(b"\n", start)
        if end < 0:
            records.append(data[start:])
            break
        records.append(data[start:end + 1])
        start = end + 1
    return records


def read_log_records(path: str | Path) -> List[bytes]:
    """The log's physical records as exact bytes; this is what the chain hashes."""
    p = Path(path)
    if not p.exists():
        return []
    return split_records(p.read_bytes())


def read_log_lines(path: str | Path) -> List[str]:
    """The log's non-empty lines as text (matches `ArtifactStore.from_log`).

    For parsing only. The chain never hashes these, because this view drops blank lines
    and line endings; use `read_log_records` for anything integrity-related.
    """
    p = Path(path)
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write `text` to `path` atomically: fill a sibling ``.tmp`` then ``os.replace`` it
    into place. ``os.replace`` is atomic on the same volume (POSIX and Windows), so a
    reader never sees a torn file and a crash mid-write leaves the ``.tmp``, never a
    half-written target. Integrity outputs (the seal and signature sidecars) must never be
    partial — a truncated seal or signature would fail to parse and take verification down.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
