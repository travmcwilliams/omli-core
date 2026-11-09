"""Blocking tags beyond the generic local-only rule are pack data, and only ever accumulate."""

from __future__ import annotations

from pathlib import Path

import pytest

from omli import sensitivity as S
from omli.artifacts import Provenance

EXAMPLE = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "sensitivity_policy.example.yaml"


@pytest.fixture(autouse=True)
def clean_policy(monkeypatch):
    monkeypatch.setattr(S, "_active", S.SensitivityPolicy())


def _policy(**over):
    data = {"policy": "sensitivity", "version": "1", "known_labels": [], "local_model_only_tags": []}
    data.update(over)
    return S.parse_policy(data)


def _prov(tags):
    return Provenance(agent="A", pack_name="p", pack_version="1", source_refs=("demo://1",), sensitivity=tags)


def test_core_has_no_blocking_tag_beyond_the_generic_rule():
    assert S.SensitivityPolicy().local_model_only_tags == ()
    assert S.blocked_nonlocal_model_tags(("public", "internal"), "openai-compatible") == ()


def test_shipped_example_pack_adds_no_tags():
    policy = S.load_policy(EXAMPLE)
    assert policy.known_labels == () and policy.local_model_only_tags == ()


def test_a_pack_tag_is_rejected_until_its_policy_is_active():
    with pytest.raises(ValueError, match="unknown sensitivity"):
        _prov(("restricted-lane",))
    S.activate_policy(_policy(local_model_only_tags=["restricted-lane"]))
    assert _prov(("RESTRICTED-LANE",)).sensitivity == ("restricted-lane",)


def test_a_pack_blocking_tag_blocks_a_hosted_kind_but_not_a_local_one():
    S.activate_policy(_policy(local_model_only_tags=["restricted-lane"]))
    assert S.blocked_nonlocal_model_tags(("restricted-lane",), "openai-compatible") == ("restricted-lane",)
    assert S.blocked_nonlocal_model_tags(("restricted-lane",), "local-ollama") == ()


def test_a_pack_label_is_known_but_does_not_block():
    S.activate_policy(_policy(known_labels=["team-only"]))
    assert _prov(("team-only",)).sensitivity == ("team-only",)
    assert S.blocked_nonlocal_model_tags(("team-only",), "openai-compatible") == ()


def test_activating_a_later_policy_cannot_remove_a_blocking_tag():
    S.activate_policy(_policy(local_model_only_tags=["restricted-lane"]))
    S.activate_policy(_policy())
    assert S.blocked_nonlocal_model_tags(("restricted-lane",), "openai-compatible")


def test_a_policy_cannot_switch_off_the_generic_rule():
    S.activate_policy(_policy(known_labels=["local-only"]))
    assert S.blocked_nonlocal_model_tags(("local-only",), "openai-compatible")
    assert S.blocked_nonlocal_model_tags(("local-only:x",), "openai-compatible")


@pytest.mark.parametrize("over", [
    {"policy": "other"}, {"known_labels": "x"}, {"local_model_only_tags": ["UPPER"]}, {"known_labels": ["a b"]},
    {"extra": []},
])
def test_a_malformed_policy_is_rejected_loudly(over):
    with pytest.raises(ValueError):
        _policy(**over)
