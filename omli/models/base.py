"""ModelClient — the generic interface every model adapter implements.

Zero domain strings. A client takes a prompt and returns text; gates and packs supply
all meaning around it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ModelClient(ABC):
    """Minimal transport contract for an open/local model."""

    #: Adapter-defined model identifier (e.g. "example-model:8b"). Generic, not domain.
    name: str = "model"

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Return the model's completion for `prompt`."""
        raise NotImplementedError
