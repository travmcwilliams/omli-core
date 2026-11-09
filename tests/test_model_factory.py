"""make_model resolves a ModelSpec to the right adapter."""

import pytest

from omli.models.factory import make_model
from omli.models.local import LocalEchoModel
from omli.models.ollama import OllamaModel
from omli.models.spec import ModelSpec


def test_factory_builds_echo():
    model = make_model(ModelSpec(provider="echo", model="stub-local"))
    assert isinstance(model, LocalEchoModel)
    assert model.name == "stub-local"


def test_factory_builds_ollama():
    spec = ModelSpec(
        provider="ollama",
        model="example-model:8b",
        host="http://localhost:11434",
        timeout_s=30.0,
    )
    model = make_model(spec)
    assert isinstance(model, OllamaModel)
    assert model.name == "example-model:8b"
    assert model.host == "http://localhost:11434"
    assert model.timeout_s == 30.0


def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError):
        make_model(ModelSpec(provider="does-not-exist"))
