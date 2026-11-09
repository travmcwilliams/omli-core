"""Ollama adapter — a real local model behind the generic ModelClient contract.

Talks to a local Ollama server (default http://localhost:11434) over stdlib urllib, so
it adds no runtime dependency. The HTTP transport is injectable so tests run fully
offline. Every ``Exception`` a transport raises, and every malformed answer, becomes one of
the typed errors in `errors.py` so the Agent can route it deterministically (Primitive 2).
``BaseException`` subclasses that are not ``Exception`` propagate.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from typing import Callable, Mapping, Optional

from omli.models.base import ModelClient
from omli.models.errors import ModelError, ModelTimeout, ModelUnavailable
from omli.net import SAFE_OPENER

# A transport takes (url, body-or-None, timeout) and returns the raw response bytes.
# body=None means an HTTP GET; bytes means a POST with a JSON content type. A transport used for
# a model with an API key must also accept a `headers` keyword: that is how the key reaches it.
Transport = Callable[..., bytes]

# The process-wide opener. It refuses cross-origin redirects, so an `Authorization` header
# (the hosted adapter's API key) never follows a redirect to another host. See omli.net.
_OPENER = SAFE_OPENER


def _urllib_transport(url: str, body: Optional[bytes], timeout: float,
                      headers: Optional[Mapping[str, str]] = None) -> bytes:
    method = "POST" if body is not None else "GET"
    sent = {"Content-Type": "application/json"} if body is not None else {}
    sent.update(headers or {})
    request = urllib.request.Request(url, data=body, headers=sent, method=method)
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read()


def build_generate_payload(serving_tag: str, prompt: str, *,
                           num_ctx: int = 16384, temperature: float = 0.2) -> dict:
    """The /api/generate payload that `generate_completion` sends.
    `OllamaModel` builds its own request."""
    return {
        "model": serving_tag,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": temperature},
    }


def call_transport(
    transport: Transport,
    url: str,
    body: Optional[bytes],
    timeout: float,
    host: str,
    headers: Optional[Mapping[str, str]] = None,
) -> bytes:
    """Run a transport, turning a timeout into ModelTimeout and a URL, OS or ``http.client``
    error into ModelUnavailable. A ModelError passes through as raised. Any other ``Exception``
    becomes ModelUnavailable naming only its type, so no transport failure escapes untyped.

    Shared by the model client and the providers so the failure-routing semantics
    (Primitive 2) are defined in exactly one place.
    """
    try:
        if headers:
            return transport(url, body, timeout, headers=headers)
        return transport(url, body, timeout)
    except ModelError:
        raise  # already typed (e.g. an injected test transport)
    except (TimeoutError, socket.timeout) as exc:  # TimeoutError before OSError
        raise ModelTimeout(f"timed out after {timeout}s calling {host}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ModelTimeout(f"timed out after {timeout}s calling {host}") from exc
        raise ModelUnavailable(f"unreachable at {host}: {exc}") from exc
    except (ConnectionError, OSError) as exc:
        raise ModelUnavailable(f"unreachable at {host}: {exc}") from exc
    except http.client.HTTPException as exc:  # IncompleteRead, BadStatusLine, ...
        raise ModelUnavailable(f"unreachable at {host}: {type(exc).__name__}") from None
    except Exception as exc:  # noqa: BLE001 - every other transport failure, typed by name
        raise ModelUnavailable(f"transport failed at {host}: {type(exc).__name__}") from None


def load_json_object(raw: bytes, url: str) -> dict:
    """Decode a response body that must be one JSON object. Every ``Exception`` from decoding
    (not UTF-8, not JSON, nested too deeply, ...) and every non-object value raises ModelError
    naming only the URL and a type, never the body."""
    problem: Optional[str] = None
    data: object = None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - typed by name below
        problem = f"invalid JSON from {url}: {type(exc).__name__}"
    if problem is not None:
        raise ModelError(problem)  # outside the except block: no chained original
    if not isinstance(data, dict):
        raise ModelError(f"unexpected response shape from {url}: {type(data).__name__}")
    return data


def _response_text(data: dict, url: str) -> str:
    """The /api/generate ``response`` field, which must be a string."""
    text = data.get("response")
    if not isinstance(text, str):
        raise ModelError(f"unexpected response shape from {url}: 'response' is "
                         f"{type(text).__name__}, not a string")
    return text


def generate_completion(entry, prompt: str, *, temperature: float = 0.2,
                        num_ctx: int = 16384, timeout_s: float = 600.0,
                        transport: Optional[Transport] = None) -> tuple[str, dict]:
    """One /api/generate call via the shared payload + transport.

    Returns (text, raw_json). It applies no gate on either side of the call. An answer that is
    not a JSON object, or whose ``response`` is not a string, raises ModelError.
    """
    payload = build_generate_payload(
        getattr(entry, "serving_tag", None) or getattr(entry, "model", ""),
        prompt, num_ctx=num_ctx, temperature=temperature,
    )
    host = str(getattr(entry, "endpoint", "http://localhost:11434")).rstrip("/")
    raw = call_transport(transport or _urllib_transport,
                         f"{host}/api/generate",
                         json.dumps(payload).encode("utf-8"),
                         timeout_s, host)
    url = f"{host}/api/generate"
    data = load_json_object(raw, url)
    return _response_text(data, url), data


class OllamaModel(ModelClient):
    """ModelClient backed by a local Ollama server's /api/generate endpoint."""

    def __init__(
        self,
        model: str = "example-model:8b",
        host: str = "http://localhost:11434",
        timeout_s: float = 60.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self.name = model
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport or _urllib_transport

    def generate(self, prompt: str, images: Optional[list[str]] = None) -> str:
        """Complete `prompt`. `images` (base64, no data: prefix) engages a
        multimodal model's vision path via Ollama's `images` field; omit it for
        text-only models. An empty list sends no `images` field."""
        url = f"{self.host}/api/generate"
        req: dict = {"model": self.model, "prompt": prompt, "stream": False}
        if images:
            req["images"] = list(images)
        body = json.dumps(req).encode("utf-8")
        raw = self._call(url, body)
        return _response_text(load_json_object(raw, url), url)

    def available(self) -> bool:
        """Cheap reachability check against /api/tags. True only when the answer is a JSON
        object whose ``models`` is a list. False for every ``Exception`` the transport raises
        (`call_transport` turns each one into a ModelError) and for every other answer."""
        url = f"{self.host}/api/tags"
        try:
            data = load_json_object(self._call(url, None), url)
        except ModelError:
            return False
        return isinstance(data.get("models"), list)

    # -- internals -----------------------------------------------------------

    def _call(self, url: str, body: Optional[bytes]) -> bytes:
        """Run the transport through `call_transport` (see there for which errors are typed)."""
        return call_transport(self._transport, url, body, self.timeout_s, self.host)
