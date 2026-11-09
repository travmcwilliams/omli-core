"""Pack-declared score floor enforced through the PostGate (Primitives 1 & 2).

A scoring-domain pack names a numeric `score_field` and a `threshold` as DATA; the
deterministic PostGate — not the model — decides whether an output above the floor becomes
an artifact. These tests are the contract for that wiring: schema + loader (the field is
pack data), Agent (`_build_candidate`/`_check_post` carry and enforce it), and the gate
(`omli/gates/post.py`) itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore
from omli.artifacts.provenance import Provenance
from omli.artifacts.schema import new_artifact
from omli.gates.post import PostGate
from omli.models.provider import ModelProvider, ModelResponse
from omli.models.registry import load_registry
from omli.packs.loader import load_pack
from omli.packs.schema import Scoring

_ROOT = Path(__file__).resolve().parent.parent
_SCORING_PACK = _ROOT / "packs" / "scoring" / "pack.yaml"
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


def _seed_decision(store: ArtifactStore):
    """A valid DecisionRecord predecessor for the scoring agent to advance to a WorkResult."""
    dec = new_artifact(
        "DecisionRecord",
        {"text": "assess delivery readiness", "project": "demo"},
        Provenance(agent="test", pack_name="seed", pack_version="0.0.0",
                   source_refs=("file:///code/demo",)),
    )
    return store.write(dec)


def _agent(pack, provider, store) -> Agent:
    registry = load_registry(_REGISTRY)
    # The scoring pack selects example-3b (adapter "ollama"); inject the canned provider there.
    return Agent(pack, registry, {"ollama": provider}, store)


# --------------------------------------------------------------------------- #
# schema + loader: the floor is pack DATA (Primitive 1)
# --------------------------------------------------------------------------- #
def test_pack_declares_scoring_as_data():
    pack = load_pack(_SCORING_PACK)
    assert pack.scoring == Scoring(score_field="score", threshold=0.6)
    assert "score" in pack.required_output_fields


def test_scoring_requires_a_field_name():
    with pytest.raises(ValueError):
        Scoring(score_field="", threshold=0.5)


def test_loader_rejects_partial_scoring_block(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\nversion: '0.1.0'\nidentity: y\nprompt_template: '{input}'\n"
        "scoring:\n  score_field: score\n",  # threshold missing
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_pack(bad)


# --------------------------------------------------------------------------- #
# end-to-end through the Agent: the gate, not the model, decides
# --------------------------------------------------------------------------- #
def test_score_above_threshold_ships(tmp_path):
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    dec = _seed_decision(store)
    prov = _CannedProvider(json.dumps({"score": 0.82, "text": "solid CI, broad tests"}))

    out = _agent(load_pack(_SCORING_PACK), prov, store).run(dec, "WorkResult")

    assert out is not None and out.kind == "WorkResult"
    assert out.payload["score"] == 0.82           # the named score rode into the artifact
    assert store.get(out.id) is not None          # and it is durable


def test_score_below_threshold_is_gated_out(tmp_path):
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    dec = _seed_decision(store)
    prov = _CannedProvider(json.dumps({"score": 0.20, "text": "no tests, stale deps"}))

    out = _agent(load_pack(_SCORING_PACK), prov, store).run(dec, "WorkResult")

    assert out is None                            # escape hatch: nothing written
    assert store.by_kind("WorkResult") == ()


def test_missing_score_is_blocked_as_a_missing_required_field(tmp_path):
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    dec = _seed_decision(store)
    prov = _CannedProvider("just prose — no JSON, no score field")

    from omli.events.bus import EventBus

    bus = EventBus()
    agent = Agent(load_pack(_SCORING_PACK), load_registry(_REGISTRY), {"ollama": prov}, store, bus=bus)
    out = agent.run(dec, "WorkResult")

    assert out is None                            # 'score' required but absent → blocked
    assert store.by_kind("WorkResult") == ()
    blocked = [e for e in bus.log() if e.name == "blocked_post"]
    assert "missing required field 'score'" in blocked[0].payload["reason"]


# --------------------------------------------------------------------------- #
# PostGate itself: the two rejection tags the escalation gate keys on
# --------------------------------------------------------------------------- #
def test_postgate_below_threshold_tags_escape_hatch():
    r = PostGate().check({"text": "x", "score": 0.2},
                         required_fields=("text", "score"), score_field="score", threshold=0.6)
    assert not r.passed
    assert "below_threshold" in r.tags and "escape_hatch" in r.tags


def test_postgate_non_numeric_score_is_schema_violation():
    r = PostGate().check({"text": "x", "score": "high"},
                         required_fields=("text",), score_field="score", threshold=0.6)
    assert not r.passed
    assert "schema_violation" in r.tags
