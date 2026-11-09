"""Attacks on the integrity seal, the mail senders, the gates, memory consolidation, artifact
freezing, credential handling and the hosted adapter, each with the behaviour that refuses it.

No test here opens a socket.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
import yaml

from omli.agents.reflector import Consolidation, Lesson, MemoryStore, Reflector
from omli.artifacts.provenance import Provenance
from omli.artifacts.schema import new_artifact
from omli.connections import authoring, reader, secrets
from omli.connections.schema import load_registry, parse_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate
from omli.connections.sender import _default_graph_mail_sender, _default_smtp_sender, dispatch
from omli.gates.post import PostGate
from omli.integrity import compute_chain, read_log_records, verify
from omli.integrity.__main__ import main as integrity_cli
from omli.integrity.keyring import DEFAULT_KEYRING
from omli.integrity.signer import DEFAULT_PUBKEY, PASSPHRASE_ENV, generate_keypair, verify_signature
from omli.models import ollama
from omli.models.errors import ModelError
from omli.models.provider import Message, ModelRequest, OpenAICompatibleProvider
from test_provider import _hosted_entry
from test_score_threshold_gate import _SCORING_PACK, _CannedProvider, _agent, _seed_decision

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "omli" / "defaults" / "connection_registry.example.yaml"
PW = "throwaway-passphrase"
ALLOWED = {"smtp": "ops@example.com", "graph-mail": "owner@example.com"}
STRANGER = "stranger@example.net"


# --------------------------------------------------------------------------- integrity
@pytest.fixture
def sealed(tmp_path, monkeypatch):
    """A signed, anchored log in a throwaway home with a throwaway key."""
    pytest.importorskip("cryptography")
    monkeypatch.setenv("OMLI_HOME", str(tmp_path))
    monkeypatch.setenv("OMLI_SEAL_KEY", str(tmp_path / "seal.key"))
    monkeypatch.setenv(PASSPHRASE_ENV, PW)
    _, _, key_id = generate_keypair(PW, tmp_path / "seal.key", tmp_path / DEFAULT_PUBKEY,
                                    keyring_path=tmp_path / DEFAULT_KEYRING)
    log = tmp_path / "var" / "artifacts.jsonl"
    log.parent.mkdir()
    log.write_text("".join(json.dumps({"x": v}) + "\n" for v in ("alpha", "bravo", "charlie")), encoding="utf-8")
    assert integrity_cli(["seal"]) == 0
    assert integrity_cli(["verify"]) == 0
    return {"home": tmp_path, "log": log, "seal": log.with_suffix(".seal.json"),
            "sig": log.with_name("artifacts.seal.sig.json"), "key_id": key_id}


def _rewrite_seal(path, **fields):
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_edited_log_with_rewritten_chain_and_signed_head_fails_every_layer(sealed, capsys):
    """The attack: edit a line, rewrite only the seal's chain, keep its signed head and count."""
    text = sealed["log"].read_text(encoding="utf-8")
    sealed["log"].write_text(text.replace("bravo", "EDITED"), encoding="utf-8")
    _rewrite_seal(sealed["seal"], chain=compute_chain(read_log_records(sealed["log"])))

    assert verify(sealed["log"], sealed["seal"]).status == "tampered"
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "TAMPERED" in capsys.readouterr().out


def test_seal_head_that_disagrees_with_its_chain_is_tampered(sealed):
    _rewrite_seal(sealed["seal"], head="0" * 64)
    assert verify(sealed["log"], sealed["seal"]).status == "tampered"


def test_seal_count_that_disagrees_with_its_chain_is_tampered(sealed):
    _rewrite_seal(sealed["seal"], count=2)
    assert verify(sealed["log"], sealed["seal"]).status == "tampered"


def test_seal_with_no_count_is_rejected(sealed):
    data = json.loads(sealed["seal"].read_text(encoding="utf-8"))
    del data["count"]
    sealed["seal"].write_text(json.dumps(data), encoding="utf-8")
    report = verify(sealed["log"], sealed["seal"])
    assert report.status == "corrupt" and not report.trustworthy
    assert integrity_cli(["verify"]) == 1


def test_appended_lines_verify_as_appended_and_trustworthy(sealed):
    with sealed["log"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"x": "delta"}) + "\n")
    report = verify(sealed["log"], sealed["seal"])
    assert report.status == "appended" and report.trustworthy


def test_revoked_key_cannot_escape_by_naming_an_unknown_key_id(sealed, capsys):
    """The attack: revoke the key, then change the id in the signature file to an unlisted one."""
    assert integrity_cli(["revoke", sealed["key_id"]]) == 0
    assert integrity_cli(["verify"]) == 1
    _rewrite_seal(sealed["sig"], pubkey_id="not-a-known-key")
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "UNKNOWN-KEY" in capsys.readouterr().out


def test_signature_naming_an_unknown_key_is_refused_with_a_live_keyring(sealed):
    """No revocation needed: with a keyring present, a key the keyring does not list is refused."""
    _rewrite_seal(sealed["sig"], pubkey_id="not-a-known-key")
    data = json.loads(sealed["seal"].read_text(encoding="utf-8"))
    got = verify_signature(data["head"], data["count"], sealed["sig"], sealed["home"] / DEFAULT_PUBKEY,
                           keyring_path=sealed["home"] / DEFAULT_KEYRING)
    assert not got.trustworthy and got.status == "unknown-key"


def test_absent_signature_under_a_keyring_with_no_active_key_is_missing(sealed):
    assert integrity_cli(["revoke", sealed["key_id"]]) == 0
    (sealed["home"] / DEFAULT_PUBKEY).unlink()
    sealed["sig"].unlink()
    data = json.loads(sealed["seal"].read_text(encoding="utf-8"))
    got = verify_signature(data["head"], data["count"], sealed["sig"], sealed["home"] / DEFAULT_PUBKEY,
                           keyring_path=sealed["home"] / DEFAULT_KEYRING)
    assert got.status == "missing" and not got.trustworthy


def test_crash_while_re_sealing_leaves_a_state_that_fails_verify(sealed, monkeypatch):
    """A signature for the new head beside the earlier seal must not verify."""
    import omli.integrity.__main__ as cli

    with sealed["log"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"x": "delta"}) + "\n")

    real_replace = cli.os.replace

    def crash(src, dst):
        if str(src).endswith(".staged"):
            raise OSError("crash before the seal is published")
        real_replace(src, dst)

    monkeypatch.setattr(cli.os, "replace", crash)
    assert integrity_cli(["seal"]) == 1  # reported, exit 1, not a traceback
    monkeypatch.undo()
    monkeypatch.setenv("OMLI_HOME", str(sealed["home"]))
    assert integrity_cli(["verify"]) == 1


# --------------------------------------------------------------------------- mail
class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, payload, credential, connection, action):
        self.calls.append(payload)
        return {"ok": True}


def _mail(kind):
    registry = load_registry(EXAMPLE)
    return next(c for c in registry.connections if c.target.kind == kind)


def _send(kind, payload, *, to=None):
    connection = _mail(kind)
    to = to or ALLOWED[kind]
    recorder = _Recorder()
    action = ConnectionAction(connection.connection_id, "send-email", path=to, sensitivity_tags=("public",),
                              sends_today=0)
    outcome = dispatch(connection, action, payload, senders={kind: recorder}, resolve=lambda *a, **k: "fake")
    return outcome, recorder


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
def test_an_allowed_message_with_allowed_copies_sends(kind):
    outcome, recorder = _send(kind, {"cc": [ALLOWED[kind]], "bcc": ALLOWED[kind], "to": [ALLOWED[kind]]})
    assert outcome.ok and len(recorder.calls) == 1


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_every_recipient_passes_the_gate(kind, field):
    outcome, recorder = _send(kind, {field: [ALLOWED[kind], STRANGER]})
    assert outcome.ok is False and outcome.decision != "allow"
    assert recorder.calls == []


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
@pytest.mark.parametrize("field", ["cc", "bcc"])
def test_a_second_address_hidden_in_one_string_is_refused(kind, field):
    outcome, recorder = _send(kind, {field: f"{STRANGER},{ALLOWED[kind]}"})
    assert outcome.ok is False and recorder.calls == []


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
def test_a_second_address_hidden_in_the_addressed_recipient_is_refused(kind):
    outcome, recorder = _send(kind, {}, to=f"{STRANGER},{ALLOWED[kind]}")
    assert outcome.ok is False and recorder.calls == []


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
@pytest.mark.parametrize("key", ["from", "from_addr", "sender"])
def test_a_payload_sender_override_is_refused(kind, key):
    outcome, recorder = _send(kind, {key: "someone-else@example.com"})
    assert outcome.ok is False and outcome.decision == "deny"
    assert recorder.calls == []


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
@pytest.mark.parametrize("key,value", [("attachments", [{"path": "x"}]), ("attachments", []), ("attachment", "x")])
def test_an_attachment_key_is_refused(kind, key, value):
    outcome, recorder = _send(kind, {key: value})
    assert outcome.ok is False and outcome.decision == "deny"
    assert recorder.calls == []


def test_the_smtp_wire_sends_from_the_principal_and_delivers_to_bcc_without_the_header(monkeypatch):
    """Runs the REAL `smtplib.SMTP.send_message` with the server-facing calls (greeting,
    `starttls`, `login`, `sendmail`) stubbed, so the check is on what would go on the wire: the
    envelope recipients and the transmitted bytes."""
    import smtplib

    sent = {}
    bcc_only = "hidden@example.com"

    class WireSMTP(smtplib.SMTP):
        def __init__(self, *args, **kwargs):
            super().__init__()  # no host: nothing connects

        def __exit__(self, *args):
            return False

        def starttls(self, *args, **kwargs):
            return (220, b"ok")

        def login(self, user, password, **kwargs):
            sent["login"] = user
            return (235, b"ok")

        def ehlo_or_helo_if_needed(self):
            return None  # no server to greet

        def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
            sent.update(envelope_from=from_addr, envelope_to=list(to_addrs), data=msg)
            return {}

    monkeypatch.setattr("smtplib.SMTP", WireSMTP)
    connection = _mail("smtp")
    allowed = ALLOWED["smtp"]
    action = ConnectionAction(connection.connection_id, "send-email", path=allowed, sensitivity_tags=("public",))
    _default_smtp_sender({"from": "someone-else@example.com", "bcc": [bcc_only], "body": "x"},
                         "fake", connection, action)
    data = sent["data"] if isinstance(sent["data"], bytes) else sent["data"].encode("utf-8")
    assert sent["login"] == connection.target.principal
    assert sent["envelope_from"] == connection.target.principal
    assert bcc_only in sent["envelope_to"] and allowed in sent["envelope_to"]
    assert b"Bcc" not in data and bcc_only.encode() not in data
    assert b"someone-else" not in data


def test_the_graph_wire_sends_from_the_principal_and_carries_no_attachment(monkeypatch, tmp_path):
    secret_file = tmp_path / "private.txt"
    secret_file.write_text("do-not-send", encoding="utf-8")
    requests = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"access_token": "t"}).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return Response()

    from urllib.parse import quote

    from omli.net import SAFE_OPENER

    monkeypatch.setattr(SAFE_OPENER, "open", fake_urlopen)
    connection = _mail("graph-mail")
    allowed = ALLOWED["graph-mail"]
    action = ConnectionAction(connection.connection_id, "send-email", path=allowed, sensitivity_tags=("public",))
    _default_graph_mail_sender(
        {"from": "someone-else@example.com", "attachments": [{"path": str(secret_file)}], "bcc": [allowed]},
        "fake", connection, action)
    send = requests[-1]
    body = json.loads(send.data.decode("utf-8"))
    assert send.full_url.endswith(f"/users/{quote(connection.target.principal)}/sendMail")
    assert "someone-else" not in send.full_url and "someone-else" not in send.data.decode("utf-8")
    assert "do-not-send" not in send.data.decode("utf-8")
    assert "attachments" not in body["message"]
    assert body["message"]["bccRecipients"] == [{"emailAddress": {"address": allowed}}]


@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
def test_a_sender_that_raises_comes_back_as_an_outcome_without_the_error_text(kind):
    connection = _mail(kind)

    def boom(*args, **kwargs):
        raise OSError("wire down, token=fake-secret")

    action = ConnectionAction(connection.connection_id, "send-email", path=ALLOWED[kind], sensitivity_tags=("public",),
                              sends_today=0)
    outcome = dispatch(connection, action, {}, senders={kind: boom}, resolve=lambda *a, **k: "fake")
    assert outcome.ok is False and outcome.decision == "allow"
    assert outcome.reason == "wire-raised" and outcome.error_type == "OSError"
    assert "fake-secret" not in repr(outcome)


# --------------------------------------------------------------------------- gates
@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_post_gate_rejects_a_non_finite_score(score):
    result = PostGate().check({"score": score}, score_field="score", threshold=0.5)
    assert result.passed is False and "schema_violation" in result.tags


def test_post_gate_rejects_a_boolean_score():
    assert PostGate().check({"score": True}, score_field="score", threshold=0.5).passed is False


def test_json_nan_from_a_model_writes_nothing(tmp_path):
    from omli.artifacts.store import ArtifactStore
    from omli.packs.loader import load_pack

    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    decision = _seed_decision(store)
    out = _agent(load_pack(_SCORING_PACK), _CannedProvider('{"score": NaN}'), store).run(decision, "WorkResult")
    assert out is None and store.by_kind("WorkResult") == ()


def test_a_declared_score_is_required_even_when_the_pack_does_not_list_it(tmp_path):
    from omli.artifacts.store import ArtifactStore
    from omli.packs.loader import load_pack

    pack = load_pack(_SCORING_PACK)
    unlisted = dataclasses.replace(
        pack, required_output_fields=tuple(f for f in pack.required_output_fields if f != pack.scoring.score_field))
    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    decision = _seed_decision(store)
    out = _agent(unlisted, _CannedProvider("plain prose, no score"), store).run(decision, "WorkResult")
    assert out is None and store.by_kind("WorkResult") == ()


def test_a_connection_action_with_no_tags_is_denied_as_sensitivity_missing():
    connection = load_registry(EXAMPLE).get("example_repo")
    action = ConnectionAction("example_repo", "read", path="src/lib/utils.py")
    decision = connection_scope_gate(action, connection)
    assert (decision.decision, decision.reason) == ("deny", "sensitivity-missing")


def test_sensitivity_missing_does_not_soften_to_an_escalation():
    connection = load_registry(EXAMPLE).get("example_repo")
    opted_in = dataclasses.replace(connection, approval=dataclasses.replace(
        connection.approval, escalate_on=("sensitivity-mismatch", "out-of-selection")))
    assert connection_scope_gate(ConnectionAction("example_repo", "read", path="src/lib/utils.py"), opted_in).decision == "deny"


def test_a_read_with_no_tags_is_refused_by_default():
    connection = load_registry(EXAMPLE).get("team_slack")
    out = reader.read(connection, "CEXAMPLE001", {}, readers={"slack": lambda *a: {"ok": True, "messages": [{"t": 1}]}},
                      resolve=lambda *a, **k: "fake")
    assert out.ok is False and out.reason == "sensitivity-missing"


# --------------------------------------------------------------------------- consolidations
def _two_approved(store):
    a = store.append(Lesson(text="first approved", source_artifact_ids=()))
    b = store.append(Lesson(text="second approved", source_artifact_ids=()))
    store.approve(a.id)
    store.approve(b.id)
    return a, b


def test_a_deterministic_consolidation_refuses_caller_text():
    store = MemoryStore()
    a, b = _two_approved(store)
    with pytest.raises(ValueError):
        Reflector(store).consolidate("smuggled instructions", supersedes=(a.id, b.id))
    with pytest.raises(ValueError):
        store.append_consolidation(Consolidation(text="smuggled instructions", supersedes=(a.id, b.id)))
    assert store.consolidations() == ()


def test_a_deterministic_consolidation_renders_only_approved_lesson_text():
    store = MemoryStore()
    a, b = _two_approved(store)
    made = store.append_consolidation(Consolidation(text="", supersedes=(a.id, b.id)))
    assert made.text == "first approved\nsecond approved"
    assert store.live_consolidations() == (made,)
    assert {getattr(x, "id") for x in store.active()} == {made.id}


def test_a_deterministic_consolidation_of_an_unapproved_lesson_is_refused():
    store = MemoryStore()
    a, _ = _two_approved(store)
    pending = store.append(Lesson(text="not approved", source_artifact_ids=()))
    with pytest.raises(ValueError):
        store.append_consolidation(Consolidation(text="", supersedes=(a.id, pending.id)))


def test_a_model_consolidation_needs_text_and_is_inactive_until_approved():
    store = MemoryStore()
    a, b = _two_approved(store)
    with pytest.raises(ValueError):
        store.append_consolidation(Consolidation(text="", supersedes=(a.id,), mode="model"))
    made = Reflector(store).consolidate("model summary", supersedes=(a.id, b.id), mode="model")
    assert store.live_consolidations() == ()
    assert store.approve(made.id)
    assert store.live_consolidations() == (made,)


def test_an_edited_deterministic_consolidation_line_does_not_take_effect(tmp_path):
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a, b = _two_approved(store)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "consolidation", "id": "forged", "text": "smuggled instructions",
                             "supersedes": [a.id, b.id], "mode": "deterministic"}) + "\n")
    assert MemoryStore(log).live_consolidations() == ()


def test_a_lesson_cannot_arrive_approved():
    with pytest.raises(ValueError):
        MemoryStore().append(Lesson(text="self approved", source_artifact_ids=(), approved=True))


def test_a_lesson_line_marked_approved_needs_an_approval_marker(tmp_path):
    log = tmp_path / "lessons.jsonl"
    log.write_text(json.dumps({"type": "lesson", "id": "x", "text": "t", "approved": True}) + "\n", encoding="utf-8")
    assert MemoryStore(log).approved() == ()


# --------------------------------------------------------------------------- deep freeze
def _artifact(payload):
    return new_artifact("CanonicalInput", payload, Provenance(agent="a", pack_name="p", pack_version="1"))


def test_nested_payload_mutation_raises():
    art = _artifact({"a": {"b": [1, {"c": 2}]}})
    with pytest.raises(TypeError):
        art.payload["a"]["x"] = 1
    with pytest.raises(TypeError):
        art.payload["a"]["b"][1]["c"] = 3
    assert isinstance(art.payload["a"]["b"], tuple)


def test_mutating_the_callers_input_does_not_reach_the_artifact():
    nested = {"b": [1]}
    art = _artifact({"a": nested})
    nested["b"].append(2)
    nested["new"] = True
    assert art.payload["a"]["b"] == (1,) and "new" not in art.payload["a"]


def test_serialization_of_a_nested_payload_is_byte_identical_to_the_plain_form():
    payload = {"z": 1, "a": {"list": [1, [2, {"k": None}], "é"], "t": (3, 4)}, "e": {}, "l": []}
    art = _artifact(payload)
    expected_payload = '{"z": 1, "a": {"list": [1, [2, {"k": null}], "\\u00e9"], "t": [3, 4]}, "e": {}, "l": []}'
    assert json.dumps(art.to_dict()["payload"]) == expected_payload
    assert json.dumps(art.to_dict()["payload"]) == json.dumps(payload)


def test_a_log_line_reloads_to_the_same_bytes(tmp_path):
    from omli.artifacts.schema import Artifact

    line = json.dumps(_artifact({"a": {"b": [1, {"c": [2]}]}}).to_dict())
    assert json.dumps(Artifact.from_dict(json.loads(line)).to_dict()) == line


def test_prompt_text_shows_nested_payloads_in_their_plain_form():
    from omli.agents.base import _stringify

    assert _stringify(_artifact({"a": {"b": [1]}})) == "a={'b': [1]}"


# --------------------------------------------------------------------------- credentials in errors
def test_a_rejected_inline_credential_is_not_in_the_registry_error():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["connections"][0]["target"]["credential_ref"] = "ghp_not_a_real_token_value"
    with pytest.raises(ValueError) as caught:
        parse_registry(data)
    assert "ghp_not_a_real_token_value" not in str(caught.value)


@pytest.mark.parametrize("bad", [
    "ghp_not_a_real_token_value",
    "secret://a\\ghp_not_a_real_token_value",
    "secret://a//ghp_not_a_real_token_value",
    "secret://../ghp_not_a_real_token_value",
    "secret://a:ghp_not_a_real_token_value",
])
def test_a_rejected_reference_is_not_in_the_resolver_error(bad):
    with pytest.raises(ValueError) as caught:
        secrets.validate_ref(bad)
    assert "ghp_not_a_real_token_value" not in str(caught.value)


# --------------------------------------------------------------------------- hosted API key
KEY = "fake-hosted-key-value"


def _keyed(monkeypatch):
    monkeypatch.setenv("TEST_HOSTED_MODEL_KEY", KEY)
    return dataclasses.replace(_hosted_entry(), api_key_env="TEST_HOSTED_MODEL_KEY")


def _request():
    return ModelRequest(messages=(Message("user", "hello"),))


def _answer(text="answer"):
    return json.dumps({"model": "m", "choices": [{"message": {"content": text}, "finish_reason": "stop"}]}).encode("utf-8")


def test_the_hosted_adapter_sends_the_key_in_the_auth_header_and_nowhere_else(monkeypatch):
    seen = []

    def transport(url, body, timeout, headers=None):
        seen.append((url, body, headers))
        return _answer()

    response = OpenAICompatibleProvider(transport=transport).invoke(_keyed(monkeypatch), _request())
    url, body, headers = seen[0]
    assert (headers or {}).get("Authorization") == f"Bearer {KEY}"
    assert KEY not in url and KEY.encode("utf-8") not in body
    assert KEY not in repr(response)


def test_the_default_transport_puts_the_key_on_the_wire(monkeypatch):
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return _answer()

    def fake_open(request, timeout=None):
        captured.append(request)
        return Response()

    monkeypatch.setattr(ollama._OPENER, "open", fake_open)
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    OpenAICompatibleProvider().invoke(_keyed(monkeypatch), _request())
    assert captured[0].get_header("Authorization") == f"Bearer {KEY}"


def test_a_key_echoed_by_the_server_is_not_in_the_response(monkeypatch):
    def transport(url, body, timeout, headers=None):
        return _answer(f"your key is {KEY}")

    response = OpenAICompatibleProvider(transport=transport).invoke(_keyed(monkeypatch), _request())
    assert KEY not in repr(response)


def _chain(exc):
    """The exception and everything reachable through __cause__ and __context__."""
    seen, todo = [], [exc]
    while todo:
        e = todo.pop()
        if e is None or any(e is s for s in seen):
            continue
        seen.append(e)
        todo += [e.__cause__, e.__context__]
    return seen


@pytest.mark.parametrize("error", [OSError(f"refused for {KEY}"), RuntimeError(f"bad {KEY}"),
                                   ModelError(f"bad {KEY}"), TimeoutError(f"slow {KEY}")])
def test_a_key_in_a_transport_error_is_not_in_the_raised_error(monkeypatch, error):
    def transport(url, body, timeout, headers=None):
        raise error

    with pytest.raises(ModelError) as caught:
        OpenAICompatibleProvider(transport=transport).invoke(_keyed(monkeypatch), _request())
    for e in _chain(caught.value):
        assert KEY not in str(e) and KEY not in repr(e) and KEY not in repr(e.args)


def test_an_unset_key_fails_loud_without_repeating_the_field_value(monkeypatch):
    entry = _keyed(monkeypatch)
    monkeypatch.delenv("TEST_HOSTED_MODEL_KEY")
    with pytest.raises(ModelError, match="needs an API key") as caught:
        OpenAICompatibleProvider(transport=lambda *a, **k: _answer()).invoke(entry, _request())
    assert "TEST_HOSTED_MODEL_KEY" not in str(caught.value)


# --------------------------------------------------------------------------- stated contracts
def test_the_sink_persists_exactly_what_the_agent_emits_plus_the_traffic_guard():
    from omli.events.sink import PERSISTED_EVENTS

    source = (ROOT / "omli" / "agents" / "base.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'_emit\(\s*"([a-z_]+)"', source))
    assert PERSISTED_EVENTS == emitted | {"traffic_gate"}


def test_a_requirement_against_a_model_with_no_text_surface_raises_value_error():
    """`Router.resolve` promises ValueError on any problem, including a text requirement
    against a kind whose context window and output limit are None."""
    from omli.core.router import Router
    from omli.models.registry import parse_registry
    from omli.packs.schema import ModelRequirements, ModelSelection, Pack

    data = yaml.safe_load((ROOT / "omli" / "defaults" / "model_registry.example.yaml")
                          .read_text(encoding="utf-8"))
    data["models"].append({
        "model_id": "local-sfx", "display_name": "local-sfx", "family": "audio",
        "provider_kind": "local-audio", "adapter": "audio-example",
        "endpoint": "http://localhost:8189",
        "pin": {"ref": "0123456789abcdef0123456789abcdef01234567", "quant": None},
        "reproducible": True, "status": "staging",
        "capabilities": {"structured_output": "none", "sample_rate_hz": 44100, "channels": 2,
                         "max_duration_s": 47.0, "streaming": False},
    })
    pack = Pack(name="p", version="1", identity="i", prompt_template="{input}",
                model_selection=ModelSelection(model_id="local-sfx"),
                model_requirements=ModelRequirements(min_context_window=1, min_max_output_tokens=1))
    with pytest.raises(ValueError, match="does not meet model_requirements"):
        Router(parse_registry(data)).resolve(pack)


def test_an_invalid_staging_draft_does_not_survive(tmp_path, monkeypatch):
    from omli.packs import promotion

    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({"name": "n", "version": "1.0.0", "identity": "i", "prompt_template": "{input}"}), encoding="utf-8")

    def reject(path):
        raise ValueError("invalid draft")

    monkeypatch.setattr(promotion, "load_pack", reject)
    memory = MemoryStore()
    lesson = memory.append(Lesson(text="t", source_artifact_ids=()))
    memory.approve(lesson.id)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="invalid draft"):
        promotion.draft_staging_pack(base, memory, lesson.id, promoted_by="human", out_dir=out)
    assert list(out.iterdir()) == []


def test_a_model_promotion_must_name_a_new_version():
    from omli.governance.promotion import propose_promotion

    data = yaml.safe_load((ROOT / "tests" / "data" / "registry.yaml").read_text(encoding="utf-8"))
    current = data["metadata"]["registry_version"]
    model_id = data["models"][0]["model_id"]
    with pytest.raises(ValueError, match="new registry version"):
        propose_promotion(data, model_id, "staging", new_version=current)


def test_dry_run_keeps_its_probes_when_the_selection_is_long():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    connection = data["connections"][0]
    for i in range(12):
        connection["selection"]["resources"].append(
            {"id": f"extra{i}/**", "kind": "path-glob", "selected": True, "operations": ["read"]})
    labels = [r["label"] for r in authoring.dry_run(data)["results"]]
    assert "forbidden-path probe" in labels and "sensitivity mismatch (fail closed)" in labels


def test_the_dry_run_cli_works_on_a_registry_whose_first_id_is_different(tmp_path, capsys):
    from omli.connections.__main__ import main

    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["connections"][0]["connection_id"] = "renamed"
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "connection-id-mismatch" not in output and "[   ALLOW]" in output


def test_a_reader_that_raises_comes_back_as_an_outcome():
    connection = load_registry(EXAMPLE).get("team_slack")

    def boom(*args):
        raise OSError("wire down")

    out = reader.read(connection, "CEXAMPLE001", {}, sensitivity_tags=("internal",),
                      readers={"slack": boom}, resolve=lambda *a, **k: "fake")
    assert out.ok is False and out.messages == ()
    assert out.reason == "wire-raised" and out.error_type == "OSError"


def test_the_fetcher_raises_read_refused_for_a_wire_failure_and_for_a_removed_connection(tmp_path):
    def boom(*args):
        raise OSError("wire down")

    fetch = reader.slack_fetcher("team_slack", "CEXAMPLE001", sensitivity_tags=("internal",), registry_path=EXAMPLE,
                                 readers={"slack": boom}, resolve=lambda *a, **k: "fake")
    with pytest.raises(reader.ReadRefused):
        fetch()
    gone = reader.slack_fetcher("no_such_connection", "CEXAMPLE001", sensitivity_tags=("internal",),
                                registry_path=EXAMPLE, resolve=lambda *a, **k: "fake")
    with pytest.raises(reader.ReadRefused):
        gone()
