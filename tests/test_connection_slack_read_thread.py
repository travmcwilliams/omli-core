"""The inbound half of a slack channel is its own grant.

The registry names a read operation for the reply path, and the scope gate allows it for the
granted channel and denies an unlisted one.

`read-thread` is deliberately NOT the artifact-layer `read`. That one is the
read-before-change partner of propose-pr/write-branch; letting a Slack grant satisfy it would
quietly widen an unrelated git connection. Two names, because they authorize two unrelated
things.

It is also granted independently of `post`. Posting into a channel does not imply permission
to read it back, and an inbound grant hands over every message in the channel.
"""

from pathlib import Path

import pytest
import yaml

from omli.connections.schema import (
    CONNECTION_OPERATIONS,
    SLACK_OPERATIONS,
    load_registry,
    parse_registry,
)
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"

#: The one channel the example registry grants for reading.
GRANTED = "CEXAMPLE001"
#: Ticked for posting, never for reading — the independence of the two grants, in the data.
POST_ONLY = "#team-notes"


def _slack():
    return load_registry(REG_PATH).get("team_slack")


def _decide(operation: str, channel: str):
    return connection_scope_gate(
        ConnectionAction("team_slack", operation, path=channel, sensitivity_tags=("internal",)), _slack())


# --------------------------------------------------------------------------- #
# The operation exists, and is messaging-layer only.
# --------------------------------------------------------------------------- #

def test_read_thread_is_a_registered_operation():
    assert "read-thread" in CONNECTION_OPERATIONS
    assert set(SLACK_OPERATIONS) == {"post", "read-thread"}


def test_the_registry_declares_a_1x_version():
    """Checks the version field only. Nothing here can show that a version was never edited."""
    meta = yaml.safe_load(REG_PATH.read_text(encoding="utf-8"))["metadata"]
    assert meta["registry_version"].startswith("1."), meta["registry_version"]


def test_the_grant_ships_under_pr_only_approval():
    approval = _slack().approval
    assert approval.mode == "pr-only"
    assert approval.auto_merge is False


# --------------------------------------------------------------------------- #
# The allow and the deny.
# --------------------------------------------------------------------------- #

def test_the_granted_channel_may_be_read_back():
    assert _decide("read-thread", GRANTED).decision == "allow"


def test_an_unlisted_channel_may_not_be_read():
    decision = _decide("read-thread", "#not-in-the-allowlist")
    assert decision.decision != "allow"
    assert "out-of-selection" in (decision.reason or "")


def test_an_unticked_channel_may_not_be_read():
    """#general is present in the checklist but unticked — default-deny, not absence."""
    decision = _decide("read-thread", "#general")
    assert decision.decision != "allow"


def test_a_forbidden_channel_is_hard_denied_even_for_reading():
    """forbidden_paths win over any row, in either direction."""
    decision = _decide("read-thread", "#incidents-live")
    assert decision.decision == "deny"
    assert "forbidden-path" in (decision.reason or "")


# --------------------------------------------------------------------------- #
# Posting does not imply reading.
# --------------------------------------------------------------------------- #

def test_a_post_only_channel_cannot_be_read_back():
    assert _decide("post", POST_ONLY).decision == "allow"
    decision = _decide("read-thread", POST_ONLY)
    assert decision.decision != "allow", (
        "a channel granted only 'post' was readable — the inbound grant is not independent")


def test_the_artifact_layer_read_is_refused_on_slack():
    """'read' is the read-before-change partner elsewhere; it must not mean 'read a channel'."""
    decision = _decide("read", GRANTED)
    assert decision.decision == "deny"
    assert "slack-messaging-only" in (decision.reason or "")


# --------------------------------------------------------------------------- #
# The validator refuses to author the grant anywhere it does not belong.
# --------------------------------------------------------------------------- #

def _registry_with(kind: str, resource: dict) -> dict:
    return {
        "metadata": {"registry_id": "t", "registry_version": "0.1.0", "authored_at": "t",
                     "authored_by": "t", "status": "draft"},
        "connections": [{
            "connection_id": "c",
            "target": {"kind": kind, "identifier": f"{kind}:x",
                       "credential_ref": "secret://c/token", "principal": "example-owner",
                       "sensitivity_allowed": ["internal"]},
            "selection": {"mode": "allowlist", "resources": [resource]},
            "blast_radius": {"direct_push": False, "destructive_ops": False},
            "approval": {"mode": "pr-only", "required_reviewers": ["example-owner"],
                         "auto_merge": False},
        }],
    }


def test_read_thread_cannot_be_granted_on_a_git_repo():
    data = _registry_with("git-repo", {"id": "src/**", "kind": "path-glob",
                                       "selected": True, "operations": ["read-thread"]})
    with pytest.raises(ValueError, match="slack-only"):
        parse_registry(data)


def test_an_unticked_slack_row_may_not_declare_read_thread():
    data = _registry_with("slack", {"id": "#x", "kind": "channel",
                                    "selected": False, "operations": ["read-thread"]})
    with pytest.raises(ValueError, match="must not declare operations"):
        parse_registry(data)
