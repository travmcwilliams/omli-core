"""Bounded escalation. Deterministic, gate-driven, never unbounded."""

import json

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.events.bus import EventBus
from omli.models.errors import ModelUnavailable
from omli.models.provider import ModelProvider, ModelResponse
from omli.models.registry import parse_registry
from omli.packs.schema import Escalation, ModelSelection, Pack


def _entry_dict(model_id, tag):
    return {
        "model_id": model_id,
        "display_name": model_id,
        "family": "f",
        "provider_kind": "local-ollama",
        "adapter": "echo",
        "endpoint": "http://localhost:11434",
        "serving_tag": tag,
        "pin": {"ref": "sha256:" + "ab" * 32, "quant": "Q4_K_M"},
        "reproducible": True,
        "status": "staging",
        "capabilities": {
            "context_window": 8192,
            "max_output_tokens": 2048,
            "native_tool_calling": False,
            "parallel_tool_calls": False,
            "structured_output": "none",
            "thinking_mode": False,
            "vision": False,
        },
        "params_default": {"temperature": 0.0},
    }


def _registry():
    return parse_registry(
        {
            "metadata": {
                "registry_id": "t",
                "registry_version": "1.0.0",
                "authored_at": "2026-06-18T00:00:00Z",
                "authored_by": "t",
                "status": "staging",
            },
            "models": [_entry_dict("primary-m", "primary:tag"), _entry_dict("esc-m", "esc:tag")],
        }
    )


class _MapProvider(ModelProvider):
    """Returns a per-model JSON payload as the response text."""

    key = "echo"

    def __init__(self, by_model):
        self._by_model = by_model

    def invoke(self, entry, request):
        payload = self._by_model[entry.model_id]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(text=text, served_model_id=entry.model_id)


def _pack(trigger="gate_rejection", max_escalations=1, with_escalation=True, thresholds=None):
    selection = ModelSelection(
        model_id="primary-m",
        escalation=Escalation(model_id="esc-m", trigger=trigger, max_escalations=max_escalations)
        if with_escalation
        else None,
    )
    return Pack(
        name="p",
        version="1",
        identity="demo",
        prompt_template="{input}",
        required_output_fields=("text", "produced_by_pack"),
        thresholds=thresholds or {},
        model_selection=selection,
    )


def _canonical():
    prov = Provenance(agent="Intake", pack_name="p", pack_version="1", source_refs=("src://1",))
    return new_artifact("CanonicalInput", {"q": "hi"}, prov)


def _run(pack, by_model):
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    agent = Agent(pack, _registry(), {"echo": _MapProvider(by_model)}, store, bus=bus)
    out = agent.run(_canonical(), "Spec")
    return out, store, bus


def test_primary_passes_no_escalation():
    out, store, bus = _run(_pack(), {"primary-m": {"x": "ok"}, "esc-m": {"x": "unused"}})
    assert out is not None
    assert "escalated" not in bus.names()
    assert out.provenance.model_ref.model_id == "primary-m"


def test_gate_rejection_escalates_and_writes_escalation_output():
    # Primary returns empty text -> post-gate rejects "text" -> escalate; escalation is valid.
    out, store, bus = _run(_pack(trigger="gate_rejection"), {"primary-m": "", "esc-m": "final answer"})
    assert out is not None
    assert bus.names().count("escalated") == 1
    assert out.provenance.model_ref.model_id == "esc-m"
    assert out.provenance.model_ref.served_model_id == "esc-m"
    assert dict(out.payload)["text"] == "final answer"


def test_low_confidence_escalates_on_threshold():
    pack = _pack(trigger="low_confidence", thresholds={"min_confidence": 0.5})
    out, store, bus = _run(pack, {"primary-m": {"confidence": 0.2}, "esc-m": {"confidence": 0.9}})
    assert out is not None
    assert "escalated" in bus.names()
    assert out.provenance.model_ref.model_id == "esc-m"


def test_explicit_flag_escalates_even_when_gate_passes():
    # Primary output is gate-valid (non-empty text) but flags escalate=true.
    out, store, bus = _run(
        _pack(trigger="explicit_flag"),
        {"primary-m": {"escalate": True, "note": "unsure"}, "esc-m": {"note": "confident"}},
    )
    assert out is not None
    assert "escalated" in bus.names()
    assert out.provenance.model_ref.model_id == "esc-m"


def test_max_escalations_is_a_hard_bound():
    # Both models fail; max_escalations=1 => exactly 2 attempts, 1 escalation, then blocked.
    pack = _pack(trigger="gate_rejection", max_escalations=1)
    out, store, bus = _run(pack, {"primary-m": "", "esc-m": ""})
    assert out is None
    assert len(store) == 0
    names = bus.names()
    assert names.count("model_call") == 2  # 1 + max_escalations, never unbounded
    assert names.count("escalated") == 1
    assert "blocked_post" in names
    assert "wrote" not in names


def test_no_escalation_config_blocks_at_the_post_gate():
    out, store, bus = _run(_pack(with_escalation=False), {"primary-m": "", "esc-m": "x"})
    assert out is None
    assert "escalated" not in bus.names()
    assert "blocked_post" in bus.names()


class _FailingProvider(ModelProvider):
    """Like _MapProvider, but raises ModelUnavailable for any model id in `fail`."""

    key = "echo"

    def __init__(self, by_model, fail=()):
        self._by_model = by_model
        self._fail = frozenset(fail)

    def invoke(self, entry, request):
        if entry.model_id in self._fail:
            raise ModelUnavailable(f"{entry.model_id} unreachable")
        payload = self._by_model[entry.model_id]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(text=text, served_model_id=entry.model_id)


def test_model_error_on_escalation_attempt_is_escape_hatch():
    """A ModelError on the ESCALATION attempt takes the escape hatch: nothing is written, the
    run returns None, and the event log shows `model_error`, not a post-gate block (P2). The
    model is never the last decision-maker on a late attempt."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    # Primary returns empty text -> post-gate rejects -> escalate; the esc model then raises.
    provider = _FailingProvider({"primary-m": "", "esc-m": "unused"}, fail=("esc-m",))
    agent = Agent(_pack(trigger="gate_rejection"), _registry(), {"echo": provider}, store, bus=bus)
    out = agent.run(_canonical(), "Spec")

    assert out is None
    assert len(store) == 0                        # nothing persisted
    names = bus.names()
    assert names.count("model_call") == 2         # primary + escalation attempt
    assert names.count("escalated") == 1          # primary was rejected and escalated...
    assert "model_error" in names                 # ...then the escalation model was unreachable
    assert "wrote" not in names
    assert "blocked_post" not in names            # escape hatch, NOT a post-gate block
