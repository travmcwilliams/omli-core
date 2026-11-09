"""Scope-gated reader adapter — the deterministic gate wraps the INBOUND wire (Primitive 2).

The mirror image of :mod:`omli.connections.sender`, and it exists for the same reason: the
Connection Registry NAMES a scope, the scope gate ENFORCES it, :mod:`omli.connections.secrets`
RESOLVES the credential, and only a clean "allow" touches the network. Without something on this
path asking, a ``read-thread`` grant would be a claim no code checked.

Flow, fail-closed:
  1. ``connection_scope_gate(action, connection)`` — ONLY an "allow" proceeds. A deny or an
     escalate returns WITHOUT reading (nothing reaches this machine on anything but a clean allow).
  2. ``credential_for(connection, required=True)`` — resolve the ``secret://`` ref at use time.
     Missing -> fail closed. The value is NEVER logged, printed, or put on the outcome.
  3. the low-level reader for ``target.kind`` fires. Readers are INJECTABLE so tests never
     touch the network; the default is the real wire.

**Reading is its own grant.** ``read-thread`` is not the artifact-layer ``read`` (the
read-before-change partner of propose-pr), and it is not implied by ``post``: announcing into
a channel must not confer the right to read every message in it. Both distinctions live in
:mod:`omli.connections.schema` and are enforced by the gate; this module only ever asks for
``read-thread``, so it cannot smuggle either.

**A refusal must be visible.** :func:`check_read` is the pure pre-flight for any caller that
reads in a loop: run it ONCE before the loop — no credential, no wire, just the decision. A
loop that skipped it would have every deny swallowed by its own retries and would look exactly
like a channel where nobody answered. That failure mode is why this is a separate, cheap,
deterministic call rather than an inference from an empty result.

The outcome's status fields are fixed (see :mod:`omli.connections.outcome`): ok, the gate
decision, the kind, the target the action addressed, a reason code from a closed set, and an
exception type name. Nothing a reader or a resolver returns or raises is copied into them as
text. The messages a successful read returns are a separate field holding the channel's content
as the wire returned it: untrusted data, never merged into outcome text, and not inspected or
rewritten here. Any ``Exception`` raised anywhere in `read` comes back as an outcome.

Trust boundary: the wire is untrusted; code in this process is trusted. The caller chooses the
channel, so it appears in ``target`` as given; an injected reader or resolver can define its own
exception classes, so ``error_type`` is not a defence against it. ``read`` catches
``Exception``, not ``BaseException``: ``KeyboardInterrupt``, ``SystemExit`` and other
``BaseException`` subclasses propagate unchanged.

There is no LLM anywhere in this path — it is a pure deterministic sensor, so it is not subject
to the pre/post *model* sandwich; the scope gate is its deterministic pre-check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from omli.net import SAFE_OPENER
from omli.connections.outcome import error_type_name, reason_code
from omli.connections.schema import ConnectionEntry, is_registry_loaded, load_registry
from omli.connections.scope_gate import ConnectionAction, GateDecision, connection_scope_gate
from omli.connections.secrets import credential_for
from omli.defaults import default_pack_path

#: The one operation this module ever asks for. Named once, here, so no caller can widen it.
READ_OPERATION = "read-thread"

#: A low-level reader turns an approved (query, credential, connection, action) into a wire
#: call. It receives the resolved credential — it MUST NOT log or persist it. Return
#: ``{"ok": True, "messages": [{...}, ...]}`` on success: ``ok`` is the boolean ``True`` and
#: ``messages`` is a list of mappings.
LowLevelReader = Callable[[dict, Optional[str], ConnectionEntry, ConnectionAction], dict]


class ReadRefused(RuntimeError):
    """The gate did not clear this read, the wire failed, the connection is not in the registry,
    or the registry cannot be loaded. Raised by :func:`slack_fetcher`, for each of those.

    A poller that catches this and retries is behaving correctly for a transport hiccup and
    incorrectly for a deny — which is what :func:`check_read` is for.
    """


@dataclass(frozen=True)
class ReaderOutcome:
    """What one attempted read produced. The status fields are fixed values chosen by this
    module. ``messages`` is the channel content exactly as the wire returned it: UNTRUSTED data
    for the caller to treat as such, never copied into a status field. It is empty on every
    unhappy path — there is no partial read."""

    ok: bool
    decision: str                  # scope-gate decision: "allow" | "deny" | "escalate"
    kind: str                      # target.kind ("slack" | ...), from the registry entry
    target: str                    # the channel the action addressed (action.path)
    reason: str = ""               # a code from omli.connections.outcome.REASONS; "" on success
    error_type: str = ""           # the raised class's name (see error_type_name), else ""
    messages: tuple = ()           # untrusted channel content, as returned by the wire


def default_registry_path() -> Path:
    """The registry used when a caller names none. In order: OMLI_CONNECTION_REGISTRY;
    packs/connection_registry.yaml under OMLI_HOME when that variable is set and the file exists;
    the example shipped in the package.

    The current directory is never searched: a registry planted there could widen what is allowed.
    """
    override = os.environ.get("OMLI_CONNECTION_REGISTRY")
    if override:
        return Path(override)
    home = os.environ.get("OMLI_HOME")
    if home:
        own = Path(home).expanduser() / "packs" / "connection_registry.yaml"
        if own.is_file():
            return own
    return default_pack_path("connection_registry.example.yaml")


# --------------------------------------------------------------------------- #
# Default low-level readers (the real wires). Injectable — tests pass fakes.
# --------------------------------------------------------------------------- #
def _default_slack_reader(query: dict, credential: Optional[str],
                          connection: ConnectionEntry, action: ConnectionAction) -> dict:
    """GET the messages on one Slack surface. ``action.path`` is the (gate-approved) channel.

    One optional query key, plain data so no caller-specific logic lives here:

    * ``thread_ts`` — read the replies inside that thread (``conversations.replies``) instead
      of the channel itself (``conversations.history``).

    Both surfaces are the SAME channel the gate approved, and both are the same grant: a thread
    is not a separate scope, it is a subset of the channel already ticked for ``read-thread``.
    Each call reads one surface. A human may answer a threaded question as a top-level channel
    message, so a caller waiting for an answer should read both.
    """
    import json
    import urllib.parse
    import urllib.request

    params: dict[str, Any] = {"channel": action.path, "limit": int(query.get("limit") or 50)}
    thread_ts = str(query.get("thread_ts") or "")
    if thread_ts:
        method = "conversations.replies"
        params["ts"] = thread_ts
    else:
        method = "conversations.history"
    req = urllib.request.Request(
        f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {credential}"},
    )
    with SAFE_OPENER.open(req, timeout=20) as resp:      # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    # Success is the JSON boolean true and nothing else; ``messages`` is passed on as parsed, and
    # `read` refuses any shape other than a list of objects.
    return {"ok": data.get("ok") is True, "channel": action.path, "method": method,
            "messages": data.get("messages")}


DEFAULT_READERS: Mapping[str, LowLevelReader] = {
    "slack": _default_slack_reader,
}


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
def check_read(connection: ConnectionEntry, channel: str, *,
               sensitivity_tags: Sequence[str] = ()) -> GateDecision:
    """The pure pre-flight: may this connection read `channel`? No credential, no wire.

    Run it once before a poll loop and SAY what it returns. Inferring a deny from an empty
    result is the same mistake as inferring "nobody answered" from a query that never ran.
    """
    if not is_registry_loaded(connection):
        return GateDecision("deny", "entry-not-registry-loaded")
    action = ConnectionAction(connection.connection_id, READ_OPERATION, path=channel,
                             sensitivity_tags=tuple(t for t in sensitivity_tags if t))
    return connection_scope_gate(action, connection)


def read(connection: ConnectionEntry, channel: str, query: Optional[dict] = None, *,
         sensitivity_tags: Sequence[str] = (),
         readers: Optional[Mapping[str, LowLevelReader]] = None,
         resolve: Callable[..., Optional[str]] = credential_for) -> ReaderOutcome:
    """Gate -> resolve credential -> read. Nothing is read unless the scope gate returns a clean
    "allow" AND the credential resolves. The status fields are fixed values, ``messages`` is
    untrusted channel content kept apart from them, and any ``Exception`` from any step comes
    back as an outcome rather than being raised (``BaseException`` subclasses that are not
    ``Exception`` propagate). Only an entry produced by the registry
    loader is accepted."""
    kind = str(getattr(getattr(connection, "target", None), "kind", ""))
    target = channel if isinstance(channel, str) else ""
    try:
        return _read(connection, target, query, kind, sensitivity_tags, readers, resolve)
    except Exception as exc:  # a failure before the wire: nothing was read
        return ReaderOutcome(ok=False, decision="deny", kind=kind, target=target,
                             reason="adapter-error", error_type=error_type_name(exc))


def _read(connection, channel, query, kind, sensitivity_tags, readers, resolve) -> ReaderOutcome:
    if not is_registry_loaded(connection):
        return ReaderOutcome(ok=False, decision="deny", kind=kind, target=channel,
                             reason="entry-not-registry-loaded")
    action = ConnectionAction(connection.connection_id, READ_OPERATION, path=channel,
                             sensitivity_tags=tuple(t for t in sensitivity_tags if t))
    gate = connection_scope_gate(action, connection)
    if gate.decision != "allow":
        # deny OR escalate: the deterministic gate did not clear it — do not read.
        return ReaderOutcome(ok=False, decision=gate.decision, kind=kind, target=channel,
                             reason=reason_code(gate.reason))

    table = readers if readers is not None else DEFAULT_READERS
    reader = table.get(kind)
    if reader is None:
        return ReaderOutcome(ok=False, decision="allow", kind=kind, target=channel,
                             reason="no-wire-for-kind")

    try:
        credential = resolve(connection, required=True)
    except Exception as exc:  # SecretNotFound, or any other resolver failure
        # Fail closed: an approved, scoped read still must not fire without its credential.
        return ReaderOutcome(ok=False, decision="allow", kind=kind, target=channel,
                             reason="credential-unavailable", error_type=error_type_name(exc))

    try:
        result = reader(dict(query or {}), credential, connection, action)
    except Exception as exc:  # every wire failure, including http.client errors
        return ReaderOutcome(ok=False, decision="allow", kind=kind, target=channel,
                             reason="wire-raised", error_type=error_type_name(exc))
    if not isinstance(result, dict):
        return ReaderOutcome(ok=False, decision="allow", kind=kind, target=channel,
                             reason="wire-returned-no-result")
    # The base lookups: a dict subclass cannot run code here.
    if dict.get(result, "ok") is not True:
        return ReaderOutcome(ok=False, decision="allow", kind=kind, target=channel,
                             reason="wire-reported-failure")
    # ``messages`` must be a list of mappings. Any other shape (missing, None, a dict, a string,
    # a list holding a non-mapping) is a reported failure, never an empty or partial success.
    messages = dict.get(result, "messages")
    if not isinstance(messages, list) or not all(isinstance(m, Mapping) for m in messages):
        return ReaderOutcome(ok=False, decision="allow", kind=kind, target=channel,
                             reason="wire-returned-no-result")
    return ReaderOutcome(ok=True, decision="allow", kind=kind, target=channel,
                         messages=tuple(messages))


def slack_fetcher(connection_id: str, channel: str, *, thread_ts: str = "", limit: int = 50,
                  sensitivity_tags: Sequence[str] = (),
                  registry_path: Optional[Path] = None,
                  readers: Optional[Mapping[str, LowLevelReader]] = None,
                  resolve: Optional[Callable[..., Optional[str]]] = None):
    """A zero-arg fetcher: one gated read per call, messages out.

    Called with nothing, it returns the list of messages the reader returned (with the default
    reader, Slack's own message shape). The registry is re-read on every call so a revoked grant takes effect on the next poll
    rather than at the next process start — a poll loop can outlive a revocation.

    Raises :class:`ReadRefused` on a gate refusal or a wire failure, never returning an empty
    list for either: a polling caller reads an empty list as "nothing yet, ask again", which is
    right for silence and wrong for a denial. Pre-flight with :func:`check_read` so a deny is
    reported once instead of being retried until the window closes.
    """
    reg_path = registry_path or default_registry_path()
    tags = tuple(t for t in sensitivity_tags if t)

    def _fetch():
        try:
            connection = load_registry(reg_path).get(connection_id)
        except KeyError:
            raise ReadRefused(f"deny: connection {connection_id!r} is not in the registry") from None
        except Exception as exc:  # the registry cannot be read or parsed: nothing may be read
            raise ReadRefused(f"deny: the registry cannot be loaded ({error_type_name(exc)})") from None
        kwargs: dict[str, Any] = {}
        if readers is not None:
            kwargs["readers"] = readers
        if resolve is not None:
            kwargs["resolve"] = resolve
        out = read(connection, channel, {"thread_ts": thread_ts, "limit": limit},
                   sensitivity_tags=tags, **kwargs)
        if not out.ok:
            suffix = f" ({out.error_type})" if out.error_type else ""
            raise ReadRefused(f"{out.decision}: {out.reason}{suffix}")
        return list(out.messages)

    return _fetch
