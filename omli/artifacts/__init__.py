"""The artifact chain — OMLi's state machine (Primitives 3 & 4).

Artifacts are immutable and append-only. To change something, write a linked
successor; never mutate in place.
"""

from omli.artifacts.provenance import ModelRef, Provenance
from omli.artifacts.schema import (
    ARTIFACT_KINDS,
    Artifact,
    new_artifact,
    succeed,
)
from omli.artifacts.sqlite_index import SqliteArtifactIndex, index_path_for
from omli.artifacts.store import ArtifactStore

__all__ = [
    "ARTIFACT_KINDS",
    "Artifact",
    "Provenance",
    "ModelRef",
    "ArtifactStore",
    "SqliteArtifactIndex",
    "index_path_for",
    "new_artifact",
    "succeed",
]
