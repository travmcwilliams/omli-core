"""Deterministic gates that wrap every model call the Agent makes (Primitive 2).

Cheap, rule-based logic on BOTH sides of a model call. On the Agent path
(`omli.agents.base.Agent.run`) an LLM is never the first or last decision-maker: the PreGate
decides whether the model runs; the PostGate decides whether its output is accepted.

Not every model call goes through an Agent. `omli.models` also exposes direct helpers (for
example `generate_completion`) that call a model with no gate on either side; a caller that
uses them owns the gating.
"""

from omli.gates.pre import PreGate
from omli.gates.post import PostGate
from omli.gates.result import GateResult
from omli.gates.traffic import (
    HardwareState,
    TrafficDecision,
    TrafficGuard,
    TrafficPolicy,
    load_traffic_policy,
    parse_traffic_policy,
)

__all__ = [
    "PreGate",
    "PostGate",
    "GateResult",
    "HardwareState",
    "TrafficDecision",
    "TrafficGuard",
    "TrafficPolicy",
    "load_traffic_policy",
    "parse_traffic_policy",
]
