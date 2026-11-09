"""The scope-gated sender adapter (omli/connections/sender.py): gate -> resolve -> dispatch.

Proves the deterministic gate wraps the actuator (Primitive 2): nothing dispatches unless the
scope gate returns a clean "allow" AND the credential resolves, and the resolved credential
never leaks onto the returned outcome. Senders + the credential resolver are injected, so these
tests never touch the network or a real secret store.
"""

from pathlib import Path

import pytest

from omli.connections.schema import load_registry
from omli.connections.scope_gate import ConnectionAction
from omli.connections.secrets import SecretNotFound
from omli.connections.sender import dispatch

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"
TOKEN = "test-token-do-not-log-me"


def _conn(cid):
    return load_registry(REG_PATH).get(cid)


class _Recorder:
    """A fake low-level sender that records what it was handed and reports ok."""

    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    def __call__(self, payload, credential, connection, action):
        self.calls.append({"payload": payload, "credential": credential,
                           "target": action.path, "kind": connection.target.kind})
        return {"ok": self._ok, "to": action.path}


def _resolve_ok(connection, *, required=False):
    return TOKEN


def _resolve_missing(connection, *, required=False):
    if required:
        raise SecretNotFound("no value wired for the credential")
    return None


def test_allow_dispatches_through_the_injected_sender():
    rec = _Recorder()
    action = ConnectionAction("team_email", "send-email", path="owner@example.com",
                              sensitivity_tags=("internal",), sends_today=0)
    out = dispatch(_conn("team_email"), action, {"subject": "hi", "body": "there"},
                   senders={"smtp": rec}, resolve=_resolve_ok)
    assert out.ok is True
    assert out.decision == "allow"
    assert out.kind == "smtp" and out.target == "owner@example.com"
    assert len(rec.calls) == 1
    # the resolved credential reached the wire...
    assert rec.calls[0]["credential"] == TOKEN


def test_deny_does_not_dispatch():
    rec = _Recorder()
    # a non-send op on smtp is a hard deny — must never reach the sender
    action = ConnectionAction("team_email", "read", path="owner@example.com")
    out = dispatch(_conn("team_email"), action, {"body": "x"},
                   senders={"smtp": rec}, resolve=_resolve_ok)
    assert out.ok is False
    assert out.decision == "deny"
    assert rec.calls == []               # nothing left this machine


def test_escalate_does_not_dispatch():
    rec = _Recorder()
    # an unlisted recipient escalates (out-of-selection) — must never reach the sender
    action = ConnectionAction("team_email", "send-email", path="stranger@example.net",
                              sensitivity_tags=("internal",))
    out = dispatch(_conn("team_email"), action, {"body": "x"},
                   senders={"smtp": rec}, resolve=_resolve_ok)
    assert out.ok is False
    assert out.decision == "escalate"
    assert "out-of-selection" in out.reason
    assert rec.calls == []


def test_missing_credential_fails_closed():
    rec = _Recorder()
    action = ConnectionAction("team_email", "send-email", path="owner@example.com",
                              sensitivity_tags=("internal",), sends_today=0)
    out = dispatch(_conn("team_email"), action, {"body": "x"},
                   senders={"smtp": rec}, resolve=_resolve_missing)
    assert out.ok is False
    assert out.decision == "allow"       # the gate allowed it...
    assert rec.calls == []               # ...but no credential -> nothing dispatched
    assert out.reason == "credential-unavailable"


def test_credential_never_appears_on_the_outcome():
    """The fake sender echoes the credential back in every field it returns; none may survive."""
    import json
    from urllib.parse import quote

    def echo(payload, credential, connection, action):
        return {"ok": False, "error": f"rejected {credential}", "raw": json.dumps({"k": credential}),
                "url": f"https://x.example/?t={quote(credential, safe='')}", "nested": [{"v": credential}]}

    action = ConnectionAction("team_email", "send-email", path="owner@example.com",
                              sensitivity_tags=("internal",), sends_today=0)
    out = dispatch(_conn("team_email"), action, {"body": "x"},
                   senders={"smtp": echo}, resolve=_resolve_ok)
    assert out.decision == "allow" and out.ok is False      # the sender really ran
    assert TOKEN not in repr(out) and quote(TOKEN, safe="") not in repr(out)


def test_allow_but_no_sender_wired_for_kind():
    action = ConnectionAction("team_slack", "post", path="#team-notes",
                              sensitivity_tags=("internal",))
    out = dispatch(_conn("team_slack"), action, {"text": "hi"},
                   senders={}, resolve=_resolve_ok)   # empty table -> no slack sender
    assert out.ok is False
    assert out.decision == "allow"
    assert out.reason == "no-wire-for-kind"


def test_a_failure_returned_by_the_sender_is_reported():
    rec = _Recorder(ok=False)
    action = ConnectionAction("team_slack", "post", path="#team-notes",
                              sensitivity_tags=("internal",))
    out = dispatch(_conn("team_slack"), action, {"text": "hi"},
                   senders={"slack": rec}, resolve=_resolve_ok)
    assert out.ok is False
    assert out.decision == "allow"
    assert out.reason == "wire-reported-failure"
