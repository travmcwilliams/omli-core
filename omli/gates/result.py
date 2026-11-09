"""Shared result type for deterministic gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class GateResult:
    """Outcome of a deterministic gate check.

    `passed` is the only control-flow signal. `tags` carries cheap metadata: the gates in this
    package set routing hints such as `escape_hatch`, `schema_violation`, `below_threshold`
    and `validated`.
    """

    passed: bool
    reason: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
