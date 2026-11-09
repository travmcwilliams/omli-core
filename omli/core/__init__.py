"""Core wiring that depends on the generic primitives — no domain logic.

The Router resolves a pack's model selection against the ModelRegistry, validates it, and
fails loud. An Agent resolves its pack when it is constructed; `validate_packs` checks a set.
"""

from omli.core.router import ResolvedModel, Router, validate_packs

__all__ = ["Router", "ResolvedModel", "validate_packs"]
