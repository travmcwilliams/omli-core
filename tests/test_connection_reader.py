"""The inbound wire is scope-gated too.

`tests/test_connection_slack_read_thread.py` proves the registry grants `read-thread` on one
channel and the gate would refuse the rest. That is the grant. THIS file is about the path:
`omli.connections.reader` is what actually asks, so a caller of it cannot read a channel nobody
allowed.

Nothing here touches the network or a real secret: the low-level reader and the credential
resolver are both injected, exactly as the sender's tests do it.
"""

from pathlib import Path

import pytest

from omli.connections import reader as reader_module
from omli.connections.reader import READ_OPERATION, ReadRefused
from omli.connections.schema import load_registry

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"

#: The one channel the example registry grants for reading.
GRANTED = "CEXAMPLE001"
#: Ticked for posting, never for reading — the independence of the two grants, in the data.
POST_ONLY = "#team-notes"

TOKEN = "test-token-never-logged"
TAGS = ("internal",)


def read(*args, **kwargs):
    kwargs.setdefault("sensitivity_tags", TAGS)
    return reader_module.read(*args, **kwargs)


def check_read(*args, **kwargs):
    kwargs.setdefault("sensitivity_tags", TAGS)
    return reader_module.check_read(*args, **kwargs)


def slack_fetcher(*args, **kwargs):
    kwargs.setdefault("sensitivity_tags", TAGS)
    return reader_module.slack_fetcher(*args, **kwargs)


def _slack():
    return load_registry(REG_PATH).get("team_slack")


def _resolve(connection, *, required=False):
    return TOKEN


def _reader(messages, *, ok=True, error=None, seen=None):
    def fake(query, credential, connection, action):
        if seen is not None:
            seen.append({"query": dict(query), "credential": credential,
                         "channel": action.path, "operation": action.operation})
        return {"ok": ok, "messages": list(messages), "error": error}
    return fake


# --------------------------------------------------------------------------- #
# The allow: the granted channel is read, through the gate, with the credential.
# --------------------------------------------------------------------------- #

def test_the_granted_channel_reads_through_the_gate():
    seen = []
    out = read(_slack(), GRANTED, {"thread_ts": "1753.400"},
               readers={"slack": _reader([{"ts": "1753.500", "text": "1"}], seen=seen)},
               resolve=_resolve)
    assert out.ok and out.decision == "allow"
    assert [m["text"] for m in out.messages] == ["1"]
    assert seen[0]["operation"] == READ_OPERATION, "the reader asked for some other operation"
    assert seen[0]["channel"] == GRANTED
    assert seen[0]["query"]["thread_ts"] == "1753.400"
    assert seen[0]["credential"] == TOKEN


def test_no_thread_ts_leaves_the_thread_out_of_the_query():
    """The fake reader records the query; which surface the real wire reads is not exercised here."""
    seen = []
    read(_slack(), GRANTED, {}, readers={"slack": _reader([], seen=seen)}, resolve=_resolve)
    assert seen[0]["query"].get("thread_ts") in (None, "")


def test_the_credential_never_reaches_a_status_field_and_messages_stay_apart():
    """The fake reader echoes the credential in a message and in an extra key. The status
    fields hold none of it, the extra key is dropped, and the messages come back unchanged in
    their own field as untrusted channel content."""
    def echo(query, credential, connection, action):
        return {"ok": True, "messages": [{"ts": "1", "text": f"token {credential}"}], "note": credential}

    out = read(_slack(), GRANTED, {}, readers={"slack": echo}, resolve=_resolve)
    assert out.ok and out.reason == "" and out.error_type == ""
    status = (out.decision, out.kind, out.target, out.reason, out.error_type)
    assert all(TOKEN not in field for field in status)
    assert out.messages == ({"ts": "1", "text": f"token {TOKEN}"},)
    assert not hasattr(out, "detail") and not hasattr(out, "note")


# --------------------------------------------------------------------------- #
# The denies: on anything but a clean allow, the wire is never touched.
# --------------------------------------------------------------------------- #

def test_a_post_only_channel_is_not_read_and_the_wire_is_never_called():
    called = []

    def never(query, credential, connection, action):     # pragma: no cover - must not run
        called.append(action.path)
        return {"ok": True, "messages": [{"ts": "1", "text": "leak"}]}

    out = read(_slack(), POST_ONLY, {}, readers={"slack": never}, resolve=_resolve)
    assert out.decision != "allow"
    assert out.ok is False
    assert out.messages == (), "messages came back off a channel the gate refused"
    assert called == [], "the reader fired on a refusal — the gate is decorative"


def test_an_unlisted_channel_is_not_read():
    out = read(_slack(), "#not-in-the-allowlist", {},
               readers={"slack": _reader([{"ts": "1", "text": "leak"}])}, resolve=_resolve)
    assert out.decision != "allow" and out.messages == ()
    assert "out-of-selection" in out.reason


def test_a_forbidden_channel_is_hard_denied():
    out = read(_slack(), "#incidents-live", {},
               readers={"slack": _reader([])}, resolve=_resolve)
    assert out.decision == "deny" and "forbidden-path" in out.reason


def test_a_sensitivity_above_the_connection_floor_is_refused():
    """The read's own sensitivity floor is a second, independent fail-closed check — a local-only
    session is not read back off Slack any more than it is announced there."""
    out = read(_slack(), GRANTED, {}, sensitivity_tags=("local-only",),
               readers={"slack": _reader([{"ts": "1", "text": "leak"}])}, resolve=_resolve)
    assert out.decision != "allow" and out.messages == ()


def test_a_missing_credential_fails_closed():
    from omli.connections.secrets import SecretNotFound

    def missing(connection, *, required=False):
        raise SecretNotFound("no token configured")

    out = read(_slack(), GRANTED, {}, readers={"slack": _reader([])}, resolve=missing)
    assert out.ok is False and out.messages == ()
    assert out.reason == "credential-unavailable"


def test_a_failure_returned_by_the_reader_yields_no_messages():
    """Slack answering not_in_channel is not an empty channel; a partial read is no read."""
    out = read(_slack(), GRANTED, {},
               readers={"slack": _reader([{"ts": "1", "text": "x"}], ok=False,
                                         error="not_in_channel")},
               resolve=_resolve)
    assert out.ok is False and out.messages == ()
    assert out.reason == "wire-reported-failure"  # a code; the wire's own text is not copied


# --------------------------------------------------------------------------- #
# check_read: the pre-flight a poller must run, because a deny is invisible otherwise.
# --------------------------------------------------------------------------- #

def test_check_read_answers_without_a_credential_or_a_wire(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("check_read touched a credential or a wire")

    monkeypatch.setattr(reader_module, "credential_for", fail)
    monkeypatch.setattr(reader_module, "DEFAULT_READERS", {"slack": fail})
    assert check_read(_slack(), GRANTED).decision == "allow"
    assert check_read(_slack(), POST_ONLY).decision != "allow"
    assert check_read(_slack(), GRANTED, sensitivity_tags=("local-only",)).decision != "allow"


# --------------------------------------------------------------------------- #
# slack_fetcher: the zero-argument polling callable.
# --------------------------------------------------------------------------- #

def test_the_fetcher_is_zero_arg_and_returns_slack_shaped_messages():
    fetch = slack_fetcher("team_slack", GRANTED, thread_ts="1753.400",
                          registry_path=REG_PATH,
                          readers={"slack": _reader([{"ts": "1753.5", "text": "2"}])},
                          resolve=_resolve)
    assert [m["text"] for m in fetch()] == ["2"]


def test_the_fetcher_raises_rather_than_returning_an_empty_list_on_a_refusal():
    """An empty list means 'nothing yet, ask again' to a poller. A denial is not that:
    returned as [] it would be retried until the window closed, and a scope refusal would be
    indistinguishable from a channel where nobody answered."""
    fetch = slack_fetcher("team_slack", POST_ONLY, registry_path=REG_PATH,
                          readers={"slack": _reader([])}, resolve=_resolve)
    with pytest.raises(ReadRefused):
        fetch()


def test_the_fetcher_re_reads_the_registry_each_call(tmp_path):
    """A poll loop can outlive a revocation, so the grant is re-checked per poll rather than
    captured at process start."""
    import shutil

    live = tmp_path / "registry.yaml"
    shutil.copy(REG_PATH, live)
    fetch = slack_fetcher("team_slack", GRANTED, registry_path=live,
                          readers={"slack": _reader([{"ts": "1", "text": "ok"}])},
                          resolve=_resolve)
    assert [m["text"] for m in fetch()] == ["ok"]

    text = live.read_text(encoding="utf-8").replace(
        '          operations: ["post", "read-thread"]', '          operations: ["post"]')
    live.write_text(text, encoding="utf-8")
    with pytest.raises(ReadRefused):
        fetch()
