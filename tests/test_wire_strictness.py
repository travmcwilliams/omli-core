"""Strict shapes at the wire and in the registries: anything other than the documented shape is
a reported failure or a load error, never a success.

Every test docstring starts with its kind:

* REGRESSION   — an input the code must refuse or report in a fixed way.
* PRESERVATION — behaviour that must stay as it is.

No test here opens a socket: the Slack wires run against a fake opener, and the Ollama adapter
against an injected transport.
"""

from __future__ import annotations

import http.client
import io
import json

import pytest
import yaml

import omli.connections.reader as reader_module
import omli.connections.sender as sender_module
from omli.agents.base import Agent
from omli.agents.reflector import MemoryStore
from omli.artifacts import ArtifactStore
from omli.artifacts.provenance import Provenance
from omli.artifacts.schema import new_artifact
from omli.connections import secrets as S
from omli.connections.schema import load_registry, parse_registry as parse_connections
from omli.connections.scope_gate import ConnectionAction
from omli.models.errors import ModelError
from omli.models.ollama import OllamaModel
from omli.models.provider import OllamaProvider, resolve_api_key
from omli.models.registry import load_registry as load_models
from omli.models.registry import parse_registry as parse_models
from omli.models._redact import MASK, scrub_text
from omli.packs.loader import load_pack
from test_fail_closed import EXAMPLE, MODELS, ROOT, _models_with, _read
from test_trust_boundaries import _keyed

SCQC_PACK = ROOT / "packs" / "scqc" / "pack.yaml"
TEST_MODELS = ROOT / "tests" / "data" / "registry.yaml"


# =========================================================================== slack wires
class _FakeOpener:
    """Stands in for SAFE_OPENER: answers every request with one fixed body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return io.BytesIO(self.body)


def _slack():
    return load_registry(EXAMPLE).get("team_slack")


def _post(monkeypatch, answer: dict):
    opener = _FakeOpener(json.dumps(answer).encode("utf-8"))
    monkeypatch.setattr(sender_module, "SAFE_OPENER", opener)
    action = ConnectionAction("team_slack", "post", path="#team-notes",
                              sensitivity_tags=("internal",))
    out = sender_module.dispatch(_slack(), action, {"text": "hello"},
                                 resolve=lambda *a, **k: "test-token")
    assert len(opener.requests) == 1
    return out


def _fetch(monkeypatch, answer):
    opener = _FakeOpener(json.dumps(answer).encode("utf-8"))
    monkeypatch.setattr(reader_module, "SAFE_OPENER", opener)
    out = reader_module.read(_slack(), "CEXAMPLE001", {}, sensitivity_tags=("internal",),
                             resolve=lambda *a, **k: "test-token")
    assert len(opener.requests) == 1
    return out


NOT_TRUE = ["false", "true", "yes", 1, [True], {"ok": True}]


@pytest.mark.parametrize("ok", NOT_TRUE, ids=lambda v: repr(v))
def test_the_slack_sender_succeeds_only_on_json_true(monkeypatch, ok):
    """REGRESSION (slack): the Slack sender wire reports success only when `ok` is the JSON
    boolean true; a string, a number or any other value is `wire-reported-failure`."""
    out = _post(monkeypatch, {"ok": ok, "ts": "1"})
    assert out.ok is False and out.reason == "wire-reported-failure"


@pytest.mark.parametrize("ok", NOT_TRUE, ids=lambda v: repr(v))
def test_the_slack_reader_succeeds_only_on_json_true(monkeypatch, ok):
    """REGRESSION (slack): the Slack reader wire reports success only when `ok` is the JSON
    boolean true; any other value is `wire-reported-failure` with no messages."""
    out = _fetch(monkeypatch, {"ok": ok, "messages": [{"ts": "1", "text": "hi"}]})
    assert out.ok is False and out.reason == "wire-reported-failure" and out.messages == ()


def test_slack_answering_json_true_is_still_success(monkeypatch):
    """PRESERVATION (slack): `"ok": true` from Slack is success on both wires, and the reader
    returns the messages unchanged."""
    assert _post(monkeypatch, {"ok": True, "ts": "1"}).ok is True
    msgs = [{"ts": "1", "text": "hi"}, {"ts": "2", "text": "there"}]
    out = _fetch(monkeypatch, {"ok": True, "messages": msgs})
    assert out.ok is True and out.reason == "" and out.messages == tuple(msgs)


# =========================================================================== reader messages
@pytest.mark.parametrize("answer", [
    {"ok": True},
    {"ok": True, "messages": None},
    {"ok": True, "messages": [1, 2]},
    {"ok": True, "messages": ["text"]},
    {"ok": True, "messages": [{"ts": "1"}, None]},
], ids=["missing", "null", "numbers", "strings", "one-null"])
def test_messages_that_are_not_a_list_of_mappings_are_a_failure(answer):
    """REGRESSION (reader): `messages` missing, null, or a list holding anything other than
    mappings is a reported failure with no messages, never a success."""
    out = _read(lambda *a: answer)
    assert out.ok is False and out.reason == "wire-returned-no-result" and out.messages == ()


@pytest.mark.parametrize("messages", [{"ts": "1", "text": "hi"}, "text", [1]],
                         ids=["dict", "string", "list-of-number"])
def test_the_default_slack_reader_refuses_a_malformed_messages_field(monkeypatch, messages):
    """REGRESSION (reader): through the real Slack wire, a `messages` field that is a dict, a
    string or a list of non-mappings is a reported failure, not its keys or characters."""
    out = _fetch(monkeypatch, {"ok": True, "messages": messages})
    assert out.ok is False and out.reason == "wire-returned-no-result" and out.messages == ()


def test_an_empty_message_list_is_success():
    """PRESERVATION (reader): an empty list of messages is a successful read with nothing in it."""
    out = _read(lambda *a: {"ok": True, "messages": []})
    assert out.ok is True and out.messages == ()


# =========================================================================== ollama
def _answer(body):
    """A transport that answers every call with `body` (bytes), or raises it (an exception)."""
    def transport(url, data, timeout, headers=None):
        if isinstance(body, BaseException):
            raise body
        return body
    return transport


MALFORMED_CHAT = {
    "message-string": b'{"message": "a string"}',
    "message-list": b'{"message": ["a"]}',
    "content-number": b'{"message": {"content": 5}}',
    "tool-calls-dict": b'{"message": {"content": "", "tool_calls": {"a": 1}}}',
    "tool-call-string": b'{"message": {"content": "", "tool_calls": ["call"]}}',
    "function-string": b'{"message": {"content": "", "tool_calls": [{"function": "f"}]}}',
    "arguments-string": b'{"message": {"content": "", "tool_calls": [{"function": {"name": "f", "arguments": "x"}}]}}',
    "tokens-text": b'{"message": {"content": "ok"}, "eval_count": "many"}',
    "tokens-numeric-text": b'{"message": {"content": "ok"}, "prompt_eval_count": "12"}',
    "tokens-list": b'{"message": {"content": "ok"}, "eval_count": [1]}',
    "tokens-float": b'{"message": {"content": "ok"}, "eval_count": 1.5}',
    "tokens-bool": b'{"message": {"content": "ok"}, "eval_count": true}',
    "tokens-negative": b'{"message": {"content": "ok"}, "eval_count": -1}',
    "model-number": b'{"model": 5, "message": {"content": "ok"}}',
    "done-reason-number": b'{"message": {"content": "ok"}, "done_reason": 5}',
}
TRANSPORT_ERRORS = {
    "incomplete-read": http.client.IncompleteRead(b"partial"),
    "bad-status-line": http.client.BadStatusLine("garbage"),
    "value-error": ValueError("unknown url type"),
}


def _local_entry():
    return load_models(TEST_MODELS).get("example-3b")


def _request():
    from omli.models.provider import Message, ModelRequest
    return ModelRequest(messages=(Message("user", "hello"),))


@pytest.mark.parametrize("body", list(MALFORMED_CHAT.values()) + list(TRANSPORT_ERRORS.values()),
                         ids=list(MALFORMED_CHAT) + list(TRANSPORT_ERRORS))
def test_the_ollama_adapter_raises_model_error_for_every_failure(body):
    """REGRESSION (ollama): a malformed /api/chat answer (a non-object message, non-string
    content, non-list tool calls, a malformed tool call, a token count that is not a
    non-negative integer) and every transport exception, including `http.client` errors,
    raise ModelError."""
    with pytest.raises(ModelError):
        OllamaProvider(transport=_answer(body)).invoke(_local_entry(), _request())


def _work(store):
    work = new_artifact(
        "WorkResult", {"text": "Shipped the intake path.", "project": "demo"},
        Provenance(agent="test", pack_name="seed", pack_version="0.0.0",
                   source_refs=("file:///code/demo",)))
    return store.write(work)


@pytest.mark.parametrize("body", [MALFORMED_CHAT["message-string"], MALFORMED_CHAT["tool-calls-dict"],
                                  MALFORMED_CHAT["tokens-text"], TRANSPORT_ERRORS["incomplete-read"],
                                  TRANSPORT_ERRORS["bad-status-line"]],
                         ids=["message-string", "tool-calls-dict", "tokens-text", "incomplete-read",
                              "bad-status-line"])
def test_agent_run_returns_none_and_writes_nothing_on_a_bad_ollama_answer(body):
    """REGRESSION (ollama): when the Ollama wire answers with a malformed shape or fails with an
    `http.client` error, `Agent.run` returns None and the store holds only the input."""
    store = ArtifactStore()
    work = _work(store)
    agent = Agent(load_pack(SCQC_PACK), load_models(TEST_MODELS),
                  {"ollama": OllamaProvider(transport=_answer(body))}, store)
    assert agent.run(work, "OutcomeRecord") is None
    assert len(store) == 1


@pytest.mark.parametrize("error", list(TRANSPORT_ERRORS.values()), ids=list(TRANSPORT_ERRORS))
def test_available_is_false_for_every_transport_exception(error):
    """REGRESSION (ollama): `OllamaModel.available()` returns False, not an exception, when the
    transport raises an `http.client` error or any other Exception."""
    assert OllamaModel(transport=_answer(error)).available() is False


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"3"], ids=["array", "string", "number"])
def test_generate_raises_model_error_for_a_non_object_answer(body):
    """REGRESSION (ollama): `OllamaModel.generate` raises ModelError when /api/generate answers
    with JSON that is not an object."""
    with pytest.raises(ModelError):
        OllamaModel(transport=_answer(body)).generate("hello")


@pytest.mark.parametrize("body", [b'{"response": 5, "note": "wire-text-123"}',
                                  b'{"note": "wire-text-123"}'], ids=["number", "missing"])
def test_a_generate_error_does_not_repeat_the_answer(body):
    """REGRESSION (ollama): when /api/generate answers without a string `response`, the
    ModelError names the problem and does not copy the answer's text."""
    with pytest.raises(ModelError) as caught:
        OllamaModel(transport=_answer(body)).generate("hello")
    assert "wire-text-123" not in str(caught.value)


def test_a_well_formed_ollama_answer_is_still_parsed():
    """PRESERVATION (ollama): a well-formed /api/chat answer gives its text, tool calls and token
    counts; absent or null optional fields take their defaults."""
    body = json.dumps({"model": "m", "message": {"content": "hi", "tool_calls": [
        {"function": {"name": "f", "arguments": {"a": 1}}}]},
        "prompt_eval_count": 3, "eval_count": 4}).encode("utf-8")
    out = OllamaProvider(transport=_answer(body)).invoke(_local_entry(), _request())
    assert out.text == "hi" and out.input_tokens == 3 and out.output_tokens == 4
    assert [(c.name, dict(c.arguments)) for c in out.tool_calls] == [("f", {"a": 1})]
    bare = OllamaProvider(transport=_answer(b'{"message": {"content": null, "tool_calls": null},'
                                            b' "eval_count": null}')).invoke(_local_entry(), _request())
    assert bare.text == "" and bare.tool_calls == () and bare.output_tokens == 0


def _routed(tags: dict):
    """A transport that answers /api/tags with `tags` and /api/chat with a short reply."""
    def transport(url, data, timeout, headers=None):
        if url.endswith("/api/tags"):
            return json.dumps(tags).encode("utf-8")
        return b'{"message": {"content": "ok"}}'
    return transport


def test_a_drift_error_does_not_repeat_the_served_digest():
    """REGRESSION (ollama): when the served digest differs from the pin, the ModelError says so
    without copying the digest the server sent."""
    entry = _local_entry()
    tags = {"models": [{"name": entry.serving_tag, "digest": "sha256:" + "e" * 64}]}
    with pytest.raises(ModelError, match="drifted") as caught:
        OllamaProvider(transport=_routed(tags), verify_digest=True).invoke(entry, _request())
    assert "e" * 64 not in str(caught.value)


def test_an_entry_without_a_serving_tag_never_verifies(monkeypatch):
    """REGRESSION (ollama): an entry built with no serving_tag does not match a listed model that
    has no name, so digest verification raises ModelError instead of passing."""
    import dataclasses
    entry = dataclasses.replace(_local_entry(), serving_tag=None)
    tags = {"models": [{"digest": entry.pin.ref}]}
    with pytest.raises(ModelError):
        OllamaProvider(transport=_routed(tags), verify_digest=True).invoke(entry, _request())


BAD_TAG_MODELS = {
    "name-number": {"name": 5, "digest": "sha256:" + "0" * 64},
    "model-list": {"model": ["x"], "digest": "sha256:" + "0" * 64},
    "digest-number": {"name": "other-model:1b", "digest": 5},
}


def _good_tag(entry):
    return {"name": entry.serving_tag, "model": entry.serving_tag, "digest": entry.pin.ref}


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("bad", list(BAD_TAG_MODELS), ids=list(BAD_TAG_MODELS))
def test_a_wrongly_typed_tags_entry_beside_the_match_is_a_model_error(bad, position):
    """REGRESSION (ollama): digest verification type-checks every model /api/tags lists before
    matching, so a model whose name, model or digest has the wrong type raises ModelError even
    when a well-formed entry for the pinned tag is listed before or after it."""
    entry = _local_entry()
    models = [_good_tag(entry), BAD_TAG_MODELS[bad]]
    if position == "before":
        models.reverse()
    with pytest.raises(ModelError):
        OllamaProvider(transport=_routed({"models": models}), verify_digest=True).invoke(entry, _request())


@pytest.mark.parametrize("bad", [5, "text", None, ["a"]], ids=["number", "string", "null", "list"])
def test_a_non_object_tags_entry_after_the_match_is_a_model_error(bad):
    """REGRESSION (ollama): a /api/tags entry that is not an object raises ModelError even when
    it is listed after the well-formed entry for the pinned tag."""
    entry = _local_entry()
    tags = {"models": [_good_tag(entry), bad]}
    with pytest.raises(ModelError):
        OllamaProvider(transport=_routed(tags), verify_digest=True).invoke(entry, _request())


def test_well_formed_tags_still_verify():
    """PRESERVATION (ollama): well-formed /api/tags entries, with other models and extra or null
    fields beside the pinned one, still verify and record the pinned digest."""
    entry = _local_entry()
    tags = {"models": [{"name": "other-model:1b", "model": None, "digest": "sha256:" + "0" * 64,
                        "size": 1, "details": {"family": "x"}},
                       _good_tag(entry), {"name": "third:2b"}]}
    out = OllamaProvider(transport=_routed(tags), verify_digest=True).invoke(entry, _request())
    assert out.served_model_id == entry.pin.ref


DEEP = b'{"a":' * 100_000 + b"1" + b"}" * 100_000


def _generate_entry():
    import types
    return types.SimpleNamespace(serving_tag="example-model:8b", endpoint="http://localhost:11434")


def _complete(body):
    from omli.models.ollama import generate_completion
    return generate_completion(_generate_entry(), "hello", transport=_answer(body))


@pytest.mark.parametrize("call", ["generate", "generate_completion"])
def test_a_deeply_nested_generate_answer_is_a_model_error(call):
    """REGRESSION (ollama): an /api/generate answer nested past the parser's recursion limit
    raises ModelError from both entry points, not an untyped RecursionError."""
    with pytest.raises(ModelError):
        if call == "generate":
            OllamaModel(transport=_answer(DEEP)).generate("hello")
        else:
            _complete(DEEP)


@pytest.mark.parametrize("body", [b'{"response": 5}', b'{"response": ["a"]}', b'{"response": {"a": 1}}',
                                  b'{"response": true}', b'{"response": null}', b'{"done": true}'],
                         ids=["number", "list", "object", "bool", "null", "missing"])
def test_generate_completion_refuses_a_response_that_is_not_text(body):
    """REGRESSION (ollama): `generate_completion` raises ModelError when `response` is not a
    string (missing and null included), instead of converting it to text."""
    with pytest.raises(ModelError):
        _complete(body)


def test_a_text_generate_answer_is_still_returned():
    """PRESERVATION (ollama): a string `response` is returned as sent by both entry points, and
    `generate_completion` also returns the decoded answer."""
    body = b'{"response": "hi there", "done": true}'
    assert OllamaModel(transport=_answer(body)).generate("hello") == "hi there"
    text, raw = _complete(body)
    assert text == "hi there" and raw == {"response": "hi there", "done": True}


@pytest.mark.parametrize("body", [b"{}", b"[]", b"not json", b'"text"', b'{"models": {}}',
                                  b'{"models": null}', b'{"models": "x"}', DEEP, b"\xff\xfe"],
                         ids=["empty-object", "array", "not-json", "string", "models-object",
                              "models-null", "models-string", "deep", "not-utf8"])
def test_available_is_true_only_for_a_models_list(body):
    """REGRESSION (ollama): `available()` is False unless /api/tags answers with a JSON object
    whose `models` is a list."""
    assert OllamaModel(transport=_answer(body)).available() is False


@pytest.mark.parametrize("body", [b'{"models": []}', b'{"models": [{"name": "m"}], "extra": 1}'],
                         ids=["empty-list", "one-model"])
def test_available_is_true_for_a_models_list(body):
    """PRESERVATION (ollama): a JSON object whose `models` is a list, empty or not, is
    available."""
    assert OllamaModel(transport=_answer(body)).available() is True


# =========================================================================== hosted adapter
def _hosted(monkeypatch, answer):
    from omli.models.provider import OpenAICompatibleProvider
    body = answer if isinstance(answer, bytes) else json.dumps(answer).encode("utf-8")
    return OpenAICompatibleProvider(transport=lambda *a, **k: body).invoke(_keyed(monkeypatch),
                                                                            _request())


def _choice(message, **extra):
    return {"model": "m", "choices": [dict({"message": message}, **extra)]}


def _call(function):
    return _choice({"content": None, "tool_calls": [{"type": "function", "function": function}]})


MALFORMED_HOSTED = {
    "choices-missing": {"model": "m"},
    "choices-null": {"choices": None},
    "choices-empty": {"choices": []},
    "choices-object": {"choices": {}},
    "message-missing": {"choices": [{"finish_reason": "stop"}]},
    "message-null": _choice(None),
    "message-list": _choice([]),
    "content-number": _choice({"content": 5}),
    "content-zero": _choice({"content": 0}),
    "content-list": _choice({"content": ["a"]}),
    "content-object": _choice({"content": {"a": 1}}),
    "content-bool": _choice({"content": True}),
    "tool-calls-object": _choice({"content": "", "tool_calls": {}}),
    "tool-calls-zero": _choice({"content": "", "tool_calls": 0}),
    "tool-calls-text": _choice({"content": "", "tool_calls": ""}),
    "function-missing": _choice({"content": None, "tool_calls": [{"type": "function"}]}),
    "name-missing": _call({"arguments": "{}"}),
    "name-null": _call({"name": None, "arguments": "{}"}),
    "name-number": _call({"name": 5, "arguments": "{}"}),
    "arguments-missing": _call({"name": "f"}),
    "arguments-null": _call({"name": "f", "arguments": None}),
    "arguments-not-json": _call({"name": "f", "arguments": "not json"}),
    "arguments-json-list": _call({"name": "f", "arguments": "[1, 2]"}),
    "arguments-json-number": _call({"name": "f", "arguments": "5"}),
    "arguments-number": _call({"name": "f", "arguments": 5}),
    "arguments-list": _call({"name": "f", "arguments": [1]}),
    "usage-list": dict(_choice({"content": "ok"}), usage=[]),
    "tokens-numeric-text": dict(_choice({"content": "ok"}), usage={"prompt_tokens": "12"}),
    "tokens-float": dict(_choice({"content": "ok"}), usage={"completion_tokens": 1.5}),
    "tokens-bool": dict(_choice({"content": "ok"}), usage={"completion_tokens": True}),
    "tokens-negative": dict(_choice({"content": "ok"}), usage={"prompt_tokens": -1}),
    "model-number": dict(_choice({"content": "ok"}), model=5),
    "model-list": dict(_choice({"content": "ok"}), model=["m"]),
    "finish-reason-number": _choice({"content": "ok"}, finish_reason=5),
}


@pytest.mark.parametrize("answer", list(MALFORMED_HOSTED.values()), ids=list(MALFORMED_HOSTED))
def test_the_hosted_adapter_refuses_a_wrongly_typed_answer(monkeypatch, answer):
    """REGRESSION (hosted): a /chat/completions answer whose `choices` is not a non-empty list,
    whose `message` is not an object, whose `content` is neither text nor null, whose tool call
    has a non-text `name` or `arguments` that are neither an object nor a JSON-object string, or
    whose token counts, `model` or `finish_reason` have the wrong type, raises ModelError."""
    with pytest.raises(ModelError):
        _hosted(monkeypatch, answer)


def test_a_null_model_takes_the_pinned_id(monkeypatch):
    """REGRESSION (hosted): a null `model` counts as absent, so the pinned id is recorded rather
    than the text of the null."""
    entry = _keyed(monkeypatch)
    out = _hosted(monkeypatch, dict(_choice({"content": "ok"}), model=None))
    assert out.served_model_id == entry.pin.ref


def test_hosted_tool_calls_with_null_content_are_accepted(monkeypatch):
    """PRESERVATION (hosted): null `content` beside tool calls gives empty text and the calls,
    with `arguments` as a JSON-object string or as an object."""
    out = _hosted(monkeypatch, _choice({"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}},
        {"id": "c2", "type": "function", "function": {"name": "g", "arguments": {"b": 2}}}]},
        finish_reason="tool_calls"))
    assert out.text == "" and out.finish_reason == "tool_calls"
    assert [(c.name, dict(c.arguments)) for c in out.tool_calls] == [("f", {"a": 1}), ("g", {"b": 2})]


@pytest.mark.parametrize("answer", [_choice({"content": "ok"}),
                                    dict(_choice({"content": "ok"}), usage=None)],
                         ids=["usage-missing", "usage-null"])
def test_a_hosted_answer_without_usage_is_accepted(monkeypatch, answer):
    """PRESERVATION (hosted): a missing or null `usage` is accepted, and both token counts are
    reported as 0 (unknown)."""
    out = _hosted(monkeypatch, answer)
    assert out.text == "ok" and out.input_tokens == 0 and out.output_tokens == 0


def test_a_hosted_answer_with_extra_fields_is_accepted(monkeypatch):
    """PRESERVATION (hosted): fields the adapter does not read are ignored at every level."""
    answer = {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
              "system_fingerprint": None, "extra": {"a": [1]},
              "choices": [{"index": 0, "logprobs": None, "finish_reason": "stop", "extra": 1,
                           "message": {"role": "assistant", "content": "ok", "refusal": None,
                                       "extra": True}}],
              "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12,
                        "prompt_tokens_details": {"cached_tokens": 0}}}
    out = _hosted(monkeypatch, answer)
    assert (out.text, out.served_model_id, out.finish_reason) == ("ok", "m", "stop")
    assert out.input_tokens == 9 and out.output_tokens == 3


def _call_with_arguments(monkeypatch, arguments):
    return _hosted(monkeypatch, _call({"name": "f", "arguments": arguments}))


@pytest.mark.parametrize("arguments", ["", " ", "\t\r\n "], ids=["empty", "space", "json-whitespace"])
def test_empty_hosted_arguments_are_read_as_an_empty_object(monkeypatch, arguments):
    """REGRESSION (hosted): a tool call whose `arguments` string is empty or holds only JSON
    whitespace is a call with no arguments, read as {}, not a refused answer."""
    out = _call_with_arguments(monkeypatch, arguments)
    assert [(c.name, dict(c.arguments)) for c in out.tool_calls] == [("f", {})]


def test_an_empty_json_object_string_is_still_read(monkeypatch):
    """PRESERVATION (hosted): an `arguments` string holding "{}" is still read as {}."""
    out = _call_with_arguments(monkeypatch, "{}")
    assert [(c.name, dict(c.arguments)) for c in out.tool_calls] == [("f", {})]


@pytest.mark.parametrize("arguments", [" x ", "[]", " [] ", "null", chr(0xA0), "{} {}"],
                         ids=["text", "list", "spaced-list", "null-text", "nbsp", "two-objects"])
def test_other_non_object_argument_strings_are_still_refused(monkeypatch, arguments):
    """PRESERVATION (hosted): any other `arguments` string that is not a JSON object, a no-break
    space (not JSON whitespace) among them, is still refused."""
    with pytest.raises(ModelError):
        _call_with_arguments(monkeypatch, arguments)


@pytest.mark.parametrize("tool_calls", [[], None], ids=["empty", "null"])
def test_an_empty_hosted_tool_call_list_is_accepted(monkeypatch, tool_calls):
    """PRESERVATION (hosted): an empty or null `tool_calls` gives no tool calls."""
    out = _hosted(monkeypatch, _choice({"content": "ok", "tool_calls": tool_calls}))
    assert out.text == "ok" and out.tool_calls == ()


# =========================================================================== registry booleans
def _connections_with_selected(value):
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    resource = next(r for c in data["connections"] for r in c["selection"]["resources"]
                    if r["selected"] is True)
    resource["selected"] = value
    return data


@pytest.mark.parametrize("value", ["false", "true", "yes", 1], ids=repr)
def test_selected_must_be_a_yaml_boolean(value):
    """REGRESSION (registry): a resource's `selected` that is a string or a number (a quoted
    "false" among them) is refused at load."""
    with pytest.raises(ValueError, match="selected"):
        parse_connections(_connections_with_selected(value))


def _models_with_reproducible(model_id, value):
    data = yaml.safe_load(TEST_MODELS.read_text(encoding="utf-8"))
    for model in data["models"]:
        if model["model_id"] == model_id:
            model["reproducible"] = value
    return data


@pytest.mark.parametrize("model_id,value", [("example-3b", "true"), ("example-3b", 1),
                                            ("example-3b", "false")], ids=["text-true", "one", "text-false"])
def test_reproducible_must_be_a_yaml_boolean(model_id, value):
    """REGRESSION (registry): a model's `reproducible` that is a string or a number is refused
    at load, even where the string's truth value would pass the pin rules."""
    with pytest.raises(ValueError, match="reproducible"):
        parse_models(_models_with_reproducible(model_id, value))


def test_the_shipped_registries_use_yaml_booleans():
    """PRESERVATION (registry): the shipped connection and model registries, and the test model
    registry, still load."""
    assert load_registry(EXAMPLE).get("team_slack").target.kind == "slack"
    parse_models(yaml.safe_load(MODELS.read_text(encoding="utf-8")))
    load_models(TEST_MODELS)


# =========================================================================== api_key_env
HOME_VARIABLES = ["HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"]


@pytest.mark.parametrize("name", HOME_VARIABLES)
def test_api_key_env_refuses_the_home_variables(name):
    """REGRESSION (hosted): `api_key_env` may not name a home-directory variable that
    `expanduser` reads, and the error does not repeat the name."""
    with pytest.raises(ValueError) as caught:
        parse_models(_models_with(name))
    assert name not in str(caught.value) and "api_key_env" in str(caught.value)


@pytest.mark.parametrize("name", HOME_VARIABLES)
def test_resolve_refuses_the_home_variables_on_a_hand_built_entry(monkeypatch, name):
    """REGRESSION (hosted): an entry built outside the registry cannot send a home-directory
    variable's value as its API key."""
    import dataclasses
    monkeypatch.setenv(name, "home-value")
    entry = dataclasses.replace(_keyed(monkeypatch), api_key_env=name)
    with pytest.raises(ModelError) as caught:
        resolve_api_key(entry)
    assert "home-value" not in str(caught.value) and name not in str(caught.value)


@pytest.mark.parametrize("name", ["HOMER", "MY_HOME", "HOME_KEY"])
def test_names_that_only_contain_home_are_accepted(name):
    """PRESERVATION (hosted): a variable whose name merely contains HOME is still accepted."""
    parse_models(_models_with(name))


PROXY_VARIABLES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]


@pytest.mark.parametrize("name", PROXY_VARIABLES)
def test_api_key_env_refuses_the_proxy_variables(name):
    """REGRESSION (hosted): `api_key_env` may not name a proxy variable the shared opener
    reads, and the error does not repeat the name."""
    with pytest.raises(ValueError) as caught:
        parse_models(_models_with(name))
    assert name not in str(caught.value) and "api_key_env" in str(caught.value)


@pytest.mark.parametrize("name", PROXY_VARIABLES)
def test_resolve_refuses_the_proxy_variables_on_a_hand_built_entry(monkeypatch, name):
    """REGRESSION (hosted): an entry built outside the registry cannot send a proxy variable's
    value as its API key."""
    import dataclasses
    monkeypatch.setenv(name, "http://proxy.invalid:3128/?token=proxy-pass")
    entry = dataclasses.replace(_keyed(monkeypatch), api_key_env=name)
    with pytest.raises(ModelError) as caught:
        resolve_api_key(entry)
    assert "proxy-pass" not in str(caught.value) and name not in str(caught.value)


OTHER_PROXY_VARIABLES = ["FTP_PROXY", "SOCKS_PROXY", "GOPHER_PROXY", "MY_CORP_PROXY", "_PROXY"]


@pytest.mark.parametrize("name", OTHER_PROXY_VARIABLES)
def test_api_key_env_refuses_every_name_ending_in_proxy(name):
    """REGRESSION (hosted): `api_key_env` may not name any variable ending in `_PROXY`, since
    urllib reads every such variable when the shared opener is built; the error does not
    repeat the name."""
    with pytest.raises(ValueError) as caught:
        parse_models(_models_with(name))
    assert name not in str(caught.value) and "api_key_env" in str(caught.value)


@pytest.mark.parametrize("name", OTHER_PROXY_VARIABLES)
def test_resolve_refuses_every_name_ending_in_proxy(monkeypatch, name):
    """REGRESSION (hosted): an entry built outside the registry cannot send the value of any
    variable ending in `_PROXY` as its API key."""
    import dataclasses
    monkeypatch.setenv(name, "http://proxy.invalid:3128/?token=proxy-pass")
    entry = dataclasses.replace(_keyed(monkeypatch), api_key_env=name)
    with pytest.raises(ModelError) as caught:
        resolve_api_key(entry)
    assert "proxy-pass" not in str(caught.value) and name not in str(caught.value)


@pytest.mark.parametrize("name", [v.lower() for v in PROXY_VARIABLES] + ["ftp_proxy", "Ftp_Proxy"])
def test_lower_case_proxy_names_are_refused(name):
    """PRESERVATION (hosted): a proxy variable in lower or mixed case is refused as
    `api_key_env`; the variable-name rule (capitals only) already refused it."""
    with pytest.raises(ValueError, match="api_key_env"):
        parse_models(_models_with(name))


@pytest.mark.parametrize("name", ["MY_PROXY_KEY", "PROXY", "HTTP_PROXY_TOKEN"])
def test_names_that_only_contain_proxy_are_accepted(name):
    """PRESERVATION (hosted): a variable whose name merely contains PROXY is still accepted."""
    parse_models(_models_with(name))


# =========================================================================== memory
@pytest.mark.parametrize("record", [
    {"type": "lesson", "id": "a", "text": 5},
    {"type": "lesson", "id": "a", "text": "t", "source_artifact_ids": 5},
    {"type": "lesson", "id": "a", "text": "t", "tags": "one"},
    {"type": "lesson", "id": ["a"], "text": "t"},
    {"type": "lesson", "id": "a", "text": "t", "approved": "yes"},
    {"type": "consolidation", "id": "c", "text": "t", "supersedes": "a"},
    {"type": "approval", "lesson_id": {"a": 1}, "text_sha256": "x"},
    {"type": "revocation", "consolidation_id": {"c": 1}},
    {"type": ["lesson"], "id": "a", "text": "t"},
], ids=["text-number", "sources-number", "tags-string", "id-list", "approved-string",
        "supersedes-string", "lesson-id-dict", "revocation-dict", "type-list"])
def test_a_wrongly_typed_field_is_a_value_error_naming_its_line(tmp_path, record):
    """REGRESSION (memory): a lessons-log line that is a JSON object with a field of the wrong
    type raises ValueError naming that line's number."""
    log = tmp_path / "lessons.jsonl"
    log.write_text(json.dumps({"type": "lesson", "id": "ok", "text": "fine"}) + "\n"
                   + json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"line 2\b"):
        MemoryStore(log)


def test_invalid_utf8_is_a_value_error_naming_its_line(tmp_path):
    """REGRESSION (memory): a lessons-log line that is not valid UTF-8 raises ValueError naming
    that line's number."""
    log = tmp_path / "lessons.jsonl"
    log.write_bytes(json.dumps({"type": "lesson", "id": "ok", "text": "fine"}).encode("utf-8")
                    + b"\n" + b'{"text": "\xff\xfe"}\n')
    with pytest.raises(ValueError, match=r"line 2\b"):
        MemoryStore(log)


def test_every_record_type_with_correct_types_still_loads(tmp_path):
    """PRESERVATION (memory): a log holding every record type (lesson, approval, rejection,
    consolidation, revocation), with every field of its documented type, loads in full."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    from omli.agents.reflector import Consolidation, Lesson
    a = store.append(Lesson(text="one", source_artifact_ids=("src",), tags=("t",),
                            project="p", sprint="s", category="c"))
    b = store.append(Lesson(text="two", source_artifact_ids=()))
    c = store.append(Lesson(text="three", source_artifact_ids=()))
    store.approve(a.id)
    store.approve(c.id)
    store.approve(b.id)
    store.reject(b.id)
    cons = store.append_consolidation(Consolidation(text="", supersedes=(a.id, c.id), actor="h",
                                                    project="p", category="c", tags=("t",)))
    store.revoke(cons.id, reason="test")
    written = [json.loads(line)["type"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert set(written) == {"lesson", "approval", "rejection", "consolidation", "revocation"}
    reloaded = MemoryStore(log)
    assert [l.text for l in reloaded.all()] == ["one", "two", "three"]
    assert reloaded.get(a.id).approved and not reloaded.get(b.id).approved
    assert [x.id for x in reloaded.consolidations()] == [cons.id]
    assert reloaded.is_revoked(cons.id) and reloaded.live_consolidations() == ()


# =========================================================================== secrets
@pytest.mark.parametrize("left,right", [
    ("secret://a-b/k", "secret://a_b/k"),
    ("secret://a_b/c", "secret://a/b_c"),
    ("secret://Key/k", "secret://key/k"),
    ("secret://a.b/k", "secret://a_b/k"),
    ("secret://a__b/k", "secret://a/b/k"),
])
def test_distinct_secret_refs_never_share_a_variable(left, right):
    """REGRESSION (secrets): two distinct references never map to the same environment
    variable; a reference whose mapping could collide is refused instead."""
    names = []
    for ref in (left, right):
        try:
            names.append(S.env_var_for(ref))
        except ValueError:
            names.append(None)
    assert None in names or names[0] != names[1]


@pytest.mark.parametrize("ref", ["secret://connections/example_api/api_key", "secret://a1/b2_c3/d"])
def test_a_valid_ref_maps_to_a_variable_that_names_it_back(ref):
    """REGRESSION (secrets): the variable name of a valid reference spells the reference back
    exactly (``__`` for each ``/``), so the mapping is one-to-one."""
    name = S.env_var_for(ref)
    assert name.startswith("OMLI_SECRET_")
    assert "secret://" + name[len("OMLI_SECRET_"):].lower().replace("__", "/") == ref


def test_the_shipped_secret_refs_are_still_valid():
    """PRESERVATION (secrets): every credential reference in the shipped connection registry
    still passes validation, and a one-segment reference keeps its variable name."""
    assert S.env_var_for("secret://link") == "OMLI_SECRET_LINK"
    refs = [c.target.credential_ref for c in load_registry(EXAMPLE).connections
            if c.target.credential_ref]
    assert refs
    for ref in refs:
        S.validate_ref(ref)


# =========================================================================== redactor
KEY = "AKEY-123"


@pytest.mark.parametrize("text", [r"\u41KEY-123", r"\u041KEY-123"],
                         ids=["two-digit-u", "three-digit-u"])
def test_a_short_escape_is_not_redacted(text):
    """REGRESSION (redactor): a ``\\u`` escape with fewer than its four hex digits is not read as
    the key, so the text is left as it was."""
    assert scrub_text(text, (KEY,)) == text


BACKSLASH = chr(92)


@pytest.mark.parametrize("text", ["a" + BACKSLASH + "u1234b", r"see \uABCD and \u0041 here",
                                  "100% done, 50%25 off"], ids=["u-escape", "two-u-escapes", "percent"])
def test_ordinary_escape_like_text_is_untouched(text):
    """PRESERVATION (redactor): ordinary text holding ``\\u`` or ``%`` sequences that do not
    spell the key is returned unchanged."""
    assert text.isascii()
    assert scrub_text(text, (KEY,)) == text


@pytest.mark.parametrize("text", [r"\u0041KEY-123", r"\\u0041KEY-123", "%41KEY-123",
                                  "%2541KEY-123", "&#x41;KEY-123", "&#x0041;KEY-123", "&#65;KEY-123"],
                         ids=["u4", "u4-double-backslash", "percent", "percent-double",
                              "html-hex", "html-hex-zeros", "html-decimal"])
def test_full_width_escapes_of_the_key_are_still_redacted(text):
    """PRESERVATION (redactor): the key with a character written as a full-width ``\\u``
    escape, a percent escape or an HTML reference is still masked."""
    assert scrub_text(text, (KEY,)) == MASK


# =========================================================================== store
def _root(text="x"):
    return new_artifact("CanonicalInput", {"q": text},
                        Provenance(agent="test", pack_name="seed", pack_version="0.0.0"))


def _lines(log):
    return log.read_text(encoding="utf-8").splitlines()


def test_a_second_store_on_a_written_log_cannot_append_a_duplicate(tmp_path):
    """REGRESSION (store): `ArtifactStore(path)` on a log that already holds records raises
    ValueError naming `from_log`, so a second store cannot append an id the file already holds."""
    log = tmp_path / "artifacts.jsonl"
    first = _root()
    ArtifactStore(log).write(first)
    with pytest.raises(ValueError, match="from_log"):
        ArtifactStore(log).write(first)
    assert len(_lines(log)) == 1


def test_from_log_then_a_new_id_still_appends(tmp_path):
    """PRESERVATION (store): a log opened with `from_log` loads its ids, appends a new one, and
    still refuses an id it already holds."""
    log = tmp_path / "artifacts.jsonl"
    first = _root("one")
    ArtifactStore(log).write(first)
    reopened = ArtifactStore.from_log(log)
    reopened.write(_root("two"))
    assert len(_lines(log)) == 2 and len(reopened) == 2
    with pytest.raises(ValueError):
        reopened.write(first)
    assert len(_lines(log)) == 2


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_a_store_on_a_missing_or_empty_log_still_writes(tmp_path, state):
    """PRESERVATION (store): `ArtifactStore(path)` on a missing or empty log is accepted and
    appends."""
    log = tmp_path / "var" / "artifacts.jsonl"
    if state == "empty":
        log.parent.mkdir(parents=True)
        log.write_bytes(b"")
    ArtifactStore(log).write(_root())
    assert len(_lines(log)) == 1
