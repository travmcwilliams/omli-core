"""SqliteArtifactIndex — a disk-persistent, indexed read path over the artifact log.

The in-memory :class:`~omli.artifacts.store.ArtifactStore` answers point queries from
secondary indexes, but only *within* a loaded process, and `from_log` pays a full read +
JSON parse of the entire log on every call. A short-lived process that needs one point
query (``has_source`` dedup, ``by_kind("CanonicalInput")``) would therefore read the whole
log. With this index, point and by-kind queries are served from a persisted SQLite projection without materializing
every Artifact, and reopening only reconciles the *appended* tail (O(new-lines), not O(n)).

It is a **derived projection of the log, never a second source of truth** (Primitive 3):
the append-only JSONL stays canonical, every row is rebuilt verbatim from it, and the index
can be deleted and rebuilt at any time. It maintains no state the log does not already
imply and it is **not** part of Primitive 4's on-disk integrity half (see `omli/integrity/`)
— it is a query accelerator, not evidence.

Uses the stdlib: ``sqlite3`` only, with a shared connection serialized through a lock (callers
may run in a threadpool). Reconcile relies on the append-only invariant: the byte length already
consumed is recorded, so a normal reopen reads only the bytes written since. It compares sizes
only, so a rewrite that leaves the log the same length or longer goes unnoticed and the index can
then serve stale rows. Use `omli.integrity` to check the log, and delete the index after a rewrite.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from omli.artifacts.schema import Artifact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id    TEXT PRIMARY KEY,
    kind  TEXT NOT NULL,
    line  TEXT NOT NULL            -- the exact JSONL line, so get() rehydrates the full Artifact
);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
CREATE TABLE IF NOT EXISTS sources (
    source_ref  TEXT NOT NULL,
    artifact_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_ref ON sources(source_ref);
CREATE TABLE IF NOT EXISTS edges (
    predecessor_id TEXT NOT NULL,  -- provenance predecessor
    successor_id   TEXT NOT NULL   -- the artifact that links back to it
);
CREATE INDEX IF NOT EXISTS idx_edges_pred ON edges(predecessor_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def index_path_for(log_path: str | Path) -> Path:
    """The sidecar index location for a given artifact log.

    Co-located with the log; ``.index.sqlite`` is appended to its full name, so
    ``var/artifacts.jsonl`` -> ``var/artifacts.jsonl.index.sqlite``.
    """
    p = Path(log_path)
    return p.with_name(p.name + ".index.sqlite")


class SqliteArtifactIndex:
    """Indexed, disk-persistent read projection of an append-only artifact log.

    Read-only over the chain: it never appends to the log and exposes no artifact writer —
    the store remains the sole writer. For a log the store wrote (every id unique, because the
    store refuses a duplicate on append), query results (order and objects) are identical to
    the equivalent :class:`ArtifactStore` reads; only the cost model differs. A log edited by
    hand to repeat an id is the exception: the index keeps the first record with that id,
    while the store's reload keeps both.
    """

    def __init__(self, log_path: str | Path, index_path: Optional[str | Path] = None) -> None:
        self._log = Path(log_path)
        self._path = Path(index_path) if index_path is not None else index_path_for(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Shared connection serialized through _lock: callers may run in a threadpool, so the
        # same index instance may be touched from several threads.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.reconcile()

    # -- reconciliation ------------------------------------------------------

    def _get_consumed(self) -> int:
        row = self._conn.execute("SELECT v FROM meta WHERE k = 'consumed_bytes'").fetchone()
        return int(row["v"]) if row is not None else 0

    def _set_consumed(self, value: int) -> None:
        self._conn.execute(
            "INSERT INTO meta (k, v) VALUES ('consumed_bytes', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (str(value),),
        )

    def _wipe(self) -> None:
        for table in ("artifacts", "sources", "edges"):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.execute("DELETE FROM meta WHERE k = 'consumed_bytes'")

    def reconcile(self) -> int:
        """Bring the index up to date with the log; return the number of lines added.

        Cheap by construction: because the log is append-only, only the bytes written since
        the last reconcile are read. If the log shrank or is missing (a rewrite, which the
        append-only store never does), the projection is wiped and rebuilt from scratch. A
        rewrite that does not shrink the log is not detected here.
        """
        with self._lock:
            if not self._log.exists():
                if self._get_consumed() != 0:
                    self._wipe()
                    self._conn.commit()
                return 0
            size = self._log.stat().st_size
            consumed = self._get_consumed()
            if size < consumed:
                # Truncation / rewrite: the append-only invariant was broken upstream —
                # rebuild rather than trust a divergent offset.
                self._wipe()
                consumed = 0
            elif size == consumed:
                return 0
            with self._log.open("rb") as fh:
                fh.seek(consumed)
                chunk = fh.read()
            # Only whole, newline-terminated lines are durable; a trailing partial line (a
            # write still in flight) is left for the next reconcile.
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                return 0
            complete = chunk[: last_nl + 1]
            added = self._add_lines(complete.split(b"\n"))
            self._set_consumed(consumed + len(complete))
            self._conn.commit()
            return added

    def _add_lines(self, raw_lines: Iterable[bytes]) -> int:
        count = 0
        for raw in raw_lines:
            if not raw.strip():
                continue
            data = json.loads(raw)
            artifact_id = data["id"]
            # INSERT OR IGNORE: a derived read index tolerates a duplicate id (first wins),
            # matching the store's guarantee that an id is written at most once.
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO artifacts (id, kind, line) VALUES (?, ?, ?)",
                (artifact_id, data["kind"], raw.decode("utf-8")),
            )
            if cur.rowcount == 0:
                continue
            prov = data.get("provenance", {})
            for source_ref in prov.get("source_refs", ()):
                self._conn.execute(
                    "INSERT INTO sources (source_ref, artifact_id) VALUES (?, ?)",
                    (source_ref, artifact_id),
                )
            for predecessor_id in prov.get("predecessor_ids", ()):
                self._conn.execute(
                    "INSERT INTO edges (predecessor_id, successor_id) VALUES (?, ?)",
                    (predecessor_id, artifact_id),
                )
            count += 1
        return count

    # -- reads (mirror ArtifactStore, served from disk) ----------------------

    def get(self, artifact_id: str) -> Optional[Artifact]:
        with self._lock:
            row = self._conn.execute(
                "SELECT line FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return Artifact.from_dict(json.loads(row["line"])) if row is not None else None

    def has_source(self, source_ref: str) -> bool:
        """Dedup helper: has any artifact already been derived from this source?"""
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM sources WHERE source_ref = ? LIMIT 1", (source_ref,)
            ).fetchone() is not None

    def ids_by_kind(self, kind: str) -> tuple[str, ...]:
        """Artifact ids of a kind, in append (rowid) order — no rehydration."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM artifacts WHERE kind = ? ORDER BY rowid", (kind,)
            ).fetchall()
        return tuple(r["id"] for r in rows)

    def by_kind(self, kind: str) -> tuple[Artifact, ...]:
        """Full artifacts of a kind, in append order (identical to ArtifactStore.by_kind)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT line FROM artifacts WHERE kind = ? ORDER BY rowid", (kind,)
            ).fetchall()
        return tuple(Artifact.from_dict(json.loads(r["line"])) for r in rows)

    def successor_ids(self, artifact_id: str) -> tuple[str, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT successor_id FROM edges WHERE predecessor_id = ? ORDER BY rowid",
                (artifact_id,),
            ).fetchall()
        return tuple(r["successor_id"] for r in rows)

    def successors(self, artifact_id: str) -> tuple[Artifact, ...]:
        """Artifacts that link back to the given id via provenance (append order)."""
        return tuple(a for a in (self.get(i) for i in self.successor_ids(artifact_id)) if a)

    def _kind_of(self, artifact_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return row["kind"] if row is not None else None

    def is_terminal_done(self, artifact_id: str, terminal_kind: str = "OutcomeRecord") -> bool:
        """Has the chain rooted at `artifact_id` reached a terminal artifact?

        Walks forward through the edge index; the answer lives entirely in the projection,
        which lives entirely in the log.
        """
        frontier: list[str] = [artifact_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for succ_id in self.successor_ids(current):
                if self._kind_of(succ_id) == terminal_kind:
                    return True
                frontier.append(succ_id)
        return False

    def __len__(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Release the file handle (on Windows an open file cannot be deleted)."""
        with self._lock:
            self._conn.close()

    @classmethod
    def for_log(cls, log_path: str | Path) -> "SqliteArtifactIndex":
        """Open (creating/reconciling as needed) the index sidecar for an artifact log."""
        return cls(log_path)
