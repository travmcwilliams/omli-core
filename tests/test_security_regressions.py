"""Fail-closed checks: unsafe secret references, local model kinds with non-loopback endpoints,
unknown sensitivity tags, connection entries the loader did not produce, and endpoints the agent
re-checks before a model call.

The secret-store fixture points the module-private `secrets._SECRETS_DIR` at a temporary
directory. No test here opens a socket.
"""

from __future__ import annotations

import copy
import dataclasses
import os
from pathlib import Path

import pytest
import yaml

EXAMPLE = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"


def _example():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- secret://
@pytest.fixture
def secrets_store(monkeypatch, tmp_path):
    from omli.connections import secrets

    store = tmp_path / "secrets"
    store.mkdir()
    (tmp_path / "outside.txt").write_text("outside-value", encoding="utf-8")
    monkeypatch.setattr(secrets, "_SECRETS_DIR", store)
    return secrets


@pytest.mark.parametrize("ref", [
    "secret://../outside.txt",
    "secret://a/../../outside.txt",
    "secret://./x",
    "secret://a//b",
    "secret://a/",
    "secret:///abs/path",
    "secret://",
    "secret://C:/x",
    "secret://a\\\\..\\\\..\\\\outside.txt",
], ids=["parent", "nested-parent", "dot", "empty-mid", "empty-end", "absolute", "empty", "drive", "backslash"])
def test_unsafe_secret_ref_is_refused_at_resolve(secrets_store, ref):
    with pytest.raises(ValueError):
        secrets_store.resolve_secret(ref)


@pytest.mark.parametrize("ref", ["secret://../outside.txt", "secret://a/../../x", "secret:///abs"],
                         ids=["parent", "nested-parent", "absolute"])
def test_unsafe_secret_ref_is_refused_at_registry_parse(ref):
    from omli.connections.schema import parse_registry

    data = _example()
    target = next(c["target"] for c in data["connections"] if c["target"].get("credential_ref"))
    target["credential_ref"] = ref
    with pytest.raises(ValueError):
        parse_registry(data)


def test_symlink_cannot_carry_a_ref_out_of_the_store(secrets_store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("value-outside-the-store", encoding="utf-8")
    try:
        (secrets_store._SECRETS_DIR / "link").symlink_to(outside)
    except OSError:  # e.g. Windows without the symlink privilege or developer mode
        pytest.skip("this account cannot create a symlink")
    with pytest.raises(ValueError) as caught:
        secrets_store.resolve_secret("secret://link")
    assert "value-outside-the-store" not in str(caught.value)
    assert "secret://link" not in str(caught.value)


# ----------------------------------------------------------- local kinds are loopback
def _local_model(endpoint, kind="local-ollama"):
    entry = {
        "model_id": "m", "display_name": "m", "family": "t", "provider_kind": kind, "adapter": "a",
        "endpoint": endpoint, "status": "staging",
        "capabilities": {"context_window": 8192, "max_output_tokens": 2048, "native_tool_calling": False,
                         "parallel_tool_calls": False, "structured_output": "none", "thinking_mode": False,
                         "vision": False},
    }
    if kind == "local-ollama":
        entry.update(serving_tag="m:t", pin={"ref": "sha256:" + "ab" * 32, "quant": None}, reproducible=True)
    else:
        entry.update(pin={"ref": "b3c9ce6f29ad4161a00fa58a62e476b9c75ca934", "quant": None}, reproducible=True)
    return {"metadata": {"registry_id": "r", "registry_version": "0.1.0", "authored_at": "2026-11-06T00:00:00Z",
                         "authored_by": "t", "status": "staging"}, "models": [entry]}


@pytest.mark.parametrize("endpoint", [
    "https://api.example.invalid/v1",
    "http://localhost.example.invalid:11434",
    "http://127.0.0.1.example.invalid",
    "http://localhost@example.invalid",
    "http://localhost:11434@example.invalid",
    "http://example.invalid\\\\@localhost",
    "http://example.invalid#@localhost",
    "http://0.0.0.0:11434",
    "http://10.0.0.1:11434",
    "http://[::ffff:10.0.0.1]:11434",
    "http://2130706433",
    "ftp://localhost",
    "Some-Org/Some-Model",
], ids=["remote", "suffix-host", "ip-suffix-host", "userinfo", "userinfo-port", "backslash", "fragment",
        "any-address", "private-ip", "mapped-ip", "decimal-ip", "ftp", "repo-id"])
def test_local_kind_with_non_loopback_endpoint_is_rejected(endpoint):
    from omli.models.registry import parse_registry

    with pytest.raises(ValueError, match="loopback"):
        parse_registry(_local_model(endpoint))


def test_local_diffusers_with_a_disk_path_is_rejected():
    from omli.models.registry import parse_registry

    with pytest.raises(ValueError, match="loopback"):
        parse_registry(_local_model("X:/models/weights", kind="local-diffusers"))


# ---------------------------------------------------------- sensitivity tags are strict
def _prov(tags):
    from omli.artifacts import Provenance

    return Provenance(agent="A", pack_name="p", pack_version="0.1.0", source_refs=("demo://1",), sensitivity=tags)


@pytest.mark.parametrize("tag", ["no-such-tag", "local_only", "localonly", "local-only:", "local-only: x", "Not A Tag"])
def test_unknown_sensitivity_tag_is_rejected_at_provenance_creation(tag):
    with pytest.raises(ValueError, match="sensitivity"):
        _prov((tag,))


def test_unknown_tag_is_rejected_when_a_stored_artifact_is_read():
    from omli.artifacts import new_artifact
    from omli.artifacts.schema import Artifact

    record = new_artifact("CanonicalInput", {"q": "x"}, _prov(())).to_dict()
    record["provenance"]["sensitivity"] = ["no-such-tag"]
    with pytest.raises(ValueError, match="sensitivity"):
        Artifact.from_dict(record)


@pytest.mark.parametrize("given, stored", [("LOCAL-ONLY:Demo", "local-only:demo"), ("Local-Only", "local-only"),
                                           ("PUBLIC", "public")])
def test_sensitivity_tags_are_stored_lowercase(given, stored):
    assert _prov((given,)).sensitivity == (stored,)


@pytest.mark.parametrize("tag", ["local-only", "LOCAL-ONLY", "Local-Only", " local-only ", "LOCAL-ONLY:demo"])
def test_local_only_variants_block_a_hosted_kind(tag):
    from omli.sensitivity import blocked_nonlocal_model_tags

    assert blocked_nonlocal_model_tags((tag,), "openai-compatible")


def test_bare_local_only_blocks_a_real_agent_before_the_provider_is_called():
    from omli.agents.base import Agent
    from omli.artifacts import ArtifactStore, new_artifact
    from omli.events.bus import EventBus
    from omli.models.provider import ModelProvider, ModelResponse
    from omli.models.registry import parse_registry
    from omli.packs.schema import ModelSelection, Pack

    class Spy(ModelProvider):
        key = "spy"
        calls = 0

        def invoke(self, entry, request):
            Spy.calls += 1
            return ModelResponse(text="x", served_model_id=entry.model_id)

    data = _local_model("https://api.example.invalid/v1")
    model = data["models"][0]
    model.update(provider_kind="openai-compatible", adapter="spy", pin={"ref": "m-2026-11-06"}, reproducible=False)
    model.pop("serving_tag")
    pack = Pack(name="p", version="0.1.0", identity="demo", prompt_template="{input}",
                required_output_fields=("text", "produced_by_pack"), model_selection=ModelSelection(model_id="m"))
    bus = EventBus()
    agent = Agent(pack, parse_registry(data), {"spy": Spy()}, ArtifactStore(bus=bus), bus=bus)
    assert agent.run(new_artifact("CanonicalInput", {"q": "x"}, _prov(("local-only",))), "Spec") is None
    assert Spy.calls == 0
    assert "blocked_pre" in bus.names()


# ------------------------------------------------- dispatch and read trust the loader only
class _Spy:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return {"ok": True, "messages": []}


def _slack_entry():
    from omli.connections.schema import parse_registry

    entry = next(c for c in parse_registry(_example()).connections if c.target.kind == "slack")
    return entry


def _row(entry, operation):
    return next(r for r in entry.selection.resources if r.selected and operation in r.operations)


def _variants(entry):
    fields = {f.name: getattr(entry, f.name) for f in dataclasses.fields(entry)}
    return {
        "constructed": type(entry)(**fields),
        "replaced": dataclasses.replace(entry),
        "deep-copied": copy.deepcopy(entry),
    }


@pytest.mark.parametrize("variant", ["constructed", "replaced", "deep-copied"])
def test_dispatch_refuses_an_entry_the_loader_did_not_produce(variant):
    from omli.connections.scope_gate import ConnectionAction
    from omli.connections.sender import dispatch

    loaded = _slack_entry()
    action = ConnectionAction(loaded.connection_id, "post", path=_row(loaded, "post").id,
                              sensitivity_tags=("public",))
    control = _Spy()  # the loaded entry, with the same action, does reach the wire
    assert dispatch(loaded, action, {"text": "x"}, senders={"slack": control},
                    resolve=lambda *a, **k: "fake").ok is True and control.calls == 1
    spy = _Spy()
    out = dispatch(_variants(loaded)[variant], action, {"text": "x"}, senders={"slack": spy},
                   resolve=lambda *a, **k: "fake")
    assert spy.calls == 0
    assert out.ok is False and out.decision == "deny" and out.reason == "entry-not-registry-loaded"


@pytest.mark.parametrize("variant", ["constructed", "replaced", "deep-copied"])
def test_read_refuses_an_entry_the_loader_did_not_produce(variant):
    from omli.connections.reader import read

    loaded = _slack_entry()
    channel = _row(loaded, "read-thread").id
    control = _Spy()  # the loaded entry, with the same read, does reach the wire
    assert read(loaded, channel, {}, sensitivity_tags=("public",), readers={"slack": control},
                resolve=lambda *a, **k: "fake").ok is True and control.calls == 1
    spy = _Spy()
    out = read(_variants(loaded)[variant], channel, {}, sensitivity_tags=("public",),
               readers={"slack": spy}, resolve=lambda *a, **k: "fake")
    assert spy.calls == 0
    assert out.ok is False and out.decision == "deny" and out.reason == "entry-not-registry-loaded"


def test_dispatch_refuses_a_loaded_entry_changed_in_place():
    from omli.connections.scope_gate import ConnectionAction
    from omli.connections.sender import dispatch

    loaded = _slack_entry()
    keep_alive = loaded
    object.__setattr__(keep_alive, "connection_id", loaded.connection_id + "-changed")
    spy = _Spy()
    action = ConnectionAction(keep_alive.connection_id, "post", path=_row(keep_alive, "post").id,
                              sensitivity_tags=("public",))
    out = dispatch(keep_alive, action, {"text": "x"}, senders={"slack": spy}, resolve=lambda *a, **k: "fake")
    assert spy.calls == 0
    assert out.ok is False and out.decision == "deny" and out.reason == "entry-not-registry-loaded"


def test_dispatch_fails_closed_when_the_action_names_another_connection():
    from omli.connections.scope_gate import ConnectionAction, connection_scope_gate
    from omli.connections.sender import dispatch

    loaded = _slack_entry()
    path = _row(loaded, "post").id
    control = _Spy()  # the same action under the entry's own id does reach the wire
    matching = ConnectionAction(loaded.connection_id, "post", path=path, sensitivity_tags=("public",))
    assert dispatch(loaded, matching, {"text": "x"}, senders={"slack": control},
                    resolve=lambda *a, **k: "fake").ok is True and control.calls == 1
    spy = _Spy()
    action = ConnectionAction("some-other-connection", "post", path=path, sensitivity_tags=("public",))
    assert connection_scope_gate(action, loaded).reason.startswith("connection-id-mismatch")
    out = dispatch(loaded, action, {"text": "x"}, senders={"slack": spy}, resolve=lambda *a, **k: "fake")
    assert spy.calls == 0
    # The outcome carries the closed-set code, which reduces connection-id-mismatch to gate-refused.
    assert out.ok is False and out.decision == "deny" and out.reason == "gate-refused"


# ------------------------------------------- the agent re-checks the endpoint before a call
class _CountingProvider:
    key = "counting"

    def __init__(self, text="x"):
        self.text = text
        self.calls = 0

    def invoke(self, entry, request):
        from omli.models.provider import ModelResponse

        self.calls += 1
        return ModelResponse(text=self.text, served_model_id=entry.model_id)


def _hand_built_registry(*extra):
    from omli.models.registry import ModelRegistry, parse_registry

    base = parse_registry(_local_model("http://localhost:11434"))
    remote = dataclasses.replace(base.models[0], endpoint="https://api.example.invalid/v1")
    return ModelRegistry(metadata=base.metadata, models=(remote,) + extra), base.models[0]


def _agent_for(registry, providers, **selection):
    from omli.agents.base import Agent
    from omli.artifacts import ArtifactStore
    from omli.events.bus import EventBus
    from omli.packs.schema import ModelSelection, Pack

    pack = Pack(name="p", version="0.1.0", identity="demo", prompt_template="{input}",
                required_output_fields=("text", "produced_by_pack"), model_selection=ModelSelection(**selection))
    bus = EventBus()
    return Agent(pack, registry, providers, ArtifactStore(bus=bus), bus=bus), bus


@pytest.mark.parametrize("tags", [(), ("local-only",)], ids=["untagged", "local-only"])
def test_hand_built_registry_with_remote_local_endpoint_gets_no_provider_call(tags):
    from omli.artifacts import new_artifact

    registry, _ = _hand_built_registry()
    provider = _CountingProvider()
    agent, bus = _agent_for(registry, {"a": provider}, model_id="m")
    assert agent.run(new_artifact("CanonicalInput", {"q": "x"}, _prov(tags)), "Spec") is None
    assert provider.calls == 0
    assert "blocked_pre" in bus.names()


def test_hand_built_escalation_with_remote_local_endpoint_gets_no_provider_call():
    from omli.artifacts import new_artifact
    from omli.models.registry import ModelRegistry
    from omli.packs.schema import Escalation

    _, good = _hand_built_registry()
    remote = dataclasses.replace(good, model_id="esc", adapter="b", endpoint="https://api.example.invalid/v1")
    local = dataclasses.replace(good, adapter="a")
    registry = ModelRegistry(metadata=_hand_built_registry()[0].metadata, models=(local, remote))
    primary, rescue = _CountingProvider(text=""), _CountingProvider()
    agent, bus = _agent_for(registry, {"a": primary, "b": rescue}, model_id="m",
                            escalation=Escalation(model_id="esc", trigger="gate_rejection", max_escalations=1))
    assert agent.run(new_artifact("CanonicalInput", {"q": "x"}, _prov(())), "Spec") is None
    assert primary.calls == 1 and rescue.calls == 0
    assert "escalated" not in bus.names()


# ----------------------------------------------------- connection tags match provenance tags
def test_a_provenance_tag_passes_the_gate_for_an_allowed_connection():
    from omli.artifacts import new_artifact
    from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

    entry = _slack_entry()
    artifact = new_artifact("CanonicalInput", {"q": "x"}, _prov(("internal",)))
    action = ConnectionAction(entry.connection_id, "post", path=_row(entry, "post").id,
                              sensitivity_tags=artifact.provenance.sensitivity)
    assert connection_scope_gate(action, entry).decision == "allow"


def test_action_tags_are_normalized_before_the_gate():
    from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

    entry = _slack_entry()
    action = ConnectionAction(entry.connection_id, "post", path=_row(entry, "post").id, sensitivity_tags=("INTERNAL",))
    assert action.sensitivity_tags == ("internal",)
    assert connection_scope_gate(action, entry).decision == "allow"


def test_unknown_action_tag_is_rejected():
    from omli.connections.scope_gate import ConnectionAction

    with pytest.raises(ValueError, match="sensitivity"):
        ConnectionAction("c", "post", path="x", sensitivity_tags=("no-such-tag",))


def test_unknown_sensitivity_allowed_tag_is_rejected_at_parse():
    from omli.connections.schema import parse_registry

    data = _example()
    data["connections"][0]["target"]["sensitivity_allowed"] = ["no-such-tag"]
    with pytest.raises(ValueError, match="sensitivity"):
        parse_registry(data)


def test_sensitivity_allowed_is_stored_lowercase():
    from omli.connections.schema import parse_registry

    data = _example()
    data["connections"][0]["target"]["sensitivity_allowed"] = ["PUBLIC", "Internal"]
    assert parse_registry(data).connections[0].target.sensitivity_allowed == ("public", "internal")
