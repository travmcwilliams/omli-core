"""Bounded escalation — a deterministic decision to retry on a better model.

This is gate-side logic (Primitive 2): given the post-gate verdict and optional signals
parsed from the model's output, decide whether to escalate. The `gate_rejection` and
`low_confidence` triggers decide from the post-gate verdict alone. The `explicit_flag` trigger
is different: a pack that chooses it lets the model's own `"escalate": true` request a retry.
Even then the model cannot choose where the retry goes (the pack names the escalation model),
cannot exceed the pack's `max_escalations`, and cannot get past the post-gate, which still
judges the escalated output.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from omli.gates.result import GateResult


@dataclass(frozen=True)
class ModelSignals:
    """Optional signals parsed best-effort from a model's response text.

    `fields` is the raw JSON object the model returned (empty when it emitted plain
    text), so a pack that names a domain `score_field` can have that value read
    deterministically without core knowing the field name in advance.
    """

    confidence: Optional[float] = None
    escalate: bool = False
    fields: Mapping[str, Any] = field(default_factory=dict)
    #: True when the output HAS a confidence that is not a finite number (NaN, an infinity,
    #: an integer too large for a float, a string, a boolean). Distinct from absent: a pack
    #: with `min_confidence` rejects it instead of treating it as "no confidence reported".
    confidence_invalid: bool = False


def _finite(raw: Any) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def parse_signals(text: str) -> ModelSignals:
    """If the response is a JSON object, read optional confidence/escalate. Never raises.

    Anything that is not a JSON object yields empty signals — plain-text models are fine.
    A `confidence` key that is present (and not null) but not a finite number sets
    `confidence_invalid` rather than reading as absent.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return ModelSignals()
    if not isinstance(data, dict):
        return ModelSignals()
    raw = data.get("confidence")
    confidence = _finite(raw)
    return ModelSignals(
        confidence=confidence,
        escalate=data.get("escalate") is True,
        fields=MappingProxyType(dict(data)),
        confidence_invalid=raw is not None and confidence is None,
    )


def should_escalate(trigger: str, post: GateResult, signals: ModelSignals) -> bool:
    """Deterministic escalation decision for a single attempt.

    - gate_rejection: the post-gate rejected the output for any reason.
    - low_confidence: the post-gate rejected specifically on a threshold (below_threshold).
    - explicit_flag: the output asked to escalate, even if the gate otherwise passed.
    """
    if trigger == "gate_rejection":
        return not post.passed
    if trigger == "low_confidence":
        return (not post.passed) and ("below_threshold" in post.tags)
    if trigger == "explicit_flag":
        return signals.escalate
    return False
