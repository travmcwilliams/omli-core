"""Governance — the only cross-agent reader (Primitive 5).

By design no agent reads another agent's memory; nothing in the code enforces that, so it
holds only as long as callers keep to it. Governance is the intended cross-reader of the
artifact store, and it only reads (audit/trace), never writes.
"""

from omli.governance.reader import Governance
from omli.governance.findings import (
    Finding,
    FindingStore,
    finding_from_evaluation,
    finding_from_shadow,
)
from omli.governance.promotion import propose_promotion

__all__ = [
    "Governance",
    "Finding",
    "FindingStore",
    "finding_from_evaluation",
    "finding_from_shadow",
    "propose_promotion",
]
