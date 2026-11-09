"""Live integration: a real Ollama completion writes a real OutcomeRecord.

Every OTHER Ollama test in the suite injects a fake transport so the suite runs fully
offline. This one deliberately hits a *running* server through the same gated Agent path
(an OllamaProvider with no injected transport), and confirms the completion is persisted as
a real terminal artifact with the live model stamped into provenance.

It is opt-in: the whole module is SKIPPED, before any network call, unless OMLI_LIVE_OLLAMA=1.
When opted in it is also server-gated: with nothing reachable on localhost:11434 (or no model
pulled) it skips too. That probe is a never-raising GET of `/api/tags`, the endpoint
OllamaModel.available() checks.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from omli.agents.base import Agent, _stringify
from omli.artifacts import ArtifactStore, Provenance, new_artifact
from omli.models.provider import OllamaProvider
from omli.models.registry import parse_registry
from omli.packs.schema import ModelSelection, Pack

if os.environ.get("OMLI_LIVE_OLLAMA") != "1":
    pytest.skip("live Ollama test is opt-in: set OMLI_LIVE_OLLAMA=1", allow_module_level=True)

HOST = "http://localhost:11434"


def _server_models(host: str = HOST, timeout: float = 3.0):
    """Return the list of pulled models, or None if no server is reachable (never raises)."""
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    models = [m for m in (data.get("models") or []) if m.get("digest")]
    return models or None


_MODELS = _server_models()

pytestmark = pytest.mark.skipif(
    not _MODELS,
    reason="no reachable Ollama server with a pulled model on localhost:11434",
)


def _pick_model(models):
    """The smallest pulled model by reported size; a model with no reported size sorts last."""
    return min(models, key=lambda m: m.get("size") if isinstance(m.get("size"), int) else float("inf"))


def _live_registry(model):
    tag = model.get("name") or model.get("model")
    digest = str(model.get("digest", "")).split(":")[-1]  # 64-hex, tolerate a prefix
    return parse_registry({
        "metadata": {
            "registry_id": "live_registry",
            "registry_version": "1.0.0",
            "authored_at": "2026-07-05T00:00:00Z",
            "authored_by": "test",
            "status": "staging",
        },
        "models": [{
            "model_id": "live-ollama",
            "display_name": "Live Ollama",
            "family": "live",
            "provider_kind": "local-ollama",
            "adapter": "ollama",
            "endpoint": HOST,
            "serving_tag": tag,
            "pin": {"ref": "sha256:" + digest, "quant": None},
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
            # Bound the completion so a thinking model can't run away with the test.
            "params_default": {"temperature": 0.0, "max_tokens": 200},
        }],
    })


def _outcome_pack():
    return Pack(
        name="live-outcome",
        version="0.1.0",
        identity="You finalize a recommendation. Reply with a single plain-English line.",
        prompt_template="Summarize this as one short line:\n{input}",
        required_output_fields=("text",),
        model_selection=ModelSelection(model_id="live-ollama"),
    )


def test_live_ollama_completion_writes_a_real_outcome_record(tmp_path):
    model = _pick_model(_MODELS)
    registry = _live_registry(model)
    store_path: Path = tmp_path / "artifacts.jsonl"
    store = ArtifactStore.from_log(store_path)

    # Seed a WorkResult; the live model advances it to the terminal OutcomeRecord.
    work = new_artifact(
        "WorkResult",
        {"text": "The quick brown fox jumps over the lazy dog and then rests in the shade."},
        Provenance(agent="Seed", pack_name="seed", pack_version="0.1.0",
                   source_refs=("file:///live-test",)),
    )
    store.write(work)

    # Real provider table: no injected transport => OllamaProvider hits the live server.
    providers = {"ollama": OllamaProvider(timeout_s=180.0)}
    agent = Agent(_outcome_pack(), registry, providers, store)

    out = agent.run(work, "OutcomeRecord")

    # A real terminal artifact was produced and persisted (not gated out, not an echo).
    assert out is not None, "live model returned nothing / was gated out"
    assert out.kind == "OutcomeRecord"
    assert isinstance(out.payload.get("text"), str)
    assert out.payload["text"] != _stringify(work)  # a real completion, not the input echoed

    # Provenance carries the model that actually produced it, with the live-reported id.
    mref = out.provenance.model_ref
    assert mref is not None
    assert mref.model_id == "live-ollama"
    assert mref.reproducible is True
    assert mref.served_model_id, "server did not report a served model id"

    # Durable: reload from disk and confirm the chain reached its terminal artifact.
    reloaded = ArtifactStore.from_log(store_path)
    assert reloaded.is_terminal_done(work.id)
    outcomes = reloaded.by_kind("OutcomeRecord")
    assert len(outcomes) == 1 and outcomes[0].id == out.id
