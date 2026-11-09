"""SqliteArtifactIndex — a disk-persistent indexed read path over the artifact log.

An indexed query path, so reads need not scan the whole log. `ArtifactStore.from_log` reads
the whole JSONL each time it is called; this index persists its projection to disk and, on
reopen, reconciles only the appended tail. These tests pin the contract: its answers are
IDENTICAL to an ArtifactStore over the same log (membership AND order), a reopen ingests
only new lines (not the whole log), and the projection is disposable — deleting it and
rebuilding yields the same answers.
"""

from __future__ import annotations

import threading

from omli.artifacts import ArtifactStore, SqliteArtifactIndex, index_path_for
from omli.artifacts.provenance import Provenance
from omli.artifacts.schema import new_artifact, succeed


def _ci(project: str):
    return new_artifact(
        "CanonicalInput",
        {"project": project},
        Provenance(agent="Intake", pack_name="intake", pack_version="0.1.0",
                   source_refs=(f"file:///code/{project}",)),
    )


def _step(pred, kind: str):
    return succeed(pred, kind, {"text": f"{kind} of {pred.id[:6]}"},
                   agent="Agent", pack_name="p", pack_version="0.1.0")


def _seed_full_chain(store, project: str):
    ci = store.write(_ci(project))
    cur = ci
    for kind in ("Spec", "DecisionRecord", "WorkResult", "OutcomeRecord"):
        cur = store.write(_step(cur, kind))
    return ci


def test_index_matches_store_including_order(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    roots = [_seed_full_chain(store, p) for p in ("a", "b", "c")]

    index = SqliteArtifactIndex.for_log(path)
    try:
        assert len(index) == len(store)
        for kind in ("CanonicalInput", "Spec", "DecisionRecord", "WorkResult", "OutcomeRecord"):
            # membership AND insertion order identical to the store's indexed read
            assert index.by_kind(kind) == store.by_kind(kind)
            assert index.ids_by_kind(kind) == tuple(a.id for a in store.by_kind(kind))

        for a in store.all():
            assert index.get(a.id) == a
            assert index.successors(a.id) == store.successors(a.id)

        for p in ("a", "b", "c"):
            assert index.has_source(f"file:///code/{p}") is True
        assert index.has_source("file:///code/nope") is False

        for root in roots:
            assert index.is_terminal_done(root.id) is True
    finally:
        index.close()


def test_unknown_ids_and_kinds_are_empty(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    store.write(_ci("a"))
    index = SqliteArtifactIndex.for_log(path)
    try:
        assert index.get("does-not-exist") is None
        assert index.successors("does-not-exist") == ()
        assert index.by_kind("Spec") == ()
        assert index.ids_by_kind("Spec") == ()
    finally:
        index.close()


def test_reopen_reconciles_only_the_appended_tail(tmp_path):
    """A reconcile ingests only the lines written since the last reconcile."""
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    _seed_full_chain(store, "first")  # 5 lines

    index = SqliteArtifactIndex.for_log(path)
    assert len(index) == 5
    # Nothing new -> zero work.
    assert index.reconcile() == 0

    store.write(_ci("second"))  # append one line
    assert index.reconcile() == 1  # ONLY the new line, not a full re-read of all 6
    assert len(index) == 6
    assert index.has_source("file:///code/second") is True
    index.close()


def test_persisted_index_survives_reopen_without_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    root = _seed_full_chain(store, "proj")

    first = SqliteArtifactIndex.for_log(path)
    first.close()
    # Same sidecar file on disk; a second open finds it already populated and only
    # reconciles the (empty) tail. Reading any log line on reopen would be a rebuild, so the
    # line reader is replaced by one that fails the test.
    assert index_path_for(path).exists()

    def no_rebuild(self, raw_lines):
        raise AssertionError("the reopened index re-read log lines")

    monkeypatch.setattr(SqliteArtifactIndex, "_add_lines", no_rebuild)
    reopened = SqliteArtifactIndex.for_log(path)
    try:
        assert reopened.reconcile() == 0
        assert reopened.is_terminal_done(root.id) is True
        assert len(reopened.by_kind("OutcomeRecord")) == 1
    finally:
        reopened.close()


def test_index_is_disposable_delete_and_rebuild(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    _seed_full_chain(store, "proj")

    index = SqliteArtifactIndex.for_log(path)
    before = index.by_kind("CanonicalInput")
    index.close()
    index_path_for(path).unlink()  # delete the derived projection

    rebuilt = SqliteArtifactIndex.for_log(path)  # rebuilds from the log alone
    try:
        assert rebuilt.by_kind("CanonicalInput") == before
        assert len(rebuilt) == 5
    finally:
        rebuilt.close()


def test_missing_log_is_empty(tmp_path):
    path = tmp_path / "artifacts.jsonl"  # never written
    index = SqliteArtifactIndex.for_log(path)
    try:
        assert len(index) == 0
        assert index.by_kind("CanonicalInput") == ()
        assert index.has_source("anything") is False
    finally:
        index.close()


def test_multiple_predecessors_indexed_under_each(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    p1 = store.write(_ci("a"))
    p2 = store.write(_ci("b"))
    merged = new_artifact(
        "Spec",
        {"text": "merges two roots"},
        Provenance(agent="Agent", pack_name="p", pack_version="0.1.0",
                   predecessor_ids=(p1.id, p2.id)),
    )
    store.write(merged)

    index = SqliteArtifactIndex.for_log(path)
    try:
        assert index.successor_ids(p1.id) == (merged.id,)
        assert index.successor_ids(p2.id) == (merged.id,)
        assert index.successors(p1.id) == store.successors(p1.id)
    finally:
        index.close()


def test_partial_trailing_line_is_deferred(tmp_path):
    """A half-written final line (no terminating newline) is not ingested; the complete line
    before it is."""
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    a = store.write(_ci("done"))

    # Simulate a write in flight: append a line with no terminating newline.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "partial", "kind": "CanonicalInput"')  # truncated JSON, no \n

    index = SqliteArtifactIndex.for_log(path)
    try:
        assert len(index) == 1  # only the complete line
        assert index.get(a.id) == a
        assert index.get("partial") is None
    finally:
        index.close()


def test_reconcile_is_threadsafe(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    for i in range(50):
        store.write(_ci(f"p{i}"))

    index = SqliteArtifactIndex.for_log(path)
    results, errors = [], []

    def reader():
        # An assert inside a thread cannot fail the test, so record what happened instead.
        try:
            index.reconcile()
            results.append(len(index.by_kind("CanonicalInput")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    index.close()
    assert errors == []
    assert results == [50] * 8
