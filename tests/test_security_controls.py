"""Positive controls for tests/test_security_regressions.py: the safe paths work."""

from __future__ import annotations

import pytest


def test_nested_secret_ref_resolves_from_a_file(monkeypatch, tmp_path):
    from omli.connections import secrets

    monkeypatch.setattr(secrets, "_SECRETS_DIR", tmp_path)
    (tmp_path / "connections" / "svc").mkdir(parents=True)
    (tmp_path / "connections" / "svc" / "key").write_text("fake-value", encoding="utf-8")
    monkeypatch.delenv("OMLI_SECRET_CONNECTIONS__SVC__KEY", raising=False)
    assert secrets.resolve_secret("secret://connections/svc/key") == "fake-value"


@pytest.mark.parametrize("endpoint", [
    "http://localhost:11434", "http://LOCALHOST:11434", "http://127.0.0.1:11434", "http://127.5.5.5",
    "http://[::1]:11434", "https://localhost",
])
def test_loopback_endpoint_parses(endpoint):
    from omli.models.registry import parse_registry
    from test_security_regressions import _local_model

    assert parse_registry(_local_model(endpoint)).get("m").endpoint == endpoint


def test_a_registry_loaded_entry_reaches_the_sender_and_reader():
    from omli.connections.reader import read
    from omli.connections.scope_gate import ConnectionAction
    from omli.connections.sender import dispatch
    from test_security_regressions import _row, _slack_entry, _Spy

    entry = _slack_entry()
    sender, reader = _Spy(), _Spy()
    action = ConnectionAction(entry.connection_id, "post", path=_row(entry, "post").id, sensitivity_tags=("internal",))
    assert dispatch(entry, action, {"text": "x"}, senders={"slack": sender}, resolve=lambda *a, **k: "fake").ok
    assert read(entry, _row(entry, "read-thread").id, {}, sensitivity_tags=("internal",),
                readers={"slack": reader}, resolve=lambda *a, **k: "fake").ok
    assert sender.calls == 1 and reader.calls == 1
