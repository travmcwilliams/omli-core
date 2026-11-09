"""Email SEND is a governed Connection Registry kind.

These tests pin the smtp kind in the example registry and the scope-gate decisions that the
sender adapter (omli/connections/sender.py) requires before it sends; test_connection_slack.py
does the same for the slack kind.
"""

from pathlib import Path

from omli.connections.schema import (
    CONNECTION_OPERATIONS,
    TARGET_KINDS,
    load_registry,
)
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"


def _email():
    return load_registry(REG_PATH).get("team_email")


def test_smtp_is_a_registered_kind_with_a_secret_credential():
    assert "smtp" in TARGET_KINDS
    assert "send-email" in CONNECTION_OPERATIONS
    email = _email()
    assert email.target.kind == "smtp"
    # credential is a secret:// reference, never inline (schema enforces the prefix)
    assert email.target.credential_ref is not None
    assert email.target.credential_ref.startswith("secret://")


def test_scope_gate_allows_a_ticked_recipient():
    decision = connection_scope_gate(
        ConnectionAction("team_email", "send-email", path="owner@example.com",
                         sensitivity_tags=("internal",), sends_today=0),
        _email(),
    )
    assert decision.decision == "allow"


def test_scope_gate_allows_a_domain_glob_recipient():
    decision = connection_scope_gate(
        ConnectionAction("team_email", "send-email", path="ops@example.com",
                         sensitivity_tags=("public",), sends_today=0),
        _email(),
    )
    assert decision.decision == "allow"


def test_scope_gate_rejects_an_unlisted_recipient():
    # An unlisted address is NOT allowed — it surfaces to the human (escalate) rather than
    # silently sending. Either way the deterministic gate refuses to let it through unattended.
    decision = connection_scope_gate(
        ConnectionAction("team_email", "send-email", path="stranger@example.net",
                         sensitivity_tags=("public",)),
        _email(),
    )
    assert decision.decision != "allow"
    assert "out-of-selection" in (decision.reason or "")


def test_scope_gate_hard_denies_a_forbidden_recipient():
    decision = connection_scope_gate(
        ConnectionAction("team_email", "send-email", path="someone@team.restricted.example.com"),
        _email(),
    )
    assert decision.decision == "deny"
    assert "forbidden-path" in (decision.reason or "")


def test_scope_gate_denies_a_non_send_operation_on_smtp():
    # 'send-email' is the only operation an smtp connection permits (defense in depth over the
    # validator).
    decision = connection_scope_gate(
        ConnectionAction("team_email", "read", path="owner@example.com"),
        _email(),
    )
    assert decision.decision == "deny"
    assert "smtp-send-only" in (decision.reason or "")


def test_scope_gate_fails_closed_above_allowed_sensitivity():
    # sensitivity_allowed is [public, internal]; a local-only note never reaches the transport.
    decision = connection_scope_gate(
        ConnectionAction("team_email", "send-email", path="owner@example.com",
                         sensitivity_tags=("local-only",)),
        _email(),
    )
    assert decision.decision != "allow"
    assert "sensitivity-mismatch" in (decision.reason or "")


def test_scope_gate_enforces_the_daily_send_ceiling():
    # The max_sends_per_day ceiling is the outbound-actuator cap. The count of today's sends is
    # supplied by the caller: once it reaches the ceiling the next send is refused, and a send
    # with no count at all is refused too (see test_trust_boundaries.py).
    email = _email()
    assert email.blast_radius.max_sends_per_day == 50
    decision = connection_scope_gate(
        ConnectionAction("team_email", "send-email", path="owner@example.com",
                         sensitivity_tags=("internal",), sends_today=50),
        email,
    )
    assert decision.decision != "allow"
    assert "blast-radius-exceeded" in (decision.reason or "")
