"""Local / open-model adapter.

`LocalEchoModel` is a dependency-free stub so the package runs offline and
deterministically. `OllamaModel` (`ollama.py`) implements the same `ModelClient.generate`
contract, and `make_model` picks between them; core, gates and agents do not change.
"""

from __future__ import annotations

from omli.models.base import ModelClient


class LocalEchoModel(ModelClient):
    """Deterministic stub: echoes a trimmed, labeled view of the prompt.

    Deterministic on purpose — no randomness — so tests and reruns are reproducible.
    """

    def __init__(self, name: str = "stub-local") -> None:
        self.name = name

    def generate(self, prompt: str) -> str:
        trimmed = " ".join(prompt.split())[:200]
        return f"[{self.name}] {trimmed}"
