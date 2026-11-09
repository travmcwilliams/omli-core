"""Primitive 4: artifacts are immutable and append-only; successors link predecessors.

Also guards Primitive 3's corollary that the store exposes no mutators.
"""

import dataclasses

import pytest

from omli.artifacts import ArtifactStore, Provenance, new_artifact, succeed


def _prov(**kw):
    base = dict(agent="TestAgent", pack_name="example", pack_version="0.1.0")
    base.update(kw)
    return Provenance(**base)


def test_artifact_attributes_are_frozen():
    art = new_artifact("CanonicalInput", {"a": 1}, _prov(source_refs=("src://1",)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        art.kind = "Spec"  # type: ignore[misc]


def test_artifact_payload_is_read_only():
    art = new_artifact("CanonicalInput", {"a": 1}, _prov())
    with pytest.raises(TypeError):
        art.payload["a"] = 2  # type: ignore[index]


def test_successor_links_to_predecessor_and_inherits_sources():
    root = new_artifact("CanonicalInput", {"a": 1}, _prov(source_refs=("src://1",)))
    spec = succeed(
        root,
        "Spec",
        {"b": 2},
        agent="TestAgent",
        pack_name="example",
        pack_version="0.1.0",
    )
    assert spec.provenance.predecessor_ids == (root.id,)
    assert "src://1" in spec.provenance.source_refs


def test_successor_must_move_forward_in_chain():
    spec = new_artifact("Spec", {"b": 2}, _prov())
    with pytest.raises(ValueError):
        # Spec -> CanonicalInput goes backwards; not allowed.
        succeed(
            spec,
            "CanonicalInput",
            {"a": 1},
            agent="TestAgent",
            pack_name="example",
            pack_version="0.1.0",
        )


def test_store_is_append_only_and_has_no_mutators():
    store = ArtifactStore()
    art = new_artifact("CanonicalInput", {"a": 1}, _prov())
    store.write(art)

    # Writing the same id twice is rejected.
    with pytest.raises(ValueError):
        store.write(art)

    # The store deliberately exposes no mutation API.
    for forbidden in ("update", "delete", "remove", "set_status", "mutate"):
        assert not hasattr(store, forbidden), f"store must not expose {forbidden}()"


def test_store_answers_state_from_artifacts_alone():
    store = ArtifactStore()
    root = new_artifact("CanonicalInput", {"a": 1}, _prov(source_refs=("src://1",)))
    store.write(root)
    assert store.has_source("src://1")
    assert not store.is_terminal_done(root.id)

    outcome = succeed(
        root,
        "OutcomeRecord",
        {"done": True},
        agent="TestAgent",
        pack_name="example",
        pack_version="0.1.0",
    )
    store.write(outcome)
    assert store.successors(root.id) == (outcome,)
    assert store.is_terminal_done(root.id)
