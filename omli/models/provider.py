"""Provider/adapter boundary — the stable seam that makes OMLi model-agnostic.

The core builds a normalized `ModelRequest` and gets back a normalized `ModelResponse`.
Each serving format has an adapter that translates the request and returns `served_model_id`
for drift detection. Adapters send what their format's code below shows and drop the rest: no
adapter sends `thinking`, and the hosted adapter also drops the output schema, top_p and stop.
The core depends on this interface + the post-call gate — never on a concrete model.

The Ollama and hosted adapters raise `ModelError` for every ``Exception`` from the transport, for
a body that is not one JSON object, and for every field they read whose type is wrong. The Ollama
adapter's field rules are in `_field`, `_token_count`, `_ollama_tool_call` and
`OllamaProvider._verify`; the hosted adapter's, which also accept the real-world shapes their
docstrings list, are in `_openai_response` and, for each tool call, `_openai_tool_call`.
The echo adapter makes no call. The caller (the Agent) decides the escape-hatch routing
(Primitive 2).
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from omli.models.errors import ModelError
from omli.models.ollama import Transport, _urllib_transport, call_transport
from omli.models.ollama import load_json_object as _load_json
from omli.models.registry import RESERVED_ENV_PREFIX, ModelEntry, is_api_key_env_name
from omli.models._redact import scrub_bytes, scrub_text, scrub_value

# ---- normalized request / response ----------------------------------------


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: Optional[str] = None


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class SamplingParams:
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Tuple[str, ...] = ()
    #: Context window the SERVER is told to allocate. Distinct from the model's
    #: declared capability: unset means it is not sent and the server's own default applies.
    num_ctx: Optional[int] = None
    #: How long the server keeps weights resident. Not a sampling option — it is a
    #: top-level field of the serving request (see _ollama_options' caller).
    keep_alive: Optional[str] = None


@dataclass(frozen=True)
class ModelRequest:
    messages: Tuple[Message, ...]
    tools: Tuple[ToolSchema, ...] = ()
    output_schema: Optional[Mapping[str, Any]] = None
    params: SamplingParams = field(default_factory=SamplingParams)
    thinking: bool = False


@dataclass(frozen=True)
class NormalizedToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    text: str
    served_model_id: str
    tool_calls: Tuple[NormalizedToolCall, ...] = ()
    finish_reason: str = "stop"  # stop | length | tool_calls | content_filter | error
    input_tokens: int = 0
    output_tokens: int = 0


# ---- adapter contract ------------------------------------------------------


class ModelProvider(ABC):
    """Adapter contract referenced by ModelEntry.adapter."""

    key: str = "provider"

    @abstractmethod
    def invoke(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        """Translate, call the endpoint, and normalize. Raise ModelError on transport failure."""
        raise NotImplementedError


ProviderTable = Mapping[str, "ModelProvider"]


def _last_user(request: ModelRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content
    return ""


def _serving_name(entry: ModelEntry) -> str:
    """What to send as the endpoint's `model` field: the serving tag, else the pin id."""
    return entry.serving_tag or entry.pin.ref


def _digest_matches(server_digest: str, pinned_ref: str) -> bool:
    """Compare digests by their hex part, tolerating an optional 'sha256:' prefix."""
    served = server_digest.split(":")[-1].lower()
    pinned = pinned_ref.split(":")[-1].lower()
    return bool(served) and served == pinned


# ---- adapters --------------------------------------------------------------


class EchoProvider(ModelProvider):
    """Offline, deterministic adapter: the provider-table counterpart of LocalEchoModel."""

    key = "echo"

    def invoke(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        text = f"[{entry.model_id}] " + " ".join(_last_user(request).split())[:200]
        return ModelResponse(
            text=text,
            served_model_id=entry.model_id,
            output_tokens=len(text.split()),
        )


class OllamaProvider(ModelProvider):
    """Local Ollama adapter over the native /api/chat endpoint (messages + tools).

    Calls the model by its `serving_tag` (what Ollama loads), while `pin.ref` is the
    digest that identifies it. With `verify_digest=True`, the tag's digest is checked
    against the pin via /api/tags before the call (catching a moved tag), and the
    confirmed digest — not the tag — is recorded as `served_model_id`.
    """

    key = "ollama"

    def __init__(
        self,
        timeout_s: float = 60.0,
        transport: Optional[Transport] = None,
        verify_digest: bool = False,
    ) -> None:
        self._timeout = timeout_s
        self._transport = transport or _urllib_transport
        self._verify_digest = verify_digest

    def invoke(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        verified_digest = self._verify(entry) if self._verify_digest else None

        url = f"{entry.endpoint.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": _serving_name(entry),  # the Ollama tag, not the digest
            "messages": [_as_ollama_message(m) for m in request.messages],
            "stream": False,
            "options": _ollama_options(request.params),
        }
        # keep_alive is a TOP-LEVEL field of the request, not a sampling option, so it is
        # sent beside `options`, never inside it.
        if request.params.keep_alive is not None:
            payload["keep_alive"] = request.params.keep_alive
        if request.tools:
            payload["tools"] = [_as_ollama_tool(t) for t in request.tools]
        if request.output_schema is not None:
            payload["format"] = request.output_schema  # Ollama JSON-schema enforcement

        body = json.dumps(payload).encode("utf-8")
        raw = call_transport(self._transport, url, body, self._timeout, entry.endpoint)
        data = _load_json(raw, url)

        message = _field(data, "message", dict, {}, url)
        text = _field(message, "content", str, "", url)
        tool_calls = tuple(_ollama_tool_call(tc, url)
                           for tc in _field(message, "tool_calls", list, [], url))
        done_reason = _field(data, "done_reason", str, "", url)
        finish = "tool_calls" if tool_calls else ("length" if done_reason == "length" else "stop")
        # Prefer the verified digest (drift-proof identity); else what the endpoint reported.
        served = verified_digest or _field(data, "model", str, _serving_name(entry), url)
        return ModelResponse(
            text=text,
            served_model_id=served,
            tool_calls=tool_calls,
            finish_reason=finish,
            input_tokens=_token_count(data, "prompt_eval_count", url),
            output_tokens=_token_count(data, "eval_count", url),
        )

    def _verify(self, entry: ModelEntry) -> str:
        """Confirm the serving_tag on the server resolves to the pinned digest.

        Every listed model is checked BEFORE any is matched: each must be an object whose
        ``name``, ``model`` and ``digest`` are strings where present (null counts as absent), so
        one wrongly typed entry anywhere in the list raises ModelError. Returns the pinned digest
        on success. Also raises ModelError if the entry has no serving_tag, if no listed model
        carries the tag, or if its digest differs (the model drifted out from under the pin).
        """
        url = f"{entry.endpoint.rstrip('/')}/api/tags"
        data = _load_json(call_transport(self._transport, url, None, self._timeout, entry.endpoint), url)
        listed = []
        for model in _field(data, "models", list, [], url):
            if not isinstance(model, dict):
                raise ModelError(f"unexpected response shape from {url}: a model is "
                                 f"{type(model).__name__}")
            listed.append((_field(model, "name", str, None, url), _field(model, "model", str, None, url),
                           _field(model, "digest", str, "", url)))
        for name, model_name, digest in listed:
            if entry.serving_tag and entry.serving_tag in (name, model_name):
                if _digest_matches(digest, entry.pin.ref):
                    return entry.pin.ref
                raise ModelError(
                    f"ollama tag {entry.serving_tag!r}: the served digest is not the pinned "
                    f"{entry.pin.ref!r} (model drifted)"
                )
        raise ModelError(f"ollama tag {entry.serving_tag!r} not found on server at {entry.endpoint}")


class OpenAICompatibleProvider(ModelProvider):
    """Hosted OpenAI-compatible adapter (/chat/completions). served_model_id is the drift signal."""

    key = "openai-compatible"

    def __init__(self, timeout_s: float = 60.0, transport: Optional[Transport] = None) -> None:
        self._timeout = timeout_s
        self._transport = transport or _urllib_transport

    def invoke(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        url = f"{entry.endpoint.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": _serving_name(entry),  # hosted: serving_tag or the dated pin.ref
            "messages": [_as_openai_message(m) for m in request.messages],
        }
        params = request.params
        if params.temperature is not None:
            payload["temperature"] = params.temperature
        if params.max_tokens is not None:
            payload["max_tokens"] = params.max_tokens
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": dict(t.parameters)}}
                for t in request.tools
            ]

        # API key: read ONLY from the env var the registry names; never from the registry
        # itself. Fail loud if the model declares a key env that isn't set.
        api_key = resolve_api_key(entry)
        body = json.dumps(payload).encode("utf-8")
        raw = _call_with_key(self._transport, url, body, self._timeout, entry.endpoint, api_key)
        data = _load_json(raw, url)
        secrets = (api_key,) if api_key else ()
        # Scrub after every decoding step: the raw bytes were scrubbed in _call_with_key, the
        # decoded JSON is scrubbed here, and each tool call's arguments are scrubbed again after
        # they are decoded, so no decode can turn an escaped key back into text.
        problem: Optional[str] = None
        try:
            return _openai_response(scrub_value(data, secrets), entry, url, secrets)
        except ModelError:
            raise  # a shape rule, already typed and naming no value
        except Exception as exc:  # noqa: BLE001 - e.g. RecursionError while scrubbing deep JSON
            problem = f"unexpected response shape from {url}: {type(exc).__name__}"
        raise ModelError(problem)  # outside the except block: no chained original


def _openai_response(data: dict, entry: ModelEntry, url: str,
                     secrets: Tuple[str, ...]) -> ModelResponse:
    """Normalize a /chat/completions answer, checking TYPES only.

    Accepted: extra fields anywhere; a missing or null ``usage`` (both token counts are then
    reported as 0, meaning unknown); a null or missing ``content`` (text ""); a missing, null or
    empty ``tool_calls``. Refused with ModelError: ``choices`` that is not a non-empty list; a
    first choice or ``message`` that is not an object; ``content`` that is neither a string nor
    null; ``tool_calls`` that is not a list; a tool call or its ``function`` that is not an
    object; a tool ``name`` that is not a string; ``arguments`` that is neither an object nor a
    string holding a JSON object (an empty or whitespace-only string is read as ``{}``, see
    `_openai_tool_call`); ``usage`` that is present but not an object; a token count that
    is present but not a non-negative ``int`` (``bool`` excluded); ``model`` or ``finish_reason``
    that is present but not a string. A null field counts as absent, except that ``choices``,
    the first choice, ``message``, a tool's ``function``, ``name`` and ``arguments`` must be
    present. Only the first choice is read.
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(f"unexpected response shape from {url}: choices is not a non-empty list")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ModelError(f"unexpected response shape from {url}: a choice is {type(choice).__name__}")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelError(f"unexpected response shape from {url}: message is {type(message).__name__}")
    usage = _field(data, "usage", dict, {}, url)
    return ModelResponse(
        text=_field(message, "content", str, "", url),
        served_model_id=_field(data, "model", str, entry.pin.ref, url),
        tool_calls=tuple(_openai_tool_call(tc, url, secrets)
                         for tc in _field(message, "tool_calls", list, [], url)),
        finish_reason=_field(choice, "finish_reason", str, "stop", url),
        input_tokens=_token_count(usage, "prompt_tokens", url),
        output_tokens=_token_count(usage, "completion_tokens", url),
    )


def default_provider_table(
    *,
    ollama_transport: Optional[Transport] = None,
    openai_transport: Optional[Transport] = None,
) -> dict[str, ModelProvider]:
    """The standard adapter table keyed by ModelEntry.adapter."""
    return {
        "echo": EchoProvider(),
        "ollama": OllamaProvider(transport=ollama_transport),
        "openai-compatible": OpenAICompatibleProvider(transport=openai_transport),
    }


# ---- helpers ---------------------------------------------------------------


def resolve_api_key(entry: ModelEntry) -> Optional[str]:
    """Read a hosted model's API key from the env var the registry names (fail loud).

    The registry accepts in `api_key_env` only a variable name that `is_api_key_env_name`
    accepts (outside OMLi's own prefix, not one of `RESERVED_ENV_NAMES`, and not ending in
    `RESERVED_ENV_SUFFIX` in any case); an entry built some
    other way is held to the same rule here, so nothing is read from a variable that rule
    refuses. Neither error repeats the field's value."""
    env = getattr(entry, "api_key_env", None)
    if env is None or env == "":
        return None
    if not is_api_key_env_name(env):
        raise ModelError(f"model {entry.model_id!r}: api_key_env is not an environment-variable "
                         f"name outside OMLi's own {RESERVED_ENV_PREFIX} prefix and not a "
                         "home-directory or proxy variable")
    key = os.environ.get(env)
    if not key:
        raise ModelError(
            f"model {entry.model_id!r} needs an API key in the environment variable its "
            "registry entry names, but it is not set"
        )
    return key


def _call_with_key(transport: Transport, url: str, body: Optional[bytes], timeout: float,
                   host: str, api_key: Optional[str]) -> bytes:
    """Run a transport with `Authorization: Bearer <key>` when there is a key.

    The key is scrubbed from the response bytes (see `omli.models._redact` for the encodings
    covered). A ModelError keeps its scrubbed text; any other ``Exception`` is reported by its
    type name only (``BaseException`` subclasses that are not ``Exception`` propagate). The
    replacement error is built AFTER the
    ``except`` block has finished, so it carries no ``__context__`` and no ``__cause__``: the
    original exception, whose text or arguments may hold the key, is not reachable from what
    the caller receives.
    """
    if not api_key:
        return call_transport(transport, url, body, timeout, host)
    failure: Optional[tuple[type, str]] = None
    raw = b""
    try:
        raw = call_transport(transport, url, body, timeout, host,
                             headers={"Authorization": f"Bearer {api_key}"})
    except Exception as exc:  # noqa: BLE001 - every failure is re-raised below, scrubbed
        if isinstance(exc, ModelError):
            failure = (type(exc), scrub_text(str(exc), (api_key,)))
        else:
            failure = (ModelError, f"transport failed: {type(exc).__name__}")
    if failure is not None:
        kind, text = failure
        raise kind(text)
    return scrub_bytes(raw, (api_key,))


def _field(data: dict, name: str, kind: type, default: Any, url: str) -> Any:
    """``data[name]`` when it is a ``kind``; ``default`` when it is absent or null. Any other
    value is a malformed answer and raises ModelError naming only the field and the type."""
    value = data.get(name)
    if value is None:
        return default
    if not isinstance(value, kind):
        raise ModelError(f"unexpected response shape from {url}: {name} is {type(value).__name__}")
    return value


def _token_count(data: dict, name: str, url: str) -> int:
    """A token count: a non-negative ``int`` (not ``bool``), or 0 when absent or null. Any
    other value raises ModelError."""
    value = data.get(name)
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise ModelError(f"unexpected response shape from {url}: {name} is not a token count")
    return value


def _ollama_tool_call(tc: Any, url: str) -> NormalizedToolCall:
    """One /api/chat tool call: ``{"function": {"name": str, "arguments": dict}}``. Any other
    shape raises ModelError."""
    if not isinstance(tc, dict):
        raise ModelError(f"unexpected response shape from {url}: a tool call is {type(tc).__name__}")
    function = _field(tc, "function", dict, {}, url)
    return NormalizedToolCall(name=_field(function, "name", str, "", url),
                              arguments=_field(function, "arguments", dict, {}, url))


_JSON_WHITESPACE = " \t\n\r"


def _openai_tool_call(tc: Any, url: str, secrets: Tuple[str, ...]) -> NormalizedToolCall:
    """One /chat/completions tool call: ``{"function": {"name": str, "arguments": ...}}`` where
    ``arguments`` is an object, a string holding a JSON object, or a string that is empty or
    holds only JSON whitespace (space, tab, line feed, carriage return), which is read as ``{}``.
    Any other shape raises ModelError. Decoded arguments are scrubbed of ``secrets``."""
    if not isinstance(tc, dict):
        raise ModelError(f"unexpected response shape from {url}: a tool call is {type(tc).__name__}")
    function = tc.get("function")
    if not isinstance(function, dict):
        raise ModelError(f"unexpected response shape from {url}: function is {type(function).__name__}")
    name = function.get("name")
    if not isinstance(name, str):
        raise ModelError(f"unexpected response shape from {url}: name is {type(name).__name__}")
    arguments = function.get("arguments")
    if isinstance(arguments, str) and not arguments.strip(_JSON_WHITESPACE):
        arguments = {}  # a call with no arguments, as some compatible servers send it
    elif isinstance(arguments, str):
        arguments = _load_json(arguments.encode("utf-8"), url)  # a JSON object, else ModelError
    elif not isinstance(arguments, dict):
        raise ModelError(f"unexpected response shape from {url}: arguments is "
                         f"{type(arguments).__name__}")
    return NormalizedToolCall(name=name, arguments=scrub_value(arguments, secrets))


def _as_ollama_message(m: Message) -> dict:
    out = {"role": m.role, "content": m.content}
    if m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out


def _as_openai_message(m: Message) -> dict:
    out = {"role": m.role, "content": m.content}
    if m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out


def _as_ollama_tool(t: ToolSchema) -> dict:
    return {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": dict(t.parameters)}}


def _ollama_options(params: SamplingParams) -> dict:
    options: dict[str, Any] = {}
    if params.temperature is not None:
        options["temperature"] = params.temperature
    if params.top_p is not None:
        options["top_p"] = params.top_p
    if params.max_tokens is not None:
        options["num_predict"] = params.max_tokens
    if params.stop:
        options["stop"] = list(params.stop)
    if params.num_ctx is not None:
        options["num_ctx"] = params.num_ctx
    return options
