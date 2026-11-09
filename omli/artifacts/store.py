"""ArtifactStore — append-only persistence and the single source of truth (Primitive 3).

Every state question ("is this done?", "did we process this already?") is answered
here, not from agent memory or a coordinator. The store has no update or delete
methods; writes are append-only and an id may be written exactly once: `write` refuses an id the
store already holds, and a log that already has records must be opened with `from_log`, which
loads its ids first (the constructor refuses a non-empty log).

Reads are served from secondary indexes (by kind, by predecessor -> successors, and the
set of source refs) built once at load and maintained on every write — so `by_kind`,
`successors`, `has_source`, and the `is_terminal_done` walk they back are index lookups,
not linear scans of the whole log. The indexes are derived state, never a second source of
truth: they are rebuilt verbatim from the append-only JSONL on `from_log`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, Optional

from omli.artifacts.schema import Artifact

# Serializes the JSONL append across threads so concurrent writers can't interleave partial lines into the shared log. The lock
# only orders writes; it never mutates. Module-level so every store shares one file guard.
_APPEND_LOCK = threading.Lock()


class ArtifactStore:
    """Append-only artifact store with an optional JSONL durability log.

    Deliberately missing: update(), delete(), set_status(). To change state, write a
    successor artifact (see `omli.artifacts.schema.succeed`).
    """

    def __init__(self, path: Optional[str | Path] = None, bus=None) -> None:
        """A new store. With `path`, the log must be missing or empty: a store built here starts
        with no records, so on a log that already holds some it could append an id the file
        already contains. Open an existing log with `ArtifactStore.from_log(path)` instead."""
        if path is not None:
            p = Path(path)
            if p.is_file() and p.stat().st_size > 0:
                raise ValueError(f"{p} already holds records; open it with "
                                 "ArtifactStore.from_log(path) so its ids are loaded first")
        self._setup(path, bus)

    def _setup(self, path: Optional[str | Path], bus) -> None:
        self._items: list[Artifact] = []
        self._by_id: dict[str, Artifact] = {}
        # Secondary indexes, maintained on every register (write + reload) so the state
        # questions below are answered by lookup, not by scanning the whole log. Each stays
        # in insertion order, so query results come back in the order the log holds them.
        self._by_kind: dict[str, list[Artifact]] = {}
        self._successors: dict[str, list[Artifact]] = {}  # predecessor id -> its successors
        self._source_refs: set[str] = set()               # every source ref ever recorded
        self._path = Path(path) if path is not None else None
        self._bus = bus
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Optional[Path]:
        """The durability log this store appends to (None for an in-memory store).
        Read-only: exposed so observers (e.g. the event sink) can co-locate their
        outputs beside the log, never so anyone can re-point the store."""
        return self._path

    def _register(self, artifact: Artifact) -> None:
        """Add an artifact to the in-memory items + primary and secondary indexes.

        The single point where index state is maintained, so `write` (a new artifact) and
        `from_log` (a reloaded one) can never drift out of sync. `write` checks that the id is
        unique and holds `_APPEND_LOCK` around this so concurrent writers can't race on the
        shared structures; `from_log` does not check, and trusts the log it reads.
        """
        self._items.append(artifact)
        self._by_id[artifact.id] = artifact
        self._by_kind.setdefault(artifact.kind, []).append(artifact)
        for predecessor_id in artifact.provenance.predecessor_ids:
            self._successors.setdefault(predecessor_id, []).append(artifact)
        self._source_refs.update(artifact.provenance.source_refs)

    # -- reload --------------------------------------------------------------

    @classmethod
    def from_log(cls, path: str | Path, bus=None) -> "ArtifactStore":
        """Open a store backed by `path`, rehydrating any artifacts already logged.

        Rebuilds in-memory state from the append-only JSONL so cross-run questions
        ("did we already process this source?") are answerable. Existing records are
        loaded directly (not re-written and not re-emitted on the bus) — append-only
        semantics are preserved because we are reading records that were already
        appended. New writes after load continue to append to the same file.
        """
        store = cls.__new__(cls)
        store._setup(path, bus)
        log = Path(path)
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                store._register(Artifact.from_dict(json.loads(line)))
        return store

    # -- writes --------------------------------------------------------------

    def write(self, artifact: Artifact) -> Artifact:
        """Append an artifact. Raises if its id was already written."""
        # One lock covers the duplicate check, the in-memory index update, AND the file
        # append, so concurrent writers can neither interleave partial
        # lines nor race on the shared in-memory indexes.
        with _APPEND_LOCK:
            if artifact.id in self._by_id:
                raise ValueError(
                    f"artifact {artifact.id} already written; artifacts are append-only"
                )
            self._register(artifact)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(artifact.to_dict()) + "\n")
        if self._bus is not None:
            self._bus.emit(
                "artifact_written",
                {"id": artifact.id, "kind": artifact.kind},
            )
        return artifact

    # -- reads (answer every state question from here) -----------------------

    def get(self, artifact_id: str) -> Optional[Artifact]:
        return self._by_id.get(artifact_id)

    def all(self) -> tuple[Artifact, ...]:
        return tuple(self._items)

    def by_kind(self, kind: str) -> tuple[Artifact, ...]:
        return tuple(self._by_kind.get(kind, ()))

    def successors(self, artifact_id: str) -> tuple[Artifact, ...]:
        """Artifacts that link back to the given id via provenance."""
        return tuple(self._successors.get(artifact_id, ()))

    def has_source(self, source_ref: str) -> bool:
        """Dedup helper: has any artifact already been derived from this source?"""
        return source_ref in self._source_refs

    def is_terminal_done(self, artifact_id: str, terminal_kind: str = "OutcomeRecord") -> bool:
        """Has the chain rooted at `artifact_id` reached a terminal artifact?

        Walks forward through successors; state lives entirely in the store.
        """
        frontier: list[str] = [artifact_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for succ in self.successors(current):
                if succ.kind == terminal_kind:
                    return True
                frontier.append(succ.id)
        return False

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterable[Artifact]:
        return iter(tuple(self._items))
