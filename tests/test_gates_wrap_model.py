"""Primitive 2: a model is never called without a pre-gate, nor written without a post-gate.

The end-to-end pass routes through the registry-resolved provider boundary:
CanonicalInput -> OutcomeRecord through an EchoProvider, with the event log showing gate
order and model identity stamped into provenance.
"""

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.events.bus import EventBus
from omli.models.errors import ModelUnavailable
from omli.models.provider import EchoProvider, ModelProvider
from omli.models.registry import parse_registry
from omli.packs.schema import ModelSelection, Pack


def _registry():
    return parse_registry(
        {
            "metadata": {
                "registry_id": "test_registry",
                "registry_version": "1.0.0",
                "authored_at": "2026-06-17T00:00:00Z",
                "authored_by": "test",
                "status": "staging",
            },
            "models": [
                {
                    "model_id": "echo-test",
                    "display_name": "Echo (test)",
                    "family": "echo",
                    "provider_kind": "local-ollama",
                    "adapter": "echo",
                    "endpoint": "http://localhost:11434",
                    "serving_tag": "echo:test",
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
            ],
        }
    )


def _pack(required=("text", "produced_by_pack")):
    return Pack(
        name="example",
        version="0.1.0",
        identity="demo",
        prompt_template="Echo: {input}",
        required_output_fields=required,
        model_selection=ModelSelection(model_id="echo-test"),
    )


def _canonical(payload, source_refs=()):
    prov = Provenance(
        agent="Intake",
        pack_name="example",
        pack_version="0.1.0",
        source_refs=source_refs,
    )
    return new_artifact("CanonicalInput", payload, prov)


def _agent(pack, store, bus, providers=None):
    return Agent(pack, _registry(), providers or {"echo": EchoProvider()}, store, bus=bus)


class _FailingProvider(ModelProvider):
    key = "echo"

    def invoke(self, entry, request):
        raise ModelUnavailable("endpoint down")


def test_happy_path_wraps_model_in_order():
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    out = _agent(_pack(), store, bus).run(_canonical({"q": "hello"}), "Spec")

    assert out is not None
    assert out.kind == "Spec"
    names = bus.names()
    assert names.index("pre_gate") < names.index("model_call")
    assert names.index("model_call") < names.index("post_gate")
    assert "wrote" in names
    # Model identity stamped into provenance.
    assert out.provenance.model_ref is not None
    assert out.provenance.model_ref.model_id == "echo-test"
    assert out.provenance.model_ref.served_model_id == "echo-test"
    assert out.provenance.model_ref.registry_version == "1.0.0"


def test_pre_gate_block_prevents_model_call():
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    out = _agent(_pack(), store, bus).run(_canonical({}), "Spec")

    assert out is None
    assert len(store) == 0
    assert "model_call" not in bus.names()
    assert "blocked_pre" in bus.names()


def test_post_gate_block_prevents_artifact_write():
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    pack = _pack(required=("text", "nonexistent_field"))
    out = _agent(pack, store, bus).run(_canonical({"q": "hello"}), "Spec")

    assert out is None
    assert len(store) == 0
    assert "model_call" in bus.names()
    assert "blocked_post" in bus.names()
    assert "wrote" not in bus.names()


def test_model_error_routes_to_escape_hatch():
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    agent = _agent(_pack(), store, bus, providers={"echo": _FailingProvider()})

    out = agent.run(_canonical({"q": "hello"}), "Spec")

    assert out is None
    assert len(store) == 0
    names = bus.names()
    assert "model_call" in names
    assert "model_error" in names
    assert "wrote" not in names


def test_walking_skeleton_full_chain():
    """CanonicalInput -> Spec -> DecisionRecord -> WorkResult -> OutcomeRecord."""
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    agent = _agent(_pack(), store, bus)

    art = _canonical({"q": "kickoff"}, source_refs=("src://42",))
    store.write(art)
    for kind in ("Spec", "DecisionRecord", "WorkResult", "OutcomeRecord"):
        art = agent.run(art, kind)
        assert art is not None and art.kind == kind

    root = store.by_kind("CanonicalInput")[0]
    assert store.is_terminal_done(root.id)
    assert "src://42" in art.provenance.source_refs
    # Every model-produced artifact carries model identity.
    assert art.provenance.model_ref.served_model_id == "echo-test"
