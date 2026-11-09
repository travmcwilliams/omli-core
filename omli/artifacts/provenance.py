"""Provenance — every artifact records where it came from (Primitive 4).

No artifact may be written without provenance: which agent produced it, under which
pack version, from which predecessors and source references.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class ModelRef:
    """Model identity stamped into provenance.

    Carries enough to detect drift: the pinned identity from the registry plus `served_model_id` (what the endpoint actually served).
    """

    model_id: str
    registry_version: str
    pin: str
    quant: Optional[str]
    reproducible: bool
    served_model_id: str


@dataclass(frozen=True)
class Provenance:
    """Immutable record of an artifact's origin.

    Attributes:
        agent: Class name of the producing agent (identity is pack-defined elsewhere).
        pack_name: Name of the pack applied to produce this artifact.
        pack_version: Version of that pack (packs are immutable per version).
        predecessor_ids: IDs of the artifacts this one was derived from.
        source_refs: External source references (URLs, file paths, record keys).
    """

    agent: str
    pack_name: str
    pack_version: str
    predecessor_ids: Tuple[str, ...] = field(default_factory=tuple)
    source_refs: Tuple[str, ...] = field(default_factory=tuple)
    # Sensitivity classification, ACCUMULATE-ONLY down the chain. `succeed()` unions a
    # successor's tags with its predecessor's, as it carries source_refs forward, so a
    # narrowing classification is not expressible through the chain API. Tags are
    # normalized to lowercase and an unknown tag raises here, so a typo cannot fail open.
    #
    # This carries the classification; it does not enforce routes itself. Enforcement lives
    # at the boundaries (the agent's local-only check, the connection scope gate), which
    # read these tags.
    sensitivity: Tuple[str, ...] = field(default_factory=tuple)
    model_ref: Optional[ModelRef] = None  # stamped when a model produced this artifact
    # Stamped when an EXTERNAL action was authorized by a Connection Registry (additive,
    # optional). Lets an external change be traced back to a specific selected checklist row
    # in a specific registry version. Nothing in this package sets these fields; a caller
    # that performs an external action is expected to fill them.
    connection_id: Optional[str] = None
    connection_version: Optional[str] = None
    selected_resource_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("provenance requires a producing agent")
        if not self.pack_name or not self.pack_version:
            raise ValueError("provenance requires a pack name and version")
        # Normalize to tuples so the record is hashable and cannot be mutated.
        object.__setattr__(self, "predecessor_ids", tuple(self.predecessor_ids))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        # Sensitivity may arrive as a bare string (call-site convenience) or a list;
        # always store first-seen, deduped tuple so succeed() union never char-splits a str.
        from omli.sensitivity import validate_sensitivity

        object.__setattr__(self, "sensitivity", validate_sensitivity(self.sensitivity))
