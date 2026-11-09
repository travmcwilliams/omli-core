"""Concurrent-append safety for the artifact store.

The append is guarded by a module-level lock so parallel workers writing one shared JSONL log
can't interleave partial lines. The store is strictly append-only: the lock only serializes
writes, it never mutates.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from omli.artifacts import ArtifactStore, Provenance, new_artifact


def _prov():
    return Provenance(agent="TestAgent", pack_name="example", pack_version="0.1.0")


def test_parallel_writes_produce_intact_jsonl(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore(path=path)
    arts = [new_artifact("CanonicalInput", {"i": i}, _prov()) for i in range(200)]

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(store.write, arts))

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 200                       # no dropped writes
    ids = set()
    for ln in lines:
        rec = json.loads(ln)                       # every line is complete, valid JSON
        ids.add(rec["id"])
    assert ids == {a.id for a in arts}             # no interleaving / corruption
