"""ConnectionRegistry: strict validation, default-deny, secret-only credentials, PR-only."""

import copy
from pathlib import Path

import pytest
import yaml

from omli.connections.schema import ConnectionRegistry, load_registry, parse_registry

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"


def _data():
    return yaml.safe_load(REG_PATH.read_text(encoding="utf-8"))


def test_example_registry_loads_and_resolves_ids():
    reg = load_registry(REG_PATH)
    assert isinstance(reg, ConnectionRegistry)
    assert reg.ids() == ("example_repo", "web_research", "sandbox_egress", "team_slack",
                         "team_email", "team_graph_mail", "data_api", "model_api")
    conn = reg.get("example_repo")
    assert conn.target.kind == "git-repo"
    assert conn.target.credential_ref.startswith("secret://")
    # default-deny: the unticked rows carry no operations
    risk = next(r for r in conn.selection.resources if r.id == "src/internal/**")
    assert risk.selected is False and risk.operations == ()


def test_missing_connection_raises_keyerror():
    reg = load_registry(REG_PATH)
    with pytest.raises(KeyError):
        reg.get("nope")


def test_inline_credential_is_rejected():
    data = _data()
    data["connections"][0]["target"]["credential_ref"] = "inline-token-value"
    with pytest.raises(ValueError, match="credential_ref must be a 'secret://'"):
        parse_registry(data)


def test_unselected_resource_may_not_declare_operations():
    data = _data()
    risk = next(r for r in data["connections"][0]["selection"]["resources"] if r["id"] == "src/internal/**")
    risk["operations"] = ["read"]  # unticked but claims a grant
    with pytest.raises(ValueError, match="must not declare operations"):
        parse_registry(data)


def test_change_op_without_read_is_rejected():
    data = _data()
    sig = next(r for r in data["connections"][0]["selection"]["resources"] if r["id"] == "src/lib/**")
    sig["operations"] = ["propose-pr"]  # change-class op with no read on the same row
    with pytest.raises(ValueError, match="without 'read'"):
        parse_registry(data)


def test_auto_merge_true_is_rejected():
    data = _data()
    data["connections"][0]["approval"]["auto_merge"] = True
    with pytest.raises(ValueError, match="auto_merge must be false"):
        parse_registry(data)


def test_direct_push_and_destructive_must_be_false():
    data = _data()
    data["connections"][0]["blast_radius"]["direct_push"] = True
    with pytest.raises(ValueError, match="direct_push must be false"):
        parse_registry(data)
    data = _data()
    data["connections"][0]["blast_radius"]["destructive_ops"] = True
    with pytest.raises(ValueError, match="destructive_ops must be false"):
        parse_registry(data)


def test_unknown_escalate_trigger_is_rejected():
    data = _data()
    data["connections"][0]["approval"]["escalate_on"].append("auto-adopt")
    with pytest.raises(ValueError, match="unknown escalate_on trigger"):
        parse_registry(data)


def test_duplicate_connection_id_is_rejected():
    data = _data()
    data["connections"].append(copy.deepcopy(data["connections"][0]))
    with pytest.raises(ValueError, match="duplicate connection_id"):
        parse_registry(data)


def test_live_connection_needs_a_selected_resource():
    data = _data()
    for r in data["connections"][0]["selection"]["resources"]:
        r["selected"] = False
        r["operations"] = []
    with pytest.raises(ValueError, match="no selected resources"):
        parse_registry(data)


def test_deprecated_registry_may_have_zero_selected():
    data = _data()
    data["metadata"]["status"] = "deprecated"
    for r in data["connections"][0]["selection"]["resources"]:
        r["selected"] = False
        r["operations"] = []
    reg = parse_registry(data)  # allowed: a deprecated registry grants nothing on purpose
    assert reg.metadata.status == "deprecated"


def test_required_reviewers_must_not_be_empty():
    data = _data()
    data["connections"][0]["approval"]["required_reviewers"] = []
    with pytest.raises(ValueError, match="required_reviewers needs at least one"):
        parse_registry(data)
