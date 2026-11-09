"""Fail-closed boundaries: trust files that cannot be read, wire outcomes in fixed fields,
consolidations that follow their lessons, the hosted key, traffic thresholds and registry caps.

Every test docstring starts with its kind:

* REGRESSION   — reproduces an attack or a malformed input and asserts the fail-closed
  behaviour that answers it.
* PRESERVATION — asserts the expected result for well-formed input on the same path.

The parenthesised word after the kind names the boundary under test.

No test here opens a socket, and no test touches a key outside its temporary directory.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from omli.agents.reflector import Consolidation, Lesson, MemoryStore
from omli.connections import reader
from omli.connections.schema import load_registry, parse_registry as parse_connections
from omli.connections.scope_gate import ConnectionAction
from omli.connections.sender import dispatch
from omli.gates.traffic import parse_traffic_policy
from omli.integrity import verify
from omli.integrity.__main__ import main as integrity_cli
from omli.integrity.keyring import DEFAULT_KEYRING
from omli.models.errors import ModelError
from omli.models.provider import OpenAICompatibleProvider, default_provider_table, resolve_api_key
from omli.models.registry import load_registry as load_models, parse_registry as parse_models
from test_trust_boundaries import KEY, _keyed, _mail, _request, signed  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "omli" / "defaults" / "connection_registry.example.yaml"
MODELS = ROOT / "omli" / "defaults" / "model_registry.example.yaml"
TRAFFIC = ROOT / "packs" / "traffic_guard.yaml"
SECRET = "s3cr+t/v@lue=Xy"

#: The closed set of outcome reasons, written out here so the test does not take it from the
#: code under test. "" is success.
REASONS = {
    "", "entry-not-registry-loaded", "forbidden-path", "browse-read-only", "egress-connect-only",
    "slack-messaging-only", "send-only", "malformed-recipient", "sensitivity-missing",
    "sensitivity-mismatch", "out-of-selection", "read-required-for-change",
    "blast-radius-exceeded", "sends-today-required", "change-class-op", "gate-refused",
    "attachments-not-supported", "sender-override-refused", "recipient-refused",
    "no-wire-for-kind", "credential-unavailable", "wire-raised", "wire-returned-no-result",
    "wire-reported-failure", "adapter-error",
}
SENDER_FIELDS = ("ok", "decision", "kind", "target", "reason", "error_type")
READER_FIELDS = ("ok", "decision", "kind", "target", "reason", "error_type", "messages")

ENCODINGS = {
    "raw": lambda s: s,
    "json-partial": lambda s: s[:3] + "".join(f"\\u{ord(c):04x}" for c in s[3:]),
    "json-one-char": lambda s: s.replace("+", "\\u002B"),
    "url-mixed-case": lambda s: "".join(f"%{ord(c):02x}" if i % 2 else f"%{ord(c):02X}"
                                        for i, c in enumerate(s)),
    "base64": lambda s: base64.b64encode(s.encode()).decode(),
    "html-entity": lambda s: "".join(f"&#x{ord(c):x};" for c in s),
}


# =========================================================================== integrity
def _trust_path(signed, which):
    return {"seal": signed["seal"], "sig": signed["sig"],
            "keyring": signed["home"] / DEFAULT_KEYRING,
            "anchor": signed["home"] / "integrity" / "artifacts.heads.jsonl"}[which]


DEEP = "[" * 200_000 + "]" * 200_000


@pytest.mark.parametrize("which", ["seal", "sig", "keyring", "anchor"])
def test_deeply_nested_json_in_a_trust_file_is_a_clean_corrupt_report(signed, capsys, which):
    """REGRESSION (integrity): JSON nested deeper than the parser's recursion limit, in the seal,
    signature, keyring or anchor file, gives CORRUPT and exit 1, not an uncaught RecursionError."""
    _trust_path(signed, which).write_text(DEEP + "\n", encoding="utf-8")
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    out = capsys.readouterr().out
    assert "CORRUPT" in out and "Traceback" not in out


@pytest.mark.parametrize("which", ["seal", "sig", "keyring", "anchor"])
def test_a_trust_path_that_is_a_directory_is_a_clean_corrupt_report(signed, capsys, which):
    """REGRESSION (integrity): a trust-file path that is a directory (PermissionError or
    IsADirectoryError on read) gives CORRUPT and exit 1, not an uncaught OSError."""
    path = _trust_path(signed, which)
    path.unlink()
    path.mkdir()
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "CORRUPT" in capsys.readouterr().out


def test_a_seal_path_that_is_a_directory_reads_corrupt_in_the_library(tmp_path):
    """REGRESSION (integrity): `verify` itself reports `corrupt` for a seal path that is a
    directory."""
    log = tmp_path / "a.jsonl"
    log.write_bytes(b'{"x": 1}\n')
    (tmp_path / "a.seal.json").mkdir()
    report = verify(log)
    assert report.status == "corrupt" and not report.trustworthy


def test_a_log_path_that_cannot_be_read_reads_corrupt(tmp_path):
    """REGRESSION (integrity): a log path that cannot be read gives `corrupt`, not an uncaught
    OSError."""
    log = tmp_path / "a.jsonl"
    log.mkdir()
    report = verify(log, tmp_path / "a.seal.json")
    assert report.status == "corrupt" and not report.trustworthy


@pytest.mark.parametrize("error", [MemoryError(), OSError("disk"), RuntimeError("other")])
def test_any_other_failure_of_the_cli_is_reported_as_corrupt(signed, capsys, monkeypatch, error):
    """REGRESSION (integrity): an ``Exception`` raised inside the CLI (here MemoryError,
    OSError or RuntimeError) is a CORRUPT report with exit 1."""
    import omli.integrity.__main__ as cli

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "verify", boom)
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "CORRUPT" in capsys.readouterr().out


def test_an_intact_signed_log_verifies_ok(signed, capsys):
    """PRESERVATION (integrity): an intact, signed, anchored log verifies with exit 0, reported
    OK and SIGNED and not CORRUPT."""
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 0
    out = capsys.readouterr().out
    assert "integrity: OK" in out and "SIGNED" in out and "CORRUPT" not in out


# =========================================================================== wires
def _fields(out, names):
    return {f.name for f in dataclasses.fields(out)} == set(names)


def _assert_fixed(out, kind, action_path, *, names=SENDER_FIELDS, error_type=""):
    """The outcome has exactly the fixed fields, the connection's target kind, the action's
    target, a reason from the closed set, a known decision, and exactly `error_type`."""
    assert _fields(out, names), "the outcome has a field outside the fixed set"
    assert out.kind == kind
    assert out.reason in REASONS
    assert out.target == action_path
    assert out.decision in ("allow", "deny", "escalate")
    assert out.error_type == error_type


_NO_PAYLOAD = object()


def _send(sender, *, kind="smtp", payload=_NO_PAYLOAD, resolve=None):
    connection = _mail(kind)
    path = {"smtp": "ops@example.com", "graph-mail": "owner@example.com"}[kind]
    action = ConnectionAction(connection.connection_id, "send-email", path=path,
                              sensitivity_tags=("public",), sends_today=0)
    out = dispatch(connection, action, {} if payload is _NO_PAYLOAD else payload, senders={kind: sender},
                   resolve=resolve or (lambda *a, **k: SECRET))
    return out, path


@pytest.mark.parametrize("encoding", list(ENCODINGS))
@pytest.mark.parametrize("kind", ["smtp", "graph-mail"])
def test_a_sender_returning_the_credential_gives_only_fixed_fields(kind, encoding):
    """REGRESSION (wires): a wire whose result carries the credential, in any encoding and in any
    key, produces an outcome built only from the fixed set; nothing it returned is copied."""
    echoed = ENCODINGS[encoding](SECRET)

    def echo(payload, credential, connection, action):
        return {"ok": False, "error": echoed, "detail": {"echo": echoed}, "to": echoed}

    out, path = _send(echo, kind=kind)
    _assert_fixed(out, kind, path)
    assert out.ok is False and out.reason == "wire-reported-failure"


@pytest.mark.parametrize("encoding", list(ENCODINGS))
def test_a_sender_raising_the_credential_gives_only_fixed_fields(encoding):
    """REGRESSION (wires): an exception whose text carries the credential, in any encoding,
    becomes an outcome with the reason `wire-raised` and the exception's type name only."""
    echoed = ENCODINGS[encoding](SECRET)

    def boom(*args):
        raise ConnectionError(echoed)

    out, path = _send(boom)
    _assert_fixed(out, "smtp", path, error_type="ConnectionError")
    assert out.reason == "wire-raised"


def test_an_exception_class_named_after_the_credential_does_not_name_the_outcome():
    """REGRESSION (wires): a class made at run time cannot put its own name into the outcome;
    the nearest real class in its lineage is reported instead."""
    made_up = type("s3crXvalue", (RuntimeError,), {})

    def boom(*args):
        raise made_up("x")

    out, path = _send(boom)
    _assert_fixed(out, "smtp", path, error_type="RuntimeError")


@pytest.mark.parametrize("result", [
    collections.namedtuple("Result", "ok error")(True, SECRET),   # a namedtuple, not a dict
    ["ok", True],                                                  # a list
    None,
], ids=["namedtuple", "list", "none"])
def test_a_sender_result_that_is_not_a_dict_becomes_an_outcome(result):
    """REGRESSION (wires): a namedtuple, a list or None as the sender's result is a failure
    outcome, never a crash and never a success."""
    out, path = _send(lambda *a: result)
    _assert_fixed(out, "smtp", path)
    assert out.ok is False and out.reason == "wire-returned-no-result"


def test_a_namedtuple_inside_the_sender_result_is_not_copied_or_walked():
    """REGRESSION (wires): a namedtuple anywhere in a dict result is never iterated, indexed,
    measured or turned into text by `dispatch`, and the outcome is exactly the fixed success
    outcome with no field holding the namedtuple's text."""
    touched = []

    class Detail(collections.namedtuple("Detail", "a b")):
        def __iter__(self):
            touched.append("iter")
            return super().__iter__()

        def __getitem__(self, index):
            touched.append("getitem")
            return super().__getitem__(index)

        def __len__(self):
            touched.append("len")
            return super().__len__()

        def __repr__(self):
            touched.append("repr")
            return super().__repr__()

        def __str__(self):
            touched.append("str")
            return super().__str__()

    nt = Detail(SECRET, 1)
    out, path = _send(lambda *a: {"ok": True, "detail": {"x": nt}, "id": nt})
    _assert_fixed(out, "smtp", path)
    assert dataclasses.astuple(out) == (True, "allow", "smtp", path, "", "")
    assert touched == []
    assert all(SECRET not in str(value) for value in dataclasses.astuple(out))


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, SECRET),
                                   KeyError(SECRET), OSError(SECRET)],
                         ids=["unicode-decode", "key-error", "os-error"])
def test_any_resolver_failure_becomes_an_outcome(error):
    """REGRESSION (wires): an ``Exception`` from the credential resolver (here
    UnicodeDecodeError, KeyError or OSError) becomes a `credential-unavailable` outcome naming
    only its type; the wire is never called."""
    calls = []

    def resolve(*args, **kwargs):
        raise error

    out, path = _send(lambda *a: calls.append(a) or {"ok": True}, resolve=resolve)
    _assert_fixed(out, "smtp", path, error_type=type(error).__name__)
    assert out.ok is False and out.reason == "credential-unavailable" and calls == []


@pytest.mark.parametrize("payload,error_type", [(None, "TypeError"), ("a string", "AttributeError"),
                                                (42, "TypeError")], ids=["none", "str", "int"])
def test_a_payload_of_the_wrong_type_becomes_an_outcome(payload, error_type):
    """REGRESSION (wires): an exception before the wire (here, a payload that is not a mapping)
    becomes a denied `adapter-error` outcome naming the exception's type; nothing is sent."""
    calls = []
    out, path = _send(lambda *a: calls.append(a) or {"ok": True}, payload=payload)
    _assert_fixed(out, "smtp", path, error_type=error_type)
    assert out.ok is False and out.decision == "deny" and out.reason == "adapter-error"
    assert calls == []


def test_a_clean_send_succeeds_with_empty_reason():
    """PRESERVATION (wires): an allowed send through a sender that reports success is ok, with
    an empty reason and the action's target."""
    out, path = _send(lambda *a: {"ok": True})
    assert out.ok is True and out.decision == "allow" and out.reason == "" and out.target == path


def _read(wire, *, resolve=None):
    connection = load_registry(EXAMPLE).get("team_slack")
    return reader.read(connection, "CEXAMPLE001", {}, sensitivity_tags=("internal",),
                       readers={"slack": wire}, resolve=resolve or (lambda *a, **k: SECRET))


@pytest.mark.parametrize("encoding", list(ENCODINGS))
def test_a_reader_failure_carrying_the_credential_gives_only_fixed_fields(encoding):
    """REGRESSION (wires): a reader reporting failure with the credential in its error text, in
    any encoding, gives an outcome of fixed fields; nothing it returned is copied."""
    echoed = ENCODINGS[encoding](SECRET)
    out = _read(lambda *a: {"ok": False, "error": echoed, "messages": [{"text": echoed}]})
    _assert_fixed(out, "slack", "CEXAMPLE001", names=READER_FIELDS)
    assert out.ok is False and out.reason == "wire-reported-failure" and out.messages == ()


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, SECRET),
                                   ValueError(SECRET)], ids=["unicode-decode", "value-error"])
def test_any_reader_or_resolver_exception_becomes_a_read_outcome(error):
    """REGRESSION (wires): on the read path, an exception from the resolver or the wire
    becomes an outcome naming only its type."""
    def raise_it(*args, **kwargs):
        raise error

    out = _read(lambda *a: {"ok": True}, resolve=raise_it)
    _assert_fixed(out, "slack", "CEXAMPLE001", names=READER_FIELDS, error_type=type(error).__name__)
    assert out.reason == "credential-unavailable"
    out = _read(raise_it)
    _assert_fixed(out, "slack", "CEXAMPLE001", names=READER_FIELDS, error_type=type(error).__name__)
    assert out.reason == "wire-raised"


def test_read_messages_are_a_separate_untrusted_field():
    """PRESERVATION (wires): a successful read returns the channel's messages unchanged in their
    own field; the status fields stay fixed."""
    msgs = [{"ts": "1", "text": "hello"}]
    out = _read(lambda *a: {"ok": True, "messages": msgs})
    assert out.ok is True and out.reason == "" and out.messages == tuple(msgs)


# =========================================================================== memory
def _approved_lesson(store, text="a lesson"):
    lesson = store.append(Lesson(text=text, source_artifact_ids=()))
    store.approve(lesson.id)
    return lesson


def test_a_model_consolidation_leaves_effect_when_its_lesson_is_rejected(tmp_path):
    """REGRESSION (memory): an approved model-written consolidation is live only while every lesson
    it supersedes is approved; rejecting one takes it out of effect, also after reload."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a = _approved_lesson(store)
    cons = store.append_consolidation(Consolidation(text="model summary", supersedes=(a.id,),
                                                    mode="model"))
    store.approve(cons.id)
    assert store.live_consolidations() == (cons,)
    store.reject(a.id)
    for s in (store, MemoryStore(log)):
        assert s.live_consolidations() == ()
        assert all(getattr(x, "text", "") != "model summary" for x in s.active())


@pytest.mark.parametrize("mode", ["model", "deterministic"])
def test_reject_accepts_a_consolidation_id(tmp_path, mode):
    """REGRESSION (memory): `reject()` accepts a consolidation id, returns True, and takes that
    consolidation out of effect (also after reload)."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a = _approved_lesson(store)
    cons = store.append_consolidation(Consolidation(
        text="model summary" if mode == "model" else "", supersedes=(a.id,), mode=mode))
    if mode == "model":
        store.approve(cons.id)
    assert store.live_consolidations() == (cons,)
    assert store.reject(cons.id) is True
    for s in (store, MemoryStore(log)):
        assert s.live_consolidations() == ()
        assert s.decision(cons.id) == "rejected"


def test_a_consolidation_returns_to_effect_when_its_lesson_is_approved_again(tmp_path):
    """PRESERVATION (memory): the latest decision wins, so re-approving the lesson restores a
    deterministic consolidation over it."""
    store = MemoryStore(tmp_path / "lessons.jsonl")
    a = _approved_lesson(store)
    cons = store.append_consolidation(Consolidation(text="", supersedes=(a.id,)))
    store.reject(a.id)
    assert store.live_consolidations() == ()
    store.approve(a.id)
    assert store.live_consolidations() == (cons,)


# =========================================================================== hosted key
def _models_with(api_key_env):
    data = yaml.safe_load(MODELS.read_text(encoding="utf-8"))
    for model in data["models"]:
        if model["model_id"] == "hosted-burst":
            model["api_key_env"] = api_key_env
    return data


@pytest.mark.parametrize("value", ["sk-live-0123456789abcdef", "my_key", "1KEY", "KEY NAME",
                                   "OMLI/KEY", 42])
def test_api_key_env_accepts_only_a_variable_name_and_never_echoes_the_value(value):
    """REGRESSION (hosted): the registry refuses an `api_key_env` that is not an
    environment-variable name (a pasted key, lower case, a leading digit, spaces, punctuation,
    a number), and the error names the field without repeating the value."""
    with pytest.raises(ValueError) as caught:
        parse_models(_models_with(value))
    assert str(value) not in str(caught.value)
    assert "api_key_env" in str(caught.value)


def test_an_entry_built_outside_the_registry_is_held_to_the_same_rule(monkeypatch):
    """REGRESSION (hosted): `resolve_api_key` refuses a non-name `api_key_env` on an entry built by
    hand, and neither of its errors repeats the field's value."""
    pasted = "sk-live-0123456789abcdef"
    entry = dataclasses.replace(_keyed(monkeypatch), api_key_env=pasted)
    with pytest.raises(ModelError) as caught:
        resolve_api_key(entry)
    assert pasted not in str(caught.value)
    unset = dataclasses.replace(entry, api_key_env="TEST_UNSET_KEY_NAME")
    monkeypatch.delenv("TEST_UNSET_KEY_NAME", raising=False)
    with pytest.raises(ModelError) as caught:
        resolve_api_key(unset)
    assert "TEST_UNSET_KEY_NAME" not in str(caught.value)


def test_a_variable_name_is_accepted_and_read(monkeypatch):
    """PRESERVATION (hosted): a proper variable name parses, and the key is read from it."""
    registry = parse_models(_models_with("HOSTED_MODEL_API_KEY"))
    entry = registry.get("hosted-burst")
    monkeypatch.setenv("HOSTED_MODEL_API_KEY", "value-from-env")
    assert resolve_api_key(entry) == "value-from-env"


def _strings(value):
    """Every string in decoded data, decoding any string that is itself JSON, all the way down."""
    if isinstance(value, str):
        yield value
        try:
            inner = json.loads(value)
        except (ValueError, RecursionError):
            return
        if not isinstance(inner, str) or inner != value:
            yield from _strings(inner)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _tool_call_response(arguments: str) -> bytes:
    return json.dumps({"model": "m", "choices": [{"message": {
        "content": "", "tool_calls": [{"function": {"name": "f", "arguments": arguments}}]}}]}).encode()


@pytest.mark.parametrize("arguments", [
    json.dumps({"k": KEY}).replace("+", "\\u002b"),                       # one char escaped
    json.dumps({"inner": json.dumps({"k": KEY}).replace("+", "\\u002B")}),  # nested JSON string
], ids=["one-escape", "nested"])
def test_the_key_is_not_brought_back_by_decoding_tool_call_arguments(monkeypatch, arguments):
    """REGRESSION (hosted): the key is scrubbed after every decoding step, so decoding tool-call
    arguments, or a JSON string nested inside them, never turns an escaped key back into text."""
    _assert_no_key_in_tool_calls(monkeypatch, arguments)


def test_a_fully_escaped_key_in_tool_call_arguments_stays_scrubbed(monkeypatch):
    """PRESERVATION (hosted): a key with every character escaped in tool-call arguments does not
    appear in any decoded argument string or in the response."""
    _assert_no_key_in_tool_calls(monkeypatch, json.dumps({"k": "".join(f"\\u{ord(c):04x}" for c in KEY)}))


def _assert_no_key_in_tool_calls(monkeypatch, arguments):
    response = OpenAICompatibleProvider(transport=lambda *a, **k: _tool_call_response(arguments)) \
        .invoke(_keyed(monkeypatch), _request())
    found = [s for call in response.tool_calls for s in _strings(dict(call.arguments))]
    assert found and all(KEY not in s for s in found)
    assert KEY not in repr(response)


@pytest.mark.parametrize("encode", [
    lambda k: "".join(f"%{ord(c):02x}" if i % 2 else f"%{ord(c):02X}" for i, c in enumerate(k)),
    lambda k: "".join(f"&#x{ord(c):x};" for c in k),
    lambda k: quote(quote(k, safe=""), safe=""),
], ids=["url-mixed-case", "html-entity", "url-double"])
def test_other_encodings_of_the_key_in_content_are_scrubbed(monkeypatch, encode):
    """REGRESSION (hosted): mixed-case percent encoding, HTML character references and double
    URL encoding of the key are scrubbed from the response."""
    def transport(*args, **kwargs):
        return json.dumps({"model": "m", "choices": [{"message": {"content": "k=" + encode(KEY)}}]}).encode()

    response = OpenAICompatibleProvider(transport=transport).invoke(_keyed(monkeypatch), _request())
    assert encode(KEY) not in response.text and "[redacted]" in response.text


def test_a_deeply_nested_response_is_a_model_error(monkeypatch):
    """REGRESSION (hosted): a response nested past the parser's recursion limit is a ModelError,
    not an uncaught RecursionError."""
    with pytest.raises(ModelError):
        OpenAICompatibleProvider(transport=lambda *a, **k: DEEP.encode()) \
            .invoke(_keyed(monkeypatch), _request())


def test_ordinary_content_passes_through_unchanged(monkeypatch):
    """PRESERVATION (hosted): text that does not hold the key is returned as the server sent it."""
    text = "hello, 100% fine & ok \\u0041"
    body = json.dumps({"model": "m", "choices": [{"message": {"content": text}}]}).encode()
    response = OpenAICompatibleProvider(transport=lambda *a, **k: body).invoke(_keyed(monkeypatch), _request())
    assert response.text == text


# =========================================================================== traffic guard
def _policy_with(threshold):
    data = yaml.safe_load(TRAFFIC.read_text(encoding="utf-8"))
    data["rules"][0]["when"] = {"cpu_percent_at_least": threshold}
    return data


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), float("-inf"), 10 ** 400,
                                       "80", True, None, [80]],
                         ids=["nan", "inf", "-inf", "huge-int", "string", "bool", "none", "list"])
def test_a_traffic_rule_threshold_must_be_a_finite_number(threshold):
    """REGRESSION (traffic): the parser refuses a non-finite or non-numeric rule threshold (NaN
    would make the rule never fire)."""
    with pytest.raises(ValueError, match="threshold"):
        parse_traffic_policy(_policy_with(threshold))


@pytest.mark.parametrize("rank", ["30", 30.0, True])
def test_a_priority_rank_must_be_an_integer(rank):
    """REGRESSION (traffic): a priority-class rank that is a string, a float or a boolean is refused
    rather than coerced."""
    data = yaml.safe_load(TRAFFIC.read_text(encoding="utf-8"))
    data["priority_classes"][0]["rank"] = rank
    with pytest.raises(ValueError, match="rank"):
        parse_traffic_policy(data)


def test_the_shipped_traffic_pack_parses():
    """PRESERVATION (traffic): the shipped pack loads, has rules, and every rule threshold is a
    float."""
    policy = parse_traffic_policy(yaml.safe_load(TRAFFIC.read_text(encoding="utf-8")))
    assert policy.rules and all(isinstance(v, float) for r in policy.rules for v in r.when.values())


# =========================================================================== registry claims
def test_the_example_model_registry_names_only_shipped_adapters():
    """REGRESSION (registry): every adapter the example model registry names is a key of the
    shipped default provider table."""
    registry = load_models(MODELS)
    assert {registry.get(i).adapter for i in registry.ids()} <= set(default_provider_table())


def _connections():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind,cap", [("slack", "max_sends_per_day"),
                                      ("git-repo", "max_sends_per_day"),
                                      ("smtp", "max_files_per_change"),
                                      ("http-browse", "max_sends_per_day")])
def test_a_cap_is_refused_on_a_kind_it_does_not_apply_to(kind, cap):
    """REGRESSION (registry): a numeric blast-radius cap set on a kind it does not apply to is
    refused at load."""
    data = _connections()
    entry = next(c for c in data["connections"] if c["target"]["kind"] == kind)
    entry["blast_radius"][cap] = 5
    with pytest.raises(ValueError, match="does not apply"):
        parse_connections(data)


@pytest.mark.parametrize("value", ["50", 50.5, True])
def test_a_cap_must_be_a_positive_integer(value):
    """REGRESSION (registry): a cap that is a string, a float or a boolean is refused, not
    coerced."""
    data = _connections()
    entry = next(c for c in data["connections"] if c["target"]["kind"] == "smtp")
    entry["blast_radius"]["max_sends_per_day"] = value
    with pytest.raises(ValueError, match="positive integers"):
        parse_connections(data)


@pytest.mark.parametrize("entry", [["a", "list"], "a string", 7], ids=["list", "str", "int"])
def test_a_connection_entry_that_is_not_a_mapping_is_a_value_error(entry):
    """REGRESSION (registry): a connection entry that is not a mapping is refused with the loader's
    own ValueError, not a TypeError from unpacking it."""
    data = _connections()
    data["connections"].append(entry)
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_connections(data)


def test_the_example_connection_registry_parses_with_its_caps():
    """PRESERVATION (registry): the shipped example loads; its smtp entry caps sends at 50 a day
    and its slack entry has no daily send cap."""
    caps = {c.target.kind: c.blast_radius for c in parse_connections(_connections()).connections}
    assert caps["smtp"].max_sends_per_day == 50 and caps["slack"].max_sends_per_day is None
