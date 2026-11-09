"""Pack schema — the typed shape of a domain config pack (Primitive 1).

A `Pack` is a frozen value object: immutable per version. It carries the agent identity,
the prompt template, domain thresholds, and the required output fields the PostGate
enforces. All of these are *data*; none of them are code in the core package.

Two OPTIONAL blocks: `model_selection`
(which registry model the role uses, plus bounded escalation) and `model_requirements`
(the capabilities the role needs). A pack that omits both is valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

# Escalation triggers a pack may name (deterministic; see the escalation gate).
ESCALATION_TRIGGERS = ("gate_rejection", "low_confidence", "explicit_flag")

# Structured-output strength ordering: requiring "json_mode" is satisfied by a model
# that offers "json_mode" OR the stronger "grammar".
_STRUCTURED_LEVEL = {"none": 0, "json_mode": 1, "grammar": 2}


@dataclass(frozen=True)
class Escalation:
    """Bounded hand-off to a bigger model on a deterministic trigger."""

    model_id: str
    trigger: str = "gate_rejection"
    max_escalations: int = 1

    def __post_init__(self) -> None:
        if self.trigger not in ESCALATION_TRIGGERS:
            raise ValueError(f"escalation.trigger must be one of {ESCALATION_TRIGGERS}")
        if self.max_escalations < 0:
            raise ValueError("escalation.max_escalations must be >= 0 (bounded, never unbounded)")


@dataclass(frozen=True)
class ModelSelection:
    """Which registry model this pack's role uses, by id (never a model name in code)."""

    model_id: str
    escalation: Optional[Escalation] = None


@dataclass(frozen=True)
class Scoring:
    """A deterministic floor the PostGate enforces on a model-produced numeric field.

    `score_field` names a numeric field the model emits in its (JSON) output (e.g.
    "score"); the PostGate rejects any output whose value is below `threshold` — the
    escape hatch (Primitive 2). This is DATA in the pack, never a domain string in core.

    Distinct from `thresholds["min_confidence"]`, which floors the parsed *confidence*
    signal common to every pack; `scoring` floors a domain score the pack names itself. When a
    pack declares both, both floors apply.
    """

    score_field: str
    threshold: float

    def __post_init__(self) -> None:
        if not self.score_field:
            raise ValueError("scoring.score_field must be a non-empty field name")


@dataclass(frozen=True)
class ModelRequirements:
    """Capabilities the role needs; the Router checks these ⊆ the registry entry.

    All fields are non-constraining by default, so an empty block requires nothing.
    """

    min_context_window: int = 0
    min_max_output_tokens: int = 0
    native_tool_calling: bool = False
    parallel_tool_calls: bool = False
    thinking_mode: bool = False
    vision: bool = False
    structured_output: Optional[str] = None  # minimum level: "json_mode" | "grammar"

    def __post_init__(self) -> None:
        if self.structured_output is not None and self.structured_output not in _STRUCTURED_LEVEL:
            raise ValueError(f"model_requirements.structured_output must be one of {tuple(_STRUCTURED_LEVEL)}")

    def unmet(self, caps) -> list[str]:
        """Return human-readable reasons the capabilities fail these requirements (empty = ok)."""
        problems: list[str] = []
        # A kind with no text surface states None for these, which meets no requirement.
        if self.min_context_window > (caps.context_window or 0):
            problems.append(f"context_window {caps.context_window} < required {self.min_context_window}")
        if self.min_max_output_tokens > (caps.max_output_tokens or 0):
            problems.append(f"max_output_tokens {caps.max_output_tokens} < required {self.min_max_output_tokens}")
        if self.native_tool_calling and not caps.native_tool_calling:
            problems.append("native_tool_calling required")
        if self.parallel_tool_calls and not caps.parallel_tool_calls:
            problems.append("parallel_tool_calls required")
        if self.thinking_mode and not caps.thinking_mode:
            problems.append("thinking_mode required")
        if self.vision and not caps.vision:
            problems.append("vision required")
        if self.structured_output is not None:
            need = _STRUCTURED_LEVEL[self.structured_output]
            have = _STRUCTURED_LEVEL.get(caps.structured_output, 0)
            if have < need:
                problems.append(
                    f"structured_output {caps.structured_output!r} weaker than required {self.structured_output!r}"
                )
        return problems


@dataclass(frozen=True)
class Pack:
    """An immutable domain configuration applied by an agent."""

    name: str
    version: str
    identity: str
    prompt_template: str
    required_output_fields: Tuple[str, ...] = field(default_factory=tuple)
    thresholds: Mapping[str, float] = field(default_factory=dict)
    # Optional:
    model_selection: Optional[ModelSelection] = None
    model_requirements: Optional[ModelRequirements] = None
    # Optional: a domain score floor the PostGate enforces (see `Scoring`).
    scoring: Optional[Scoring] = None

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("a pack requires a name and version")
        if "{input}" not in self.prompt_template:
            raise ValueError("prompt_template must contain the '{input}' placeholder")
        object.__setattr__(self, "required_output_fields", tuple(self.required_output_fields))
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))

    def render_prompt(self, input_text: str) -> str:
        """Render the pack's prompt template against a single input string."""
        return self.prompt_template.replace("{input}", input_text)
