"""Connection scope gate: default-deny, hard forbidden-path, read-before-change, blast radius."""

import dataclasses
from pathlib import Path

from omli.connections.schema import load_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"

PUB = ("public",)


def _conn():
    return load_registry(REG_PATH).get("example_repo")


def _act(**kw):
    kw.setdefault("connection_id", "example_repo")
    kw.setdefault("sensitivity_tags", PUB)
    return ConnectionAction(**kw)


def test_read_selected_path_is_allowed():
    d = connection_scope_gate(_act(operation="read", path="src/lib/utils.py"), _conn())
    assert d.decision == "allow"


def test_read_unticked_path_is_out_of_selection():
    d = connection_scope_gate(_act(operation="read", path="src/internal/limits.py"), _conn())
    # example opts into escalate on out-of-selection
    assert d.decision == "escalate"
    assert "out-of-selection" in d.reason


def test_forbidden_path_beats_a_ticked_row_and_never_escalates():
    d = connection_scope_gate(_act(operation="read", path="src/services/secrets/key.txt"), _conn())
    assert d.decision == "deny"
    assert d.reason.startswith("forbidden-path")


def test_forbidden_top_level_secrets_is_denied_not_escalated():
    """The `**/` zero-leading-segment case of globmatch: a TOP-LEVEL `secrets/...` (not
    nested like the test above) matches `**/secrets/**` and is hard-denied. And even
    though example_repo lists 'forbidden-path' in escalate_on, a forbidden path is advisory
    there and never softens to escalate."""
    d = connection_scope_gate(_act(operation="read", path="secrets/key.txt"), _conn())
    assert d.decision == "deny"
    assert d.reason.startswith("forbidden-path")


def test_change_class_op_on_read_only_row_is_refused_as_out_of_selection():
    # src/lib/** grants read only; propose-pr must not match it → out-of-selection.
    d = connection_scope_gate(_act(operation="propose-pr", path="src/lib/utils.py"), _conn())
    assert d.decision in ("deny", "escalate")
    assert "out-of-selection" in d.reason


def test_propose_pr_on_change_allowed_path_escalates_when_opted_in():
    # example lists "change-class-op" in escalate_on → a change-class op escalates.
    d = connection_scope_gate(
        _act(operation="propose-pr", path="src/services/x.py", files_in_change=2, lines_in_change=30),
        _conn(),
    )
    assert d.decision == "escalate"
    assert "change-class-op" in d.reason


def test_propose_pr_allowed_when_not_opted_into_change_escalation():
    conn = _conn()
    approval = dataclasses.replace(
        conn.approval, escalate_on=tuple(t for t in conn.approval.escalate_on if t != "change-class-op")
    )
    conn = dataclasses.replace(conn, approval=approval)
    d = connection_scope_gate(
        _act(operation="propose-pr", path="src/services/x.py", files_in_change=2, lines_in_change=30),
        conn,
    )
    assert d.decision == "allow"


def test_write_branch_to_namespace_allowed():
    conn = _conn()
    approval = dataclasses.replace(
        conn.approval, escalate_on=tuple(t for t in conn.approval.escalate_on if t != "change-class-op")
    )
    conn = dataclasses.replace(conn, approval=approval)
    d = connection_scope_gate(_act(operation="write-branch", branch="omli/change-1"), conn)
    assert d.decision == "allow"


def test_write_branch_to_main_is_out_of_selection():
    d = connection_scope_gate(_act(operation="write-branch", branch="main"), _conn())
    assert d.decision in ("deny", "escalate")
    assert "out-of-selection" in d.reason


def test_blast_radius_exceeded():
    conn = _conn()
    approval = dataclasses.replace(
        conn.approval, escalate_on=tuple(t for t in conn.approval.escalate_on if t != "change-class-op")
    )
    conn = dataclasses.replace(conn, approval=approval)
    d = connection_scope_gate(
        _act(operation="propose-pr", path="src/services/x.py", files_in_change=999), conn
    )
    assert d.decision == "escalate"
    assert "blast-radius-exceeded" in d.reason


def test_blast_radius_exceeded_denies_when_not_opted_into_escalation():
    """Fail-closed default: with 'blast-radius-exceeded' NOT in escalate_on, a change that
    exceeds the file cap must DENY (not allow, not escalate). The cap is a hard ceiling by
    default; test_blast_radius_exceeded above covers the opt-in-to-escalate variant."""
    conn = _conn()
    approval = dataclasses.replace(
        conn.approval,
        escalate_on=tuple(
            t for t in conn.approval.escalate_on if t not in ("change-class-op", "blast-radius-exceeded")
        ),
    )
    conn = dataclasses.replace(conn, approval=approval)
    d = connection_scope_gate(
        _act(operation="propose-pr", path="src/services/x.py", files_in_change=999), conn
    )
    assert d.decision == "deny"
    assert "blast-radius-exceeded" in d.reason


def test_sensitivity_mismatch_fails_closed():
    d = connection_scope_gate(
        _act(operation="read", path="src/lib/utils.py", sensitivity_tags=("local-only:probe",)),
        _conn(),
    )
    assert d.decision in ("deny", "escalate")
    assert "sensitivity-mismatch" in d.reason
