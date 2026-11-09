"""A route-control tag is structural, not procedural: it survives the chain and blocks hosted routes."""

import pytest

from omli.agents.base import Agent
from omli.artifacts import ArtifactStore, Provenance, new_artifact, succeed
from omli.connections.schema import parse_registry as parse_connection_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate
from omli.events.bus import EventBus
from omli.models.provider import ModelProvider, ModelResponse
from omli.models.registry import parse_registry
from omli.packs.schema import Escalation, ModelSelection, Pack

RESTRICTED_TAG = "local-only:demo"


def _prov(*, sensitivity=()):
    return Provenance(
        agent="Intake",
        pack_name="restricted-test",
        pack_version="0.1.0",
        source_refs=("demo://restricted/account/1",),
        sensitivity=sensitivity,
    )


def _canonical(*, sensitivity=(RESTRICTED_TAG,)):
    return new_artifact("CanonicalInput", {"q": "restricted row"}, _prov(sensitivity=sensitivity))


def _local_entry(model_id):
    return {
        "model_id": model_id,
        "display_name": model_id,
        "family": "test",
        "provider_kind": "local-ollama",
        "adapter": "local",
        "endpoint": "http://localhost:11434",
        "serving_tag": f"{model_id}:test",
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


def _hosted_entry(model_id):
    return {
        "model_id": model_id,
        "display_name": model_id,
        "family": "hosted-test",
        "provider_kind": "openai-compatible",
        "adapter": "hosted",
        "endpoint": "https://api.example.invalid/v1",
        "pin": {"ref": f"{model_id}-2026-08-02"},
        "reproducible": False,
        "status": "staging",
        "capabilities": {
            "context_window": 128000,
            "max_output_tokens": 4096,
            "native_tool_calling": True,
            "parallel_tool_calls": True,
            "structured_output": "json_mode",
            "thinking_mode": False,
            "vision": False,
        },
        "params_default": {"temperature": 0.0},
    }


def _registry(*, primary_local=True, escalation_hosted=False):
    models = [_local_entry("local-primary") if primary_local else _hosted_entry("hosted-primary")]
    if escalation_hosted:
        models.append(_hosted_entry("hosted-escalation"))
    return parse_registry(
        {
            "metadata": {
                "registry_id": "restricted_model_registry",
                "registry_version": "0.1.0",
                "authored_at": "2026-08-02T00:00:00Z",
                "authored_by": "test",
                "status": "staging",
            },
            "models": models,
        }
    )


def _pack(*, primary="local-primary", escalation=None):
    return Pack(
        name="restricted-test",
        version="0.1.0",
        identity="demo",
        prompt_template="{input}",
        required_output_fields=("text", "produced_by_pack"),
        model_selection=ModelSelection(
            model_id=primary,
            escalation=(
                Escalation(model_id=escalation, trigger="gate_rejection", max_escalations=1)
                if escalation
                else None
            ),
        ),
    )


class _RecordingProvider(ModelProvider):
    key = "recording"

    def __init__(self, text):
        self.text = text
        self.calls = []

    def invoke(self, entry, request):
        self.calls.append(entry.model_id)
        return ModelResponse(text=self.text, served_model_id=entry.model_id)


def test_restricted_origin_tag_accumulates_and_cannot_be_stripped():
    source_tags = [RESTRICTED_TAG]
    art = _canonical(sensitivity=source_tags)
    source_tags.append("public")
    assert art.provenance.sensitivity == (RESTRICTED_TAG,)

    for kind, added in (
        ("Spec", ("public",)),
        ("DecisionRecord", ()),
        ("WorkResult", ("public",)),
        ("OutcomeRecord", ()),
    ):
        art = succeed(
            art,
            kind,
            {"kind": kind},
            agent="Worker",
            pack_name="restricted-test",
            pack_version="0.1.0",
            sensitivity=added,
        )
        assert RESTRICTED_TAG in art.provenance.sensitivity

    assert art.provenance.sensitivity == (RESTRICTED_TAG, "public")
    assert new_artifact("CanonicalInput", {"q": "x"}, _prov(sensitivity=RESTRICTED_TAG)).provenance.sensitivity == (
        RESTRICTED_TAG,
    )


def test_restricted_payload_blocks_hosted_primary_before_external_provider_is_called():
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    hosted = _RecordingProvider("external answer")
    agent = Agent(
        _pack(primary="hosted-primary"),
        _registry(primary_local=False),
        {"hosted": hosted},
        store,
        bus=bus,
    )

    out = agent.run(_canonical(), "Spec")

    assert out is None
    assert hosted.calls == []
    assert "model_call" not in bus.names()
    blocked = next(e for e in bus.log() if e.name == "blocked_pre")
    assert blocked.payload["provider_kind"] == "openai-compatible"
    assert RESTRICTED_TAG in blocked.payload["reason"]


def test_restricted_payload_blocks_hosted_escalation_before_external_provider_is_called():
    bus = EventBus()
    store = ArtifactStore(bus=bus)
    local = _RecordingProvider("")
    hosted = _RecordingProvider("hosted rescue")
    agent = Agent(
        _pack(primary="local-primary", escalation="hosted-escalation"),
        _registry(primary_local=True, escalation_hosted=True),
        {"local": local, "hosted": hosted},
        store,
        bus=bus,
    )

    out = agent.run(_canonical(), "Spec")

    assert out is None
    assert local.calls == ["local-primary"]
    assert hosted.calls == []
    names = bus.names()
    assert names.count("model_call") == 1
    assert "escalated" not in names
    blocked = [e for e in bus.log() if e.name == "blocked_pre"][-1]
    assert blocked.payload["model"] == "hosted-escalation"
    assert RESTRICTED_TAG in blocked.payload["reason"]


def _egress_entry(*, sensitivity_allowed=("public",)):
    return parse_connection_registry(
        {
            "metadata": {
                "registry_id": "restricted_egress",
                "registry_version": "0.1.0",
                "authored_at": "2026-08-02T00:00:00Z",
                "authored_by": "test",
                "status": "draft",
            },
            "connections": [
                {
                    "connection_id": "restricted_egress",
                    "target": {
                        "kind": "container-egress",
                        "identifier": "restricted-egress",
                        "principal": "example-owner",
                        "sensitivity_allowed": list(sensitivity_allowed),
                    },
                    "selection": {
                        "mode": "allowlist",
                        "resources": [
                            {
                                "id": "api.allowed.example.com",
                                "kind": "domain",
                                "selected": True,
                                "operations": ["connect"],
                            }
                        ],
                    },
                    "blast_radius": {
                        "forbidden_paths": [],
                        "direct_push": False,
                        "destructive_ops": False,
                    },
                    "approval": {
                        "mode": "pr-only",
                        "required_reviewers": ["example-owner"],
                        "auto_merge": False,
                        "escalate_on": ["out-of-selection"],
                    },
                }
            ],
        }
    ).get("restricted_egress")


def test_restricted_sensitivity_and_unlisted_egress_never_allow_connection():
    selected_but_not_cleared = connection_scope_gate(
        ConnectionAction(
            "restricted_egress",
            "connect",
            path="api.allowed.example.com",
            sensitivity_tags=(RESTRICTED_TAG,),
        ),
        _egress_entry(),
    )
    assert selected_but_not_cleared.decision == "deny"
    assert "sensitivity-mismatch" in (selected_but_not_cleared.reason or "")

    unlisted = connection_scope_gate(
        ConnectionAction(
            "restricted_egress",
            "connect",
            path="api.unlisted.example.com",
            sensitivity_tags=(RESTRICTED_TAG,),
        ),
        _egress_entry(sensitivity_allowed=("public", RESTRICTED_TAG)),
    )
    assert unlisted.decision in ("deny", "escalate")
    assert "out-of-selection" in (unlisted.reason or "")
