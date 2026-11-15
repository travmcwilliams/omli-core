"""Every Mind-map model call: Agent.run with the public pre-gate and post-gate (Primitive 2).

Expand, analyze, interpret, dump sorting and the retrieval summary each name a pack shipped in
``omli/defaults`` (prompt, identity and model role are pack data, Primitive 1). The pack names
a model role; the user's registry, ``OMLI_HOME/models/registry.yaml``, resolves it to a
concrete model. This module names no model, provider or price.

One request is one ``Agent.run``:

* the pre-gate refuses an empty input, and the agent's route check refuses content tagged
  local-only for any provider kind that is not local, before any provider call;
* the post-gate checks the model's output; a refusal writes nothing;
* only when the agent returns a successor are the input (a CanonicalInput) and the output (a
  WorkResult) appended to ``OMLI_HOME/var/artifacts.jsonl``, input first.

No registry, no entry for the role, or a role that cannot run raises ``NoModelConfigured``
before any call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.boards.checkpoint import artifact_log
from omli.defaults import default_pack_path
from omli.home import omli_home
from omli.models.provider import ProviderTable, default_provider_table
from omli.models.registry import load_registry
from omli.packs.loader import load_pack

AGENT = "MindmapAssistant"
OUTPUT_KIND = "WorkResult"
REGISTRY = Path("models") / "registry.yaml"
PACKS = {
    "expand": "mindmap_expand.yaml",
    "analyze": "mindmap_analyze.yaml",
    "interpret": "mindmap_interpret.yaml",
    "dump": "mindmap_dump.yaml",
    "rag": "mindmap_rag.yaml",
}
_STAGES = {"blocked_pre": "pre-gate", "blocked_post": "post-gate", "model_error": "model"}


class NoModelConfigured(Exception):
    """No usable model is configured for the pack's role. Nothing was called or written."""


@dataclass(frozen=True)
class AssistResult:
    ok: bool
    stage: str                 # done | pre-gate | post-gate | model
    reason: str = ""
    text: Optional[str] = None
    input_id: Optional[str] = None
    output_id: Optional[str] = None


class _Events:
    """Collects the agent's events so a refusal can say which gate refused and why."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, dict]] = []

    def emit(self, name: str, payload: Optional[Mapping[str, Any]] = None) -> None:
        self.events.append((name, dict(payload or {})))


class _Held:
    """Holds the agent's successor so the input can be persisted first, and nothing at all is
    persisted when the agent returns no successor."""

    def __init__(self) -> None:
        self.items: list = []

    def write(self, artifact):
        self.items.append(artifact)
        return artifact


def tags_of(value: Any) -> Tuple[str, ...]:
    """Sensitivity tags from a node field: a string or a list of strings."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if isinstance(v, str))
    return ()


def board_sensitivity(state: Mapping[str, Any]) -> Tuple[str, ...]:
    """Every sensitivity tag on any node of the board. A model call that reads a board carries
    them all, so a local-only node keeps the whole request off a non-local provider."""
    out: list = []
    for node in (state.get("nodes") or {}).values():
        for tag in tags_of(node.get("sensitivity")):
            if tag not in out:
                out.append(tag)
    return tuple(out)


class Assistant:
    """Runs one gated model call per request for the Mind-map routes."""

    def __init__(self, home: str | Path | None = None,
                 providers: Optional[ProviderTable] = None) -> None:
        self._home = Path(home).resolve() if home is not None else None
        self._providers = providers

    def _root(self) -> Path:
        return self._home if self._home is not None else omli_home()

    def registry(self):
        """The user's registry, or None when there is none."""
        path = self._root() / REGISTRY
        return load_registry(path) if path.is_file() else None

    def _agent(self, action: str, events: _Events, held: _Held):
        pack = load_pack(default_pack_path(PACKS[action]))
        role = pack.model_selection.model_id if pack.model_selection else None
        where = f"OMLI_HOME/{REGISTRY.as_posix()}"
        registry = self.registry()
        if registry is None:
            raise NoModelConfigured(f"no model configured: add a model for the role {role!r} "
                                    f"to {where}")
        if role not in registry.ids():
            raise NoModelConfigured(f"no model configured: {where} has no model for the role "
                                    f"{role!r}")
        providers = self._providers if self._providers is not None else default_provider_table()
        try:
            return pack, Agent(pack, registry, providers, held, bus=events)
        except ValueError as exc:
            raise NoModelConfigured(f"no model configured that can run: {exc}") from None

    def status(self) -> Dict[str, Any]:
        """Whether every Mind-map action has a model it can run, without calling one or
        writing anything: ``{"configured": bool, "reason": str}``."""
        events, held = _Events(), _Held()
        try:
            for action in PACKS:
                self._agent(action, events, held)
        except NoModelConfigured as exc:
            return {"configured": False, "reason": str(exc)}
        return {"configured": True, "reason": ""}

    def run(self, action: str, fields: Mapping[str, Any], *,
            sensitivity: Iterable[str] = (), source_refs: Iterable[str] = ()) -> AssistResult:
        """One gated model call. Raises NoModelConfigured (nothing called, nothing written) or
        ValueError for an unknown sensitivity tag (likewise)."""
        if action not in PACKS:
            raise ValueError(f"unknown action {action!r}")
        events, held = _Events(), _Held()
        pack, agent = self._agent(action, events, held)
        payload = {k: v for k, v in fields.items() if v not in (None, "", [], {}, ())}
        ci = new_artifact("CanonicalInput", payload, Provenance(
            agent=AGENT, pack_name=pack.name, pack_version=pack.version,
            source_refs=tuple(source_refs), sensitivity=tuple(sensitivity)))
        out = agent.run(ci, OUTPUT_KIND)
        if out is None:
            name, info = next(((n, p) for n, p in reversed(events.events) if n in _STAGES),
                              ("blocked_post", {"reason": "refused"}))
            return AssistResult(False, _STAGES[name], str(info.get("reason") or ""))
        store = ArtifactStore.from_log(artifact_log(self._root()))
        store.write(ci)
        for artifact in held.items:
            store.write(artifact)
        return AssistResult(True, "done", "", str(out.payload.get("text", "")), ci.id, out.id)
