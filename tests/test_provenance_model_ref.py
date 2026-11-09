"""Model identity is stamped into provenance and round-trips."""

import json

from omli.artifacts import ModelRef, Provenance, new_artifact, succeed
from omli.artifacts.schema import Artifact


def _prov(model_ref=None):
    return Provenance(
        agent="TestAgent",
        pack_name="example",
        pack_version="0.1.0",
        source_refs=("src://1",),
        model_ref=model_ref,
    )


def _ref():
    return ModelRef(
        model_id="example-4b-instruct",
        registry_version="1.0.0",
        pin="sha256:" + "ab" * 32,
        quant="Q4_K_M",
        reproducible=True,
        served_model_id="example-served",
    )


def test_model_ref_defaults_to_none_and_serializes_none():
    art = new_artifact("CanonicalInput", {"a": 1}, _prov())
    assert art.provenance.model_ref is None
    assert art.to_dict()["provenance"]["model_ref"] is None


def test_succeed_threads_model_ref_into_provenance():
    root = new_artifact("CanonicalInput", {"a": 1}, _prov())
    spec = succeed(
        root,
        "Spec",
        {"text": "x"},
        agent="TestAgent",
        pack_name="example",
        pack_version="0.1.0",
        model_ref=_ref(),
    )
    assert spec.provenance.model_ref.served_model_id == "example-served"


def test_model_ref_round_trips_through_to_dict_and_from_dict():
    art = new_artifact("Spec", {"text": "x"}, _prov(model_ref=_ref()))
    data = art.to_dict()
    assert data["provenance"]["model_ref"] == {
        "model_id": "example-4b-instruct",
        "registry_version": "1.0.0",
        "pin": "sha256:" + "ab" * 32,
        "quant": "Q4_K_M",
        "reproducible": True,
        "served_model_id": "example-served",
    }
    restored = Artifact.from_dict(json.loads(json.dumps(data)))
    assert restored.provenance.model_ref == _ref()
    assert restored == art
