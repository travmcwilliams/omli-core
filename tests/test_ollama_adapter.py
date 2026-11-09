"""OllamaModel adapter — fully offline via an injected transport.

Covers the happy path, the exact request shape, and normalization of transport failures
to the typed errors the Agent routes on.
"""

import json

import pytest

from omli.models.errors import ModelError, ModelTimeout, ModelUnavailable
from omli.models.ollama import OllamaModel


def _transport(response=None, exc=None, capture=None):
    def transport(url, body, timeout):
        if capture is not None:
            capture.append((url, body, timeout))
        if exc is not None:
            raise exc
        return response

    return transport


def test_generate_happy_path():
    body = json.dumps({"response": "hello world", "done": True}).encode("utf-8")
    model = OllamaModel(model="example-model:8b", transport=_transport(response=body))
    assert model.generate("hi") == "hello world"


def test_generate_sends_expected_request():
    cap = []
    body = json.dumps({"response": "ok"}).encode("utf-8")
    model = OllamaModel(
        model="example-model:8b",
        host="http://localhost:11434",
        transport=_transport(response=body, capture=cap),
    )
    model.generate("the prompt")
    url, sent, _ = cap[0]
    assert url == "http://localhost:11434/api/generate"
    payload = json.loads(sent.decode("utf-8"))
    assert payload == {"model": "example-model:8b", "prompt": "the prompt", "stream": False}


def test_timeout_maps_to_model_timeout():
    model = OllamaModel(transport=_transport(exc=TimeoutError("slow")))
    with pytest.raises(ModelTimeout):
        model.generate("hi")


def test_connection_refused_maps_to_unavailable():
    model = OllamaModel(transport=_transport(exc=ConnectionRefusedError("nope")))
    with pytest.raises(ModelUnavailable):
        model.generate("hi")


def test_bad_json_maps_to_model_error():
    model = OllamaModel(transport=_transport(response=b"not json"))
    with pytest.raises(ModelError):
        model.generate("hi")


def test_missing_response_field_maps_to_model_error():
    body = json.dumps({"done": True}).encode("utf-8")
    model = OllamaModel(transport=_transport(response=body))
    with pytest.raises(ModelError):
        model.generate("hi")


def test_available_true_and_false():
    up = OllamaModel(transport=_transport(response=b'{"models": []}'))
    assert up.available() is True
    down = OllamaModel(transport=_transport(exc=ConnectionRefusedError()))
    assert down.available() is False
