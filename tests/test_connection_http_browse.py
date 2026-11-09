"""http-browse connection target kind: a controlled browser's external boundary.

A browse connection names WHICH domains the controlled browser may read, default-deny,
read-only structurally (a change-class op can't even be authored), with browse-shaped
blast-radius caps (pages per session, captures per domain). Every domain without a ticked
row is denied; forbidden_paths is the unconditional deny for domain patterns.
"""

import copy
import dataclasses

import pytest

from omli.connections.schema import parse_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate


def _browse_registry():
    return {
        "metadata": {
            "registry_id": "test_browse_registry",
            "registry_version": "0.1.0",
            "authored_at": "2026-07-10T00:00:00Z",
            "authored_by": "tester@example.com",
            "status": "draft",
        },
        "connections": [
            {
                "connection_id": "web_research",
                "target": {
                    "kind": "http-browse",
                    "identifier": "open-web",
                    "principal": "example-owner",
                    "sensitivity_allowed": ["public"],
                },
                "selection": {
                    "mode": "allowlist",
                    "resources": [
                        {"id": "docs.example.org", "kind": "domain", "selected": True,
                         "operations": ["read"], "label": "Reference docs"},
                        {"id": "*.wiki.example.org", "kind": "domain", "selected": True,
                         "operations": ["read"], "label": "Wiki subdomains"},
                        {"id": "news.example.net", "kind": "domain", "selected": False,
                         "operations": [], "label": "unticked -> fully denied"},
                    ],
                },
                "blast_radius": {
                    "max_pages_per_session": 40,
                    "max_captures_per_domain": 15,
                    "forbidden_paths": ["*.bank*", "mail.*", "*.mail.*"],
                    "direct_push": False,
                    "destructive_ops": False,
                },
                "approval": {
                    "mode": "pr-only",
                    "required_reviewers": ["example-owner"],
                    "auto_merge": False,
                    "escalate_on": ["blast-radius-exceeded"],
                },
            }
        ],
    }


def _entry():
    return parse_registry(_browse_registry()).connections[0]


def _read(domain, **kw):
    kw.setdefault("sensitivity_tags", ("public",))
    return ConnectionAction("web_research", "read", path=domain, **kw)


# -- parsing / validation ------------------------------------------------------------


def test_browse_entry_parses_without_credential_ref():
    entry = _entry()
    assert entry.target.kind == "http-browse"
    assert entry.target.credential_ref is None
    assert entry.blast_radius.max_pages_per_session == 40
    assert entry.blast_radius.max_captures_per_domain == 15


def test_browse_credential_ref_if_present_must_be_secret_ref():
    data = _browse_registry()
    data["connections"][0]["target"]["credential_ref"] = "inline-token"
    with pytest.raises(ValueError, match="secret://"):
        parse_registry(data)
    ok = _browse_registry()
    ok["connections"][0]["target"]["credential_ref"] = "secret://connections/browse/profile"
    assert parse_registry(ok).connections[0].target.credential_ref.startswith("secret://")


def test_git_repo_requires_credential_ref():
    data = _browse_registry()
    conn = data["connections"][0]
    conn["target"]["kind"] = "git-repo"
    # leave credential_ref absent — must fail for non-browse kinds
    with pytest.raises(ValueError, match="credential_ref"):
        parse_registry(data)


def test_browse_is_read_only_structurally():
    data = _browse_registry()
    data["connections"][0]["selection"]["resources"][0]["operations"] = ["read", "propose-pr"]
    with pytest.raises(ValueError, match="read-only"):
        parse_registry(data)


def test_browse_resources_must_be_domain_rows():
    data = _browse_registry()
    data["connections"][0]["selection"]["resources"][0]["kind"] = "path-glob"
    with pytest.raises(ValueError, match="domain"):
        parse_registry(data)


@pytest.mark.parametrize("cap", ["max_pages_per_session", "max_captures_per_domain"])
def test_browse_caps_required_and_positive(cap):
    missing = _browse_registry()
    del missing["connections"][0]["blast_radius"][cap]
    with pytest.raises(ValueError, match=cap):
        parse_registry(missing)
    nonpositive = _browse_registry()
    nonpositive["connections"][0]["blast_radius"][cap] = 0
    with pytest.raises(ValueError, match="positive"):
        parse_registry(nonpositive)


def test_browse_does_not_require_file_change_caps():
    entry = _entry()  # fixture omits max_files_per_change / max_lines_per_change
    assert entry.blast_radius.max_files_per_change is None
    assert entry.blast_radius.max_lines_per_change is None


def test_git_repo_file_caps_required():
    data = _browse_registry()
    conn = data["connections"][0]
    conn["target"]["kind"] = "git-repo"
    conn["target"]["credential_ref"] = "secret://connections/x/app"
    conn["selection"]["resources"] = [
        {"id": "src/**", "kind": "path-glob", "selected": True, "operations": ["read"]},
    ]
    with pytest.raises(ValueError, match="max_files_per_change"):
        parse_registry(data)


# -- scope gate ----------------------------------------------------------------------


def test_gate_allows_read_on_selected_domain():
    assert connection_scope_gate(_read("docs.example.org"), _entry()).decision == "allow"


def test_gate_allows_wildcard_subdomain():
    assert connection_scope_gate(_read("en.wiki.example.org"), _entry()).decision == "allow"


def test_gate_wildcard_does_not_match_apex():
    # "*.wiki.example.org" does NOT cover the apex domain — that needs its own ticked row.
    decision = connection_scope_gate(_read("wiki.example.org"), _entry())
    assert decision.decision == "deny"
    assert "out-of-selection" in decision.reason


def test_gate_default_denies_unticked_and_unknown_domains():
    for domain in ("news.example.net", "example.com"):
        decision = connection_scope_gate(_read(domain), _entry())
        assert decision.decision == "deny", domain
        assert "out-of-selection" in decision.reason


def test_gate_forbidden_domain_wins_and_never_escalates():
    entry = _entry()
    opted_in = dataclasses.replace(
        entry, approval=dataclasses.replace(entry.approval, escalate_on=("forbidden-path",)))
    decision = connection_scope_gate(_read("mail.example.com"), opted_in)
    assert decision.decision == "deny"
    assert "forbidden-path" in decision.reason


def test_gate_browse_is_read_only():
    action = ConnectionAction(
        "web_research", "propose-pr", path="docs.example.org",
        sensitivity_tags=("public",),
    )
    decision = connection_scope_gate(action, _entry())
    assert decision.decision == "deny"
    assert "browse-read-only" in decision.reason


def test_gate_session_page_cap():
    ok = _read("docs.example.org", pages_in_session=40)
    over = _read("docs.example.org", pages_in_session=41)
    assert connection_scope_gate(ok, _entry()).decision == "allow"
    decision = connection_scope_gate(over, _entry())
    assert decision.decision == "escalate"  # escalate_on includes blast-radius-exceeded
    assert "pages" in decision.reason


def test_gate_per_domain_capture_cap():
    over = _read("docs.example.org", captures_for_domain=16)
    decision = connection_scope_gate(over, _entry())
    assert decision.decision == "escalate"
    assert "captures" in decision.reason


def test_gate_sensitivity_fails_closed():
    decision = connection_scope_gate(_read("docs.example.org", sensitivity_tags=("internal",)), _entry())
    assert decision.decision == "deny"
    assert "sensitivity" in decision.reason


def test_example_registry_validates_with_browse_entry():
    from pathlib import Path
    from omli.connections.schema import load_registry

    path = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"
    registry = load_registry(path)
    kinds = {c.target.kind for c in registry.connections}
    assert "http-browse" in kinds, "example registry should carry a browse connection"
