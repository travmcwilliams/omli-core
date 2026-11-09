"""make_model — resolve a ModelSpec into a concrete ModelClient.

The single place provider strings map to adapters. Wiring code builds a ModelSpec
(from config/env) and calls this; agents and core never name a provider.
"""

from __future__ import annotations

from omli.models.base import ModelClient
from omli.models.local import LocalEchoModel
from omli.models.ollama import OllamaModel
from omli.models.spec import ModelSpec


def make_model(spec: ModelSpec) -> ModelClient:
    if spec.provider == "echo":
        return LocalEchoModel(name=spec.model)
    if spec.provider == "ollama":
        return OllamaModel(model=spec.model, host=spec.host, timeout_s=spec.timeout_s)
    raise ValueError(f"unknown model provider {spec.provider!r}")
