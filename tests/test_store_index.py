"""ArtifactStore secondary indexes give the same answers as a linear scan.

The store serves by_kind / successors / has_source (and therefore is_terminal_done) from
indexes maintained on write and rebuilt on reload. These tests pin correctness, not speed:
the indexed answers are IDENTICAL (same membership AND same insertion order) to a naive scan,
survive a reload, keep append-only semantics, and are safe under concurrent writers.
"""

from __future__ import annotations

import threading

from omli.artifacts import ArtifactStore
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


# --- naive references (a linear scan over every stored artifact) ------------- #
def _scan_by_kind(store, kind):
    return tuple(a for a in store.all() if a.kind == kind)


def _scan_successors(store, artifact_id):
    return tuple(a for a in store.all() if artifact_id in a.provenance.predecessor_ids)


def _scan_has_source(store, ref):
    return any(ref in a.provenance.source_refs for a in store.all())


def test_indexed_reads_match_linear_scan_including_order():
    store = ArtifactStore()
    roots = [_seed_full_chain(store, p) for p in ("a", "b", "c")]

    for kind in ("CanonicalInput", "Spec", "DecisionRecord", "WorkResult", "OutcomeRecord"):
        indexed = store.by_kind(kind)
        assert indexed == _scan_by_kind(store, kind)          # membership + order identical
        assert len(indexed) == 3

    for a in store.all():
        assert store.successors(a.id) == _scan_successors(store, a.id)

    for p in ("a", "b", "c"):
        assert store.has_source(f"file:///code/{p}") is True
    assert store.has_source("file:///code/nope") is False

    for root in roots:
        assert store.is_terminal_done(root.id) is True


def test_by_kind_is_insertion_ordered():
    store = ArtifactStore()
    a = store.write(_ci("a"))
    b = store.write(_ci("b"))
    c = store.write(_ci("c"))
    assert store.by_kind("CanonicalInput") == (a, b, c)
    assert store.by_kind("Spec") == ()  # empty kind -> empty tuple, never a KeyError


def test_unknown_ids_return_empty():
    store = ArtifactStore()
    store.write(_ci("a"))
    assert store.successors("does-not-exist") == ()
    assert store.by_kind("Nonexistent") == ()


def test_indexes_rebuild_from_log(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(path)
    root = _seed_full_chain(store, "proj")

    reloaded = ArtifactStore.from_log(path)
    # Every index is reconstructed from the append-only JSONL alone.
    assert len(reloaded.by_kind("OutcomeRecord")) == 1
    assert reloaded.by_kind("Spec") == _scan_by_kind(reloaded, "Spec")
    assert reloaded.successors(root.id) == _scan_successors(reloaded, root.id)
    assert reloaded.has_source("file:///code/proj") is True
    assert reloaded.is_terminal_done(root.id) is True


def test_rewriting_an_id_raises_and_leaves_no_duplicate():
    store = ArtifactStore()
    ci = store.write(_ci("a"))
    try:
        store.write(ci)  # same id again
    except ValueError as exc:
        assert "append-only" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("re-writing an id must raise")
    # the failed re-write left no duplicate in any index
    assert len(store.by_kind("CanonicalInput")) == 1


def test_multiple_predecessors_indexed_under_each():
    """An artifact linking to two predecessors is a successor of both (index parity)."""
    store = ArtifactStore()
    p1 = store.write(_ci("a"))
    p2 = store.write(_ci("b"))
    merged = new_artifact(
        "Spec",
        {"text": "merges two roots"},
        Provenance(agent="Agent", pack_name="p", pack_version="0.1.0",
                   predecessor_ids=(p1.id, p2.id)),
    )
    store.write(merged)
    assert merged in store.successors(p1.id)
    assert merged in store.successors(p2.id)
    assert store.successors(p1.id) == _scan_successors(store, p1.id)


def test_concurrent_writes_are_consistent():
    """Under threads, every write lands exactly once in items and in each index (by kind,
    successors and source), with no race."""
    store = ArtifactStore()
    n = 200
    roots = {}

    def worker(i):
        root = store.write(_ci(f"p{i}"))
        roots[i] = root
        store.write(_step(root, "Spec"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store) == 2 * n
    # The by-kind index agrees with the items on membership (order may interleave).
    for kind in ("CanonicalInput", "Spec"):
        assert len(store.by_kind(kind)) == n
        assert {a.id for a in store.by_kind(kind)} == {a.id for a in _scan_by_kind(store, kind)}
    for i, root in roots.items():
        # The successor index holds exactly the one Spec written after each root.
        assert store.successors(root.id) == _scan_successors(store, root.id)
        assert len(store.successors(root.id)) == 1
        # The source index holds every root's source.
        assert store.has_source(f"file:///code/p{i}") is True
    assert store.has_source("file:///code/never-written") is False
