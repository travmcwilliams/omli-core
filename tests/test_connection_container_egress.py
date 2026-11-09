"""container-egress connection target kind — a sandbox's network-egress boundary.

A container-egress connection names WHICH domains a sandbox may open a socket to,
default-deny, with a single 'connect' operation (egress reachability — never a read or
change-class op). It needs no credential_ref (anonymous at the network layer); a
credential_ref, when present, must be a secret:// reference.
"""

from pathlib import Path

import pytest

from omli.connections.schema import load_registry, parse_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"


def _egress_registry():
    return {
        "metadata": {
            "registry_id": "test_egress_registry",
            "registry_version": "0.1.0",
            "authored_at": "2026-07-14T00:00:00Z",
            "authored_by": "tester@example.com",
            "status": "draft",
        },
        "connections": [
            {
                "connection_id": "sandbox_egress",
                "target": {
                    "kind": "container-egress",
                    "identifier": "sandbox-egress",
                    "principal": "example-owner",
                    "sensitivity_allowed": ["public"],
                },
                "selection": {
                    "mode": "allowlist",
                    "resources": [
                        {"id": "api.example.com", "kind": "domain", "selected": True,
                         "operations": ["connect"], "label": "Model API"},
                        {"id": "git.example.com", "kind": "domain", "selected": True,
                         "operations": ["connect"], "label": "Code host"},
                        {"id": "pkg.example.com", "kind": "domain", "selected": False,
                         "operations": [], "label": "unticked -> unreachable"},
                    ],
                },
                "blast_radius": {
                    "forbidden_paths": ["169.254.169.254", "*.internal"],
                    "direct_push": False,
                    "destructive_ops": False,
                },
                "approval": {
                    "mode": "pr-only",
                    "required_reviewers": ["example-owner"],
                    "auto_merge": False,
                    "escalate_on": ["out-of-selection", "forbidden-path"],
                },
            }
        ],
    }


def _entry():
    return parse_registry(_egress_registry()).connections[0]


def _connect(domain, **kw):
    kw.setdefault("sensitivity_tags", ("public",))
    return ConnectionAction("sandbox_egress", "connect", path=domain, **kw)


# -- parsing / validation ------------------------------------------------------------
def test_egress_entry_parses_without_credential_ref():
    entry = _entry()
    assert entry.target.kind == "container-egress"
    assert entry.target.credential_ref is None
    # egress needs no change/browse numeric caps
    assert entry.blast_radius.max_files_per_change is None
    assert entry.blast_radius.max_pages_per_session is None


def test_egress_credential_ref_if_present_must_be_secret_ref():
    data = _egress_registry()
    data["connections"][0]["target"]["credential_ref"] = "sk-inline-token"
    with pytest.raises(ValueError, match="secret://"):
        parse_registry(data)


def test_egress_rows_must_be_domain_kind():
    data = _egress_registry()
    data["connections"][0]["selection"]["resources"][0]["kind"] = "path-glob"
    with pytest.raises(ValueError, match="domain"):
        parse_registry(data)


def test_egress_rows_may_only_grant_connect():
    data = _egress_registry()
    data["connections"][0]["selection"]["resources"][0]["operations"] = ["connect", "read"]
    with pytest.raises(ValueError, match="only grant 'connect'"):
        parse_registry(data)


def test_connect_is_egress_only_forbidden_on_git_repo():
    data = _egress_registry()
    conn = data["connections"][0]
    conn["target"]["kind"] = "git-repo"
    conn["target"]["credential_ref"] = "secret://connections/x/app"
    conn["blast_radius"].update({"max_files_per_change": 5, "max_lines_per_change": 50})
    with pytest.raises(ValueError, match="container-egress-only operation"):
        parse_registry(data)


def test_egress_unselected_row_may_not_declare_operations():
    data = _egress_registry()
    data["connections"][0]["selection"]["resources"][2]["operations"] = ["connect"]
    with pytest.raises(ValueError, match="must not declare operations"):
        parse_registry(data)


# -- scope gate ----------------------------------------------------------------------
def test_gate_allows_connect_on_selected_domain():
    assert connection_scope_gate(_connect("api.example.com"), _entry()).decision == "allow"


def test_gate_refuses_unticked_and_unknown_domains_as_out_of_selection():
    for domain in ("pkg.example.com", "evil.example.com"):
        decision = connection_scope_gate(_connect(domain), _entry())
        assert decision.decision in ("deny", "escalate"), domain
        assert "out-of-selection" in decision.reason


def test_gate_forbidden_domain_wins():
    decision = connection_scope_gate(_connect("169.254.169.254"), _entry())
    assert decision.decision == "deny"
    assert "forbidden-path" in decision.reason


def test_gate_egress_is_connect_only():
    for op in ("read", "propose-pr", "write-branch"):
        action = ConnectionAction("sandbox_egress", op, path="api.example.com",
                                  sensitivity_tags=("public",))
        decision = connection_scope_gate(action, _entry())
        assert decision.decision == "deny"
        assert "egress-connect-only" in decision.reason


def test_gate_sensitivity_fails_closed():
    decision = connection_scope_gate(_connect("api.example.com", sensitivity_tags=("internal",)), _entry())
    assert decision.decision == "deny"
    assert "sensitivity" in decision.reason


# -- the shipped example registry ----------------------------------------------------
def test_example_registry_carries_the_egress_connection():
    registry = load_registry(REG_PATH)
    kinds = {c.target.kind for c in registry.connections}
    assert "container-egress" in kinds
    egress = registry.get("sandbox_egress")
    domains = {r.id for r in egress.selection.selected_resources()}
    assert "api.example.com" in domains and "git.example.com" in domains
