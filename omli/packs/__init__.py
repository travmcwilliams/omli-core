"""Config packs — where ALL domain logic lives (Primitive 1).

A pack is immutable per version. Agents apply a pack but never modify it. Adding a
domain means adding or extending a pack, never editing core code.
"""

from omli.packs.schema import (
    Escalation,
    ModelRequirements,
    ModelSelection,
    Pack,
)
from omli.packs.loader import load_pack

__all__ = [
    "Pack",
    "ModelSelection",
    "ModelRequirements",
    "Escalation",
    "load_pack",
]
