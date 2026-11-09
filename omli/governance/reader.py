"""Governance reader — read-only cross-artifact auditing (Primitive 5).

Reconstructs lineage and tallies the chain straight from the artifact store, proving
Primitive 3: every state question is answerable from artifacts alone.
"""

from __future__ import annotations

from typing import Dict, List

from omli.artifacts.schema import Artifact
from omli.artifacts.store import ArtifactStore


class Governance:
    """Reads across the store to trace lineage and audit; never writes."""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def trace(self, artifact_id: str) -> List[Artifact]:
        """Return the lineage from the given artifact back to its roots."""
        lineage: List[Artifact] = []
        frontier = [artifact_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            artifact = self._store.get(current)
            if artifact is None:
                continue
            lineage.append(artifact)
            frontier.extend(artifact.provenance.predecessor_ids)
        return lineage

    def audit(self) -> Dict[str, int]:
        """Count artifacts by kind — a cheap health view of the whole store."""
        counts: Dict[str, int] = {}
        for artifact in self._store.all():
            counts[artifact.kind] = counts.get(artifact.kind, 0) + 1
        return counts
