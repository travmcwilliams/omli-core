"""Provider/adapter boundary — offline via injected transports."""

import json

import pytest

from omli.models.errors import ModelError, ModelTimeout, ModelUnavailable
from omli.models.provider import (
    EchoProvider,
    Message,
    ModelRequest,
    OllamaProvider,
    OpenAICompatibleProvider,
    SamplingParams,
)
from omli.models.registry import parse_registry


def _entry(model_id, *, provider_kind, adapter, endpoint, ref, quant, reproducible, serving_tag=None):
    reg = parse_registry(
        {
            "metadata": {
                "registry_id": "t",
                "registry_version": "1.0.0",
                "authored_at": "2026-06-17T00:00:00Z",
                "authored_by": "t",
                "status": "staging",
            },
            "models": [
                {
                    "model_id": model_id,
                    "display_name": model_id,
                    "family": "f",
                    "provider_kind": provider_kind,
                    "adapter": adapter,
                    "endpoint": endpoint,
                    "serving_tag": serving_tag,
                    "pin": {"ref": ref, "quant": quant},
                    "reproducible": reproducible,
                    "status": "staging",
                    "capabilities": {
                        "context_window": 8192,
                        "max_output_tokens": 2048,
                        "native_tool_calling": True,
                        "parallel_tool_calls": False,
                        "structured_output": "json_mode",
                        "thinking_mode": False,
                        "vision": False,
                    },
                    "params_default": {"temperature": 0.1},
                }
            ],
        }
    ).get(model_id)
    return reg


def _local_entry():
    return _entry(
        "example-4b-instruct",
        provider_kind="local-ollama",
        adapter="ollama",
        endpoint="http://localhost:11434",
        ref="sha256:" + "cd" * 32,
        quant="Q4_K_M",
        reproducible=True,
        serving_tag="example-model:4b",
    )


def _hosted_entry():
    return _entry(
        "hosted-example",
        provider_kind="openai-compatible",
        adapter="openai-compatible",
        endpoint="https://api.example.com/v1",
        ref="hosted-example-2026-05",
        quant=None,
        reproducible=False,
    )


def _req(user="hello"):
    return ModelRequest(
        messages=(Message("system", "you are a test"), Message("user", user)),
        params=SamplingParams(temperature=0.0),
    )


def _transport(response=None, exc=None, capture=None):
    def transport(url, body, timeout):
        if capture is not None:
            capture.append((url, body, timeout))
        if exc is not None:
            raise exc
        return response

    return transport


def test_echo_provider_returns_served_model_id():
    resp = EchoProvider().invoke(_local_entry(), _req("ping"))
    assert resp.served_model_id == "example-4b-instruct"
    assert "ping" in resp.text
    assert resp.finish_reason == "stop"


def test_ollama_provider_happy_path_and_request_shape():
    cap = []
    body = json.dumps(
        {"model": "example-served", "message": {"role": "assistant", "content": "hi there"},
         "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 2}
    ).encode("utf-8")
    resp = OllamaProvider(transport=_transport(response=body, capture=cap)).invoke(_local_entry(), _req())

    assert resp.text == "hi there"
    assert resp.served_model_id == "example-served"  # what the endpoint reported
    assert resp.input_tokens == 5 and resp.output_tokens == 2
    url, sent, _ = cap[0]
    assert url == "http://localhost:11434/api/chat"
    payload = json.loads(sent.decode("utf-8"))
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["stream"] is False
    # Ollama is called by its serving TAG, never the sha256 digest.
    assert payload["model"] == "example-model:4b"


def test_ollama_verify_digest_success_records_pinned_digest():
    pinned = "sha256:" + "cd" * 32

    def transport(url, body, timeout):
        if url.endswith("/api/tags"):
            return json.dumps({"models": [{"name": "example-model:4b", "digest": pinned}]}).encode("utf-8")
        return json.dumps({"model": "example-model:4b", "message": {"content": "ok"}}).encode("utf-8")

    resp = OllamaProvider(transport=transport, verify_digest=True).invoke(_local_entry(), _req())
    # served_model_id is the confirmed DIGEST, not the tag — the drift-proof identity.
    assert resp.served_model_id == pinned


def test_ollama_verify_digest_mismatch_raises():
    def transport(url, body, timeout):
        if url.endswith("/api/tags"):
            return json.dumps({"models": [{"name": "example-model:4b", "digest": "sha256:" + "00" * 32}]}).encode("utf-8")
        return json.dumps({"model": "example-model:4b", "message": {"content": "ok"}}).encode("utf-8")

    with pytest.raises(ModelError, match="drifted"):
        OllamaProvider(transport=transport, verify_digest=True).invoke(_local_entry(), _req())


def test_ollama_verify_digest_missing_tag_raises():
    def transport(url, body, timeout):
        if url.endswith("/api/tags"):
            return json.dumps({"models": [{"name": "other:tag", "digest": "sha256:" + "cd" * 32}]}).encode("utf-8")
        return json.dumps({"model": "example-model:4b", "message": {"content": "ok"}}).encode("utf-8")

    with pytest.raises(ModelError, match="not found"):
        OllamaProvider(transport=transport, verify_digest=True).invoke(_local_entry(), _req())


def test_ollama_provider_normalizes_tool_calls():
    body = json.dumps(
        {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "lookup", "arguments": {"q": "x"}}}],
            },
        }
    ).encode("utf-8")
    resp = OllamaProvider(transport=_transport(response=body)).invoke(_local_entry(), _req())
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls[0].name == "lookup"
    assert resp.tool_calls[0].arguments == {"q": "x"}


def test_ollama_provider_transport_errors_are_typed():
    with pytest.raises(ModelUnavailable):
        OllamaProvider(transport=_transport(exc=ConnectionRefusedError())).invoke(_local_entry(), _req())
    with pytest.raises(ModelTimeout):
        OllamaProvider(transport=_transport(exc=TimeoutError())).invoke(_local_entry(), _req())


def test_openai_compatible_provider_parses_choices():
    body = json.dumps(
        {
            "model": "hosted-example-2026-05",
            "choices": [{"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        }
    ).encode("utf-8")
    cap = []
    resp = OpenAICompatibleProvider(transport=_transport(response=body, capture=cap)).invoke(_hosted_entry(), _req())
    assert resp.text == "answer"
    assert resp.served_model_id == "hosted-example-2026-05"
    assert resp.input_tokens == 9 and resp.output_tokens == 3
    url, _, _ = cap[0]
    assert url == "https://api.example.com/v1/chat/completions"
