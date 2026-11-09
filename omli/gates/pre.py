"""Pre-call gate: deterministic checks BEFORE a model runs (Primitive 2).

Two generic checks: an empty payload is out of scope, and a source already seen is a
duplicate. Any domain threshold or vertical rule belongs in a pack, not here. It does not
assign sensitivity tags; route control reads the tags already on the artifact's provenance.
"""

from __future__ import annotations

from typing import Iterable, Optional

from omli.artifacts.schema import Artifact
from omli.gates.result import GateResult


class PreGate:
    """Decides whether the model is allowed to run on an input artifact."""

    def check(
        self,
        artifact: Artifact,
        *,
        seen_source_refs: Optional[Iterable[str]] = None,
    ) -> GateResult:
        # Scope: an empty payload is out of scope — never call the model on nothing.
        if not artifact.payload:
            return GateResult(False, reason="empty payload", tags=("out_of_scope",))

        # Dedup: if we have already processed this source, skip the model call.
        seen = set(seen_source_refs or ())
        if seen and any(ref in seen for ref in artifact.provenance.source_refs):
            return GateResult(False, reason="duplicate source", tags=("dedup",))

        return GateResult(True, tags=("scoped",))
