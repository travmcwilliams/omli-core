"""SCQC critic — the contract for a pack-configured self-consistency/quality critic.

The critic is a plain `omli/agents/base.py::Agent` driven by `packs/scqc/pack.yaml`: it
consumes a WorkResult and advances it to an OutcomeRecord verdict. It is NOT an LLM inside a
gate (Primitive 2) — its own model call is wrapped by the standard pre/post gates, and the
DETERMINISTIC PostGate decides whether a passing verdict is written. These tests are the
contract: the floor is pack DATA (Primitive 1), a well-supported result ships an
OutcomeRecord, and a weak-or-malformed verdict is gated out (nothing written).

The critic has no code of its own: these tests mirror tests/test_score_threshold_gate.py for
the WorkResult -> OutcomeRecord step, driving the shared Agent with the critic pack only.
"""

from __future__ import annotations

import json
from pathlib import Path

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore
from omli.artifacts.provenance import Provenance
from omli.artifacts.schema import new_artifact
from omli.models.provider import ModelProvider, ModelResponse
from omli.models.registry import load_registry
from omli.packs.loader import load_pack
from omli.packs.schema import Scoring

_ROOT = Path(__file__).resolve().parent.parent
_SCQC_PACK = _ROOT / "packs" / "scqc" / "pack.yaml"
_REGISTRY = _ROOT / "tests" / "data" / "registry.yaml"


class _CannedProvider(ModelProvider):
    """Returns a fixed response text no matter the request — drives the gate deterministically."""

    key = "canned"

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    def invoke(self, entry, request) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text=self._text, served_model_id=entry.model_id, output_tokens=3)


def _seed_workresult(store: ArtifactStore):
    """A valid WorkResult predecessor for the critic to review into an OutcomeRecord."""
    work = new_artifact(
        "WorkResult",
        {"text": "Shipped the intake path; all 12 acceptance tests pass.", "project": "demo"},
        Provenance(agent="test", pack_name="seed", pack_version="0.0.0",
                   source_refs=("file:///code/demo",)),
    )
    return store.write(work)


def _critic(pack, provider, store) -> Agent:
    registry = load_registry(_REGISTRY)
    # The pack selects example-3b (adapter "ollama"); inject the canned provider there.
    return Agent(pack, registry, {"ollama": provider}, store)


# --------------------------------------------------------------------------- #
# schema + loader: the critic's floor is pack DATA (Primitive 1)
# --------------------------------------------------------------------------- #
def test_scqc_pack_declares_scoring_as_data():
    pack = load_pack(_SCQC_PACK)
    assert pack.scoring == Scoring(score_field="quality", threshold=0.6)
    assert "quality" in pack.required_output_fields
    assert "text" in pack.required_output_fields


# --------------------------------------------------------------------------- #
# end-to-end through the Agent: WorkResult -> OutcomeRecord, the gate decides
# --------------------------------------------------------------------------- #
def test_pass_verdict_ships_outcome(tmp_path):
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    work = _seed_workresult(store)
    prov = _CannedProvider(json.dumps(
        {"verdict": "pass", "quality": 0.82, "text": "pass — conclusion follows from cited tests"}))

    out = _critic(load_pack(_SCQC_PACK), prov, store).run(work, "OutcomeRecord")

    assert out is not None and out.kind == "OutcomeRecord"   # advanced one step along the chain
    assert out.payload["quality"] == 0.82                    # the gated score rode into the verdict
    assert out.provenance.predecessor_ids == (work.id,)      # linked successor (Primitive 4)
    assert store.get(out.id) is not None                     # and it is durable


def test_low_quality_verdict_writes_no_outcome(tmp_path):
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    work = _seed_workresult(store)
    prov = _CannedProvider(json.dumps(
        {"verdict": "fail", "quality": 0.20, "text": "fail — claim unsupported by its own refs"}))

    out = _critic(load_pack(_SCQC_PACK), prov, store).run(work, "OutcomeRecord")

    assert out is None                              # escape hatch: no rubber-stamped verdict written
    assert store.by_kind("OutcomeRecord") == ()     # the weak result got no passing OutcomeRecord


def test_malformed_verdict_is_blocked_as_a_missing_required_field(tmp_path):
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    work = _seed_workresult(store)
    prov = _CannedProvider("just prose — no JSON, no quality field")

    from omli.events.bus import EventBus

    bus = EventBus()
    agent = Agent(load_pack(_SCQC_PACK), load_registry(_REGISTRY), {"ollama": prov}, store, bus=bus)
    out = agent.run(work, "OutcomeRecord")

    assert out is None                              # 'quality' required but absent → blocked
    assert store.by_kind("OutcomeRecord") == ()
    blocked = [e for e in bus.log() if e.name == "blocked_post"]
    assert "missing required field 'quality'" in blocked[0].payload["reason"]
