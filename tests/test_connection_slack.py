"""The slack channel is a governed Connection Registry kind.

The example slack entry uses a secret:// credential_ref, and the scope gate allows a ticked
channel and refuses an unlisted, a forbidden or a non-messaging request.
"""

from pathlib import Path

from omli.connections.schema import (
    CONNECTION_OPERATIONS,
    TARGET_KINDS,
    load_registry,
)
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"


def _slack():
    return load_registry(REG_PATH).get("team_slack")


def test_slack_is_a_registered_kind_with_a_secret_credential():
    assert "slack" in TARGET_KINDS
    assert "post" in CONNECTION_OPERATIONS
    slack = _slack()
    assert slack.target.kind == "slack"
    # credential is a secret:// reference, never inline (schema enforces the prefix)
    assert slack.target.credential_ref is not None
    assert slack.target.credential_ref.startswith("secret://")


def test_scope_gate_allows_a_ticked_channel_post():
    decision = connection_scope_gate(
        ConnectionAction("team_slack", "post", path="#team-notes", sensitivity_tags=("internal",)), _slack()
    )
    assert decision.decision == "allow"


def test_scope_gate_rejects_an_unlisted_slack_channel():
    # An unlisted channel is NOT allowed — it surfaces to the human (escalate) rather than
    # silently posting. Either way the deterministic gate refuses to let it through unattended.
    decision = connection_scope_gate(
        ConnectionAction("team_slack", "post", path="#not-in-the-allowlist", sensitivity_tags=("internal",)), _slack()
    )
    assert decision.decision != "allow"
    assert "out-of-selection" in (decision.reason or "")


def test_scope_gate_hard_denies_a_forbidden_channel():
    decision = connection_scope_gate(
        ConnectionAction("team_slack", "post", path="#incidents-live", sensitivity_tags=("internal",)), _slack()
    )
    assert decision.decision == "deny"
    assert "forbidden-path" in (decision.reason or "")


def test_scope_gate_denies_a_non_messaging_operation_on_slack():
    # Only the messaging-layer ops are permitted on slack (defense in depth over the
    # validator). The artifact-layer 'read' is NOT one of them — reading a channel is
    # 'read-thread' (see test_connection_slack_read_thread.py), kept
    # separate so a messaging grant can never satisfy read-before-change elsewhere.
    decision = connection_scope_gate(
        ConnectionAction("team_slack", "read", path="#team-notes", sensitivity_tags=("internal",)), _slack()
    )
    assert decision.decision == "deny"
    assert "slack-messaging-only" in (decision.reason or "")
