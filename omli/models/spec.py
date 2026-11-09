"""ModelSpec — infra config that binds an agent to a concrete model.

This is the direct construction path, resolved by the factory, for a caller that wants a
`ModelClient` without a pack. Agents take their model differently: a pack selects a registry
entry by `model_id`, and only the registry names a concrete model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    """Immutable description of which model to construct.

    Attributes:
        provider: Adapter key — "echo" (offline stub) or "ollama".
        model: Provider-specific model id (e.g. "example-model:8b").
        host: Base URL for networked providers; ignored by "echo".
        timeout_s: Per-call timeout in seconds; ignored by "echo".
    """

    provider: str = "echo"
    model: str = "stub-local"
    host: str = "http://localhost:11434"
    timeout_s: float = 60.0
