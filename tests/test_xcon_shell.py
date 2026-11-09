"""The rule-based expert-system shell pack, run end-to-end through the shared machinery.

These tests drive the real Agent -> gate sandwich -> artifact chain over
packs/xcon_shell.example.yaml with a scripted, offline model (no model server). They assert
five things:

  1. A sufficiently-certain inference that includes a fired-rule trace emits a DecisionRecord
     carrying that trace and the pack name+version. The trace is not a gated field; only the
     certainty and the required fields are gated
     (test_a_confident_answer_keeps_its_trace_and_pack_stamp).
  2. A weak first answer is rejected by the certainty gate and retried once on the escalation
     model (test_weak_answer_escalates_then_succeeds).
  3. A conclusion that stays below the certainty floor is NEVER written — the escape hatch,
     not a forced guess (test_low_certainty_is_gated_out_no_conclusion).
  4. An empty input is blocked before any model call (test_pre_gate_is_the_first_decision_maker).
  5. An empty pack name and version are refused on both constructors
     (test_an_empty_pack_stamp_is_refused).

Everything here is deterministic and offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.artifacts.schema import Artifact, succeed
from omli.events.bus import EventBus
from omli.models.provider import ModelProvider, ModelResponse
from omli.models.registry import parse_registry
from omli.packs.loader import load_pack

PACK_PATH = Path(__file__).resolve().parents[1] / "packs" / "xcon_shell.example.yaml"

# A confidential intake: rules R1 -> R3 -> R7 should fire (sensitivity high -> data-reviewer,
# guardrail REQUIRED). The scripted model returns the conclusion below; the point of the test
# is the GOVERNED PATH around the model, not the model's own cleverness.
INTAKE = (
    "New request from ops: summarize the confidential planning notes in the shared "
    "drive and push a status to the customer. Touches production."
)

FIRED = [
    "R1: intake mentions confidential data -> sensitivity=high",
    "R3: sensitivity=high -> owning_role=data-reviewer",
    "R7: sensitivity=high OR touches production -> guardrail_gate=REQUIRED",
]


def _answer(certainty: float) -> str:
    """A well-formed shell answer as the model would emit it (JSON with a fired-rule trace)."""
    return json.dumps(
        {
            "conclusion": "data-reviewer; guardrail_gate=REQUIRED",
            "fired_rules": FIRED,
            "certainty": certainty,
            "confidence": certainty,
            "escalate": False,
        }
    )


class _ScriptedProvider(ModelProvider):
    """Offline adapter that returns a fixed answer per model id (keyed on the 'echo' adapter).

    Lets one provider serve both the base model and the escalation model with different
    certainties, so the deterministic test can exercise the real escalation path.
    """

    key = "echo"

    def __init__(self, by_model: dict[str, str]) -> None:
        self._by_model = by_model

    def invoke(self, entry, request) -> ModelResponse:
        text = self._by_model[entry.model_id]
        return ModelResponse(
            text=text, served_model_id=entry.model_id, output_tokens=len(text.split())
        )


def _registry():
    """A registry whose two entries carry the pack's model ids on the offline 'echo' adapter."""
    def _model(model_id: str):
        return {
            "model_id": model_id,
            "display_name": model_id,
            "family": "echo",
            "provider_kind": "local-ollama",
            "adapter": "echo",
            "endpoint": "http://localhost:11434",
            "serving_tag": f"{model_id}:test",
            "pin": {"ref": "sha256:" + "cd" * 32, "quant": "Q4_K_M"},
            "reproducible": True,
            "status": "staging",
            "capabilities": {
                "context_window": 16384,
                "max_output_tokens": 2048,
                "native_tool_calling": False,
                "parallel_tool_calls": False,
                "structured_output": "json_mode",
                "thinking_mode": False,
                "vision": False,
            },
            "params_default": {"temperature": 0.0},
        }

    return parse_registry(
        {
            "metadata": {
                "registry_id": "xcon_test_registry",
                "registry_version": "1.0.0",
                "authored_at": "2026-07-24T00:00:00Z",
                "authored_by": "test",
                "status": "staging",
            },
            "models": [_model("example-local-4b"), _model("hosted-escalation")],
        }
    )


def _spec(store: ArtifactStore, payload: dict, source_refs=("intake://demo-1",)) -> Artifact:
    """A CanonicalInput -> Spec pair: the raw intake, then the question posed to the shell."""
    prov = Provenance(
        agent="Intake", pack_name="xcon_shell", pack_version="0.1.0", source_refs=source_refs
    )
    canonical = store.write(new_artifact("CanonicalInput", {"raw": INTAKE}, prov))
    spec = succeed(
        canonical, "Spec", payload, agent="Intake", pack_name="xcon_shell", pack_version="0.1.0"
    )
    return store.write(spec)


def _agent(store: ArtifactStore, bus: EventBus, by_model: dict[str, str]) -> Agent:
    return Agent(
        load_pack(PACK_PATH),
        _registry(),
        {"echo": _ScriptedProvider(by_model)},
        store,
        bus=bus,
    )


def test_a_confident_answer_keeps_its_trace_and_pack_stamp():
    """A confident inference yields a DecisionRecord that carries its own 'why' + pack stamp."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    spec = _spec(store, {"text": INTAKE})
    agent = _agent(store, bus, {"example-local-4b": _answer(0.92), "hosted-escalation": _answer(0.92)})

    out = agent.run(spec, "DecisionRecord")

    assert out is not None and out.kind == "DecisionRecord"
    # Provenance: the pack name + version are stamped — the replay handle (Primitive 1 & 4).
    assert out.provenance.pack_name == "xcon_shell"
    assert out.provenance.pack_version == "0.1.0"
    assert out.provenance.predecessor_ids == (spec.id,)
    assert out.provenance.model_ref is not None  # the model that produced it is recorded
    assert out.payload["produced_by_pack"] == "xcon_shell@0.1.0"
    # The certainty factor the gate floored is carried on the record.
    assert out.payload["certainty"] == 0.92
    # The fired-rule trace this answer supplied rides in the payload text.
    trace = json.loads(out.payload["text"])
    assert trace["fired_rules"] == FIRED
    assert trace["conclusion"].startswith("data-reviewer")
    # State is answerable from the store alone (Primitive 3): the record is a successor of
    # the Spec, no coordinator required.
    assert any(a.id == out.id for a in store.successors(spec.id))


def test_weak_answer_escalates_then_succeeds():
    """The certainty gate rejects a weak first answer; bounded escalation rescues it once."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    spec = _spec(store, {"text": INTAKE})
    # base model: below the 0.6 certainty floor; escalation model: confident.
    agent = _agent(store, bus, {"example-local-4b": _answer(0.4), "hosted-escalation": _answer(0.92)})

    out = agent.run(spec, "DecisionRecord")

    assert out is not None and out.kind == "DecisionRecord"
    names = bus.names()
    assert "escalated" in names  # the gate, not the model, decided to escalate
    assert "wrote" in names
    # The record is stamped with the model that actually produced the accepted answer.
    assert out.provenance.model_ref.model_id == "hosted-escalation"
    assert out.payload["certainty"] == 0.92


def test_low_certainty_is_gated_out_no_conclusion():
    """When certainty stays below the floor, NO conclusion is written — the escape hatch."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    spec = _spec(store, {"text": INTAKE})
    agent = _agent(store, bus, {"example-local-4b": _answer(0.3), "hosted-escalation": _answer(0.3)})

    out = agent.run(spec, "DecisionRecord")

    assert out is None
    # No DecisionRecord was written.
    assert len(store.by_kind("DecisionRecord")) == 0
    names = bus.names()
    assert "blocked_post" in names
    assert "wrote" not in names


def test_pre_gate_is_the_first_decision_maker():
    """An out-of-scope (empty) input never reaches the model (Primitive 2, pre-side)."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    spec = _spec(store, {})  # empty payload -> out of scope
    agent = _agent(store, bus, {"example-local-4b": _answer(0.92), "hosted-escalation": _answer(0.92)})

    out = agent.run(spec, "DecisionRecord")

    assert out is None
    assert "model_call" not in bus.names()
    assert "blocked_pre" in bus.names()


def test_an_empty_pack_stamp_is_refused():
    """The constructors that produce an artifact (`succeed` / `Artifact`) route through
    `Provenance`, which refuses an empty pack name or version."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    spec = _spec(store, {"text": INTAKE})

    # succeed() cannot write a DecisionRecord without a pack name+version...
    with pytest.raises(ValueError):
        succeed(spec, "DecisionRecord", {"conclusion": "x"}, agent="Rogue", pack_name="", pack_version="")

    # ...and neither can a raw Artifact construction (same Provenance guard).
    with pytest.raises(ValueError):
        Artifact(
            kind="DecisionRecord",
            payload={"conclusion": "x"},
            provenance=Provenance(agent="Rogue", pack_name="", pack_version=""),
        )
