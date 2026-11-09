"""Agents — generic machinery that applies a pack (Primitives 1 & 5).

An agent loads a pack and runs the fixed pipeline: pre-gate -> model -> post-gate ->
write successor artifact. Agent code contains zero domain strings; everything domain-
specific comes from the pack. No agent rewrites its own identity, memory, or packs.
"""

from omli.agents.base import Agent
from omli.agents.reflector import Reflector, MemoryStore

__all__ = ["Agent", "Reflector", "MemoryStore"]
