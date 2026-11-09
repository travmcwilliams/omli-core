"""Post-call gate: deterministic checks AFTER a model runs (Primitive 2).

Schema validation, threshold checks, escape-hatch routing. When the Agent runs a model, its
raw output is never written as an artifact without passing here first.

A named score must be a finite number that fits in a float, whether or not a threshold is
given; "out of range" here means an integer too large to convert. A threshold must itself be
finite.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Tuple

from omli.gates.result import GateResult


class PostGate:
    """Validates and bounds a model's output before it can become an artifact."""

    def check(
        self,
        payload: Mapping[str, Any],
        *,
        required_fields: Tuple[str, ...] = (),
        score_field: Optional[str] = None,
        threshold: Optional[float] = None,
    ) -> GateResult:
        # Schema validation: required fields must be present and non-empty.
        for field_name in required_fields:
            if field_name not in payload or payload[field_name] in (None, ""):
                return GateResult(
                    False,
                    reason=f"missing required field {field_name!r}",
                    tags=("escape_hatch", "schema_violation"),
                )

        # A threshold that is not a finite number would disable the floor (every comparison
        # with NaN is false), so it is refused rather than applied.
        if threshold is not None and _as_finite(threshold) is None:
            return GateResult(
                False,
                reason="threshold is not a finite number",
                tags=("escape_hatch", "schema_violation"),
            )

        # Whenever a score field is named, its value must be a finite number, with or without
        # a threshold. NaN compares false against everything, and a 400-digit integer cannot be
        # compared with a float at all, so neither may reach the floor below.
        if score_field is not None:
            score = _as_finite(payload.get(score_field))
            if score is None:
                return GateResult(
                    False,
                    reason=f"score field {score_field!r} is not a finite number in range",
                    tags=("escape_hatch", "schema_violation"),
                )
            # Threshold check / escape-hatch routing. Thresholds themselves come from the
            # pack (domain config); this gate only enforces whatever it is handed.
            if threshold is not None and score < float(threshold):
                return GateResult(
                    False,
                    reason=f"score {score} below threshold {threshold}",
                    tags=("escape_hatch", "below_threshold"),
                )

        return GateResult(True, tags=("validated",))


def _as_finite(value: Any) -> Optional[float]:
    """``value`` as a finite float, or None for a bool, a non-number, NaN, an infinity, or an
    integer too large to represent as a float (out of range)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None
