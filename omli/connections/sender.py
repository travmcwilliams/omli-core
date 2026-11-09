"""Scope-gated sender adapter — the deterministic gate wraps the outbound actuator (Primitive 2).

The Connection Registry NAMES a channel/recipient scope; the scope gate ENFORCES it; secrets.py
RESOLVES the credential. This module is the piece that ties the three together and actually
dispatches an outbound message.

Flow, fail-closed:
  1. ``connection_scope_gate(action, connection)`` — ONLY an "allow" proceeds. A deny or escalate
     returns WITHOUT dispatching (nothing leaves this machine on anything but a clean allow).
  2. ``credential_for(connection, required=True)`` — resolve the ``secret://`` ref to its value at
     use time. Missing -> fail closed (no dispatch). The value is NEVER logged, printed, or put on
     the returned outcome.
  3. the low-level sender for ``target.kind`` fires (slack post, smtp or graph-mail send). Senders are
     INJECTABLE so tests never touch the network; the defaults are the real wires.

There is no LLM anywhere in this path — it is a pure deterministic actuator, so it is not subject
to the pre/post *model* sandwich; the scope gate is its deterministic pre-check.

Mail is held to more than the addressed recipient. Every To, cc and bcc address must be one
plain addr-spec and passes the same gate, every one of them counts against `max_sends_per_day`,
the sending mailbox is always the registry's principal, and attachments do not exist. Each
refusal is decided here, before a credential is resolved or a sender called.

The outcome holds fixed fields only (see :mod:`omli.connections.outcome`): ok, the gate
decision, the kind, the target the action addressed, a reason code from a closed set, and the
name of a raised exception's class. No text a sender or a resolver returns, and no exception
message, is copied into it, so a credential echoed back by a remote service does not reach it.
Any ``Exception`` raised anywhere in `dispatch` (the gate, the mail checks, the resolver or the
wire) comes back as an outcome.

Trust boundary: the wire is untrusted; code in this process is trusted. The caller chooses the
action, so its path appears in ``target`` as given; an injected sender or resolver can define
its own exception classes, so ``error_type`` is not a defence against it. ``dispatch`` catches
``Exception``, not ``BaseException``: ``KeyboardInterrupt``, ``SystemExit`` and other
``BaseException`` subclasses propagate unchanged.

This module does not check for human approval, and nothing here enforces one. A caller that
needs per-message approval or redaction must do that before calling this adapter.
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Optional

from omli.net import SAFE_OPENER
from omli.connections.outcome import error_type_name, reason_code
from omli.connections.schema import ConnectionEntry, is_registry_loaded
from omli.connections.scope_gate import (
    ConnectionAction,
    connection_scope_gate,
    is_single_addr_spec,
)
from omli.connections.secrets import credential_for

# A low-level sender turns an approved (payload, credential, connection, action) into a wire call.
# It receives the resolved credential value — it MUST NOT log or persist it. Return {"ok": bool, ...};
# `dispatch` reads only whether "ok" is True and copies nothing else from the result.
LowLevelSender = Callable[[dict, Optional[str], ConnectionEntry, ConnectionAction], dict]

_MAIL_KINDS = ("smtp", "graph-mail")
_SENDER_KEYS = ("from", "from_addr", "sender")
_ATTACHMENT_KEYS = ("attachment", "attachments")


@dataclass(frozen=True)
class SenderOutcome:
    """The result of an attempted dispatch, in fixed fields only. No field holds text that a
    sender or a resolver produced."""

    ok: bool
    decision: str            # scope-gate decision: "allow" | "deny" | "escalate"
    kind: str                # target.kind ("slack" | "smtp" | ...), from the registry entry
    target: str              # the channel / recipient the action addressed (action.path)
    reason: str = ""         # a code from omli.connections.outcome.REASONS; "" on success
    error_type: str = ""     # the raised class's name (see error_type_name), else ""


# --------------------------------------------------------------------------- #
# Default low-level senders (the real wires). Injectable — tests pass fakes.
# --------------------------------------------------------------------------- #
def _addresses(value) -> list[str]:
    """A payload's to, cc or bcc value as a list of strings. Raises ValueError on any other
    shape, including a list holding anything but strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"expected an address or a list of addresses, got {type(value).__name__}")
    if not all(isinstance(a, str) for a in value):
        raise ValueError("every address must be a string")
    return [a.strip() for a in value if a.strip()]


def _to_addresses(payload: dict, action: ConnectionAction) -> list[str]:
    return list(dict.fromkeys([action.path, *_addresses(payload.get("to"))]))


def _default_slack_sender(payload: dict, credential: Optional[str],
                          connection: ConnectionEntry, action: ConnectionAction) -> dict:
    """POST chat.postMessage with the bot token. `action.path` is the (gate-approved) channel.

    Two optional payload keys, both plain data so no caller-specific logic lives here:

    * ``update_ts`` — edit that existing message in place (``chat.update``) instead of posting
      a new one.
    * ``thread_ts`` — post as a reply inside that thread.

    Neither widens the scope gate's grant: both address the SAME channel it already approved.
    """
    import json
    import urllib.request

    message: dict = {
        "channel": action.path,
        "text": payload.get("text") or payload.get("body") or payload.get("title") or "",
    }
    update_ts = str(payload.get("update_ts") or "")
    if update_ts:
        message["ts"] = update_ts
        method = "chat.update"
    else:
        method = "chat.postMessage"
        if payload.get("thread_ts"):
            message["thread_ts"] = str(payload["thread_ts"])
    body = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {credential}"},
    )
    with SAFE_OPENER.open(req, timeout=10) as resp:      # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    # Success is the JSON boolean true and nothing else: "false", 1 or any other value is a failure.
    return {"ok": data.get("ok") is True, "channel": action.path,
            "ts": data.get("ts"), "error": data.get("error")}


def _default_smtp_sender(payload: dict, credential: Optional[str],
                         connection: ConnectionEntry, action: ConnectionAction) -> dict:
    """Send one message over SMTP+STARTTLS. `dispatch` has already gated every recipient.
    Host/port parse from the connection identifier ("smtp:host:port"); the registry principal is
    both the from-address and the SMTP login user. `send_message` delivers to Bcc addresses and
    leaves that header out of what it transmits."""
    import smtplib
    from email.message import EmailMessage

    host, _, port = connection.target.identifier.removeprefix("smtp:").partition(":")
    from_addr = connection.target.principal
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(_to_addresses(payload, action))
    ccs = _addresses(payload.get("cc"))
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    bccs = _addresses(payload.get("bcc"))
    if bccs:
        msg["Bcc"] = ", ".join(bccs)
    msg["Subject"] = payload.get("subject") or payload.get("title") or "(no subject)"
    msg.set_content(payload.get("body") or payload.get("text") or "")
    with smtplib.SMTP(host, int(port or 587), timeout=15) as srv:
        srv.starttls()
        srv.login(from_addr, credential or "")
        srv.send_message(msg)
    return {"ok": True, "to": action.path, "from": from_addr}


def _default_graph_mail_sender(payload: dict, credential: Optional[str],
                               connection: ConnectionEntry, action: ConnectionAction) -> dict:
    """Send one message via Microsoft Graph `sendMail` (app-only client credentials).

    The Graph counterpart of `_default_smtp_sender`: it authenticates with an app token instead
    of an SMTP login. The credential is the app's client secret — used to mint a short-lived token here and never logged or returned.
    Identifier carries the non-secret coordinates: "graph-mail:<tenant_id>:<client_id>".
    `connection.target.principal` is the sending mailbox; `dispatch` has already gated every
    recipient. Graph answers 202 with an empty body on success.
    """
    import json
    import urllib.parse
    import urllib.request

    rest = connection.target.identifier.removeprefix("graph-mail:")
    tenant_id, _, client_id = rest.partition(":")
    if not tenant_id or not client_id:
        return {"ok": False, "error": f"malformed identifier {connection.target.identifier!r} "
                                     f"(want 'graph-mail:<tenant_id>:<client_id>')"}

    token_body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": credential or "",
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode("utf-8")
    token_req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data=token_body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with SAFE_OPENER.open(token_req, timeout=15) as resp:  # noqa: S310
            token = json.loads(resp.read().decode("utf-8")).get("access_token")
    except urllib.error.HTTPError as exc:
        # Never surface the credential; HTTP status alone is enough to diagnose.
        return {"ok": False, "to": action.path,
                "error": f"token request failed HTTP {exc.code}"}
    except OSError as exc:
        return {"ok": False, "to": action.path, "error": f"token request failed: {type(exc).__name__}"}
    if not token:
        # Fail closed and say nothing about the credential itself.
        return {"ok": False, "to": action.path, "error": "token request returned no access_token"}

    from_addr = connection.target.principal
    msg_body: dict = {
        "subject": payload.get("subject") or payload.get("title") or "(no subject)",
        "body": {"contentType": "Text",
                 "content": payload.get("body") or payload.get("text") or ""},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in _to_addresses(payload, action)],
    }
    for field_name, key in (("ccRecipients", "cc"), ("bccRecipients", "bcc")):
        addresses = _addresses(payload.get(key))
        if addresses:
            msg_body[field_name] = [{"emailAddress": {"address": addr}} for addr in addresses]
    message = {"message": msg_body, "saveToSentItems": True}
    send_req = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(from_addr)}/sendMail",
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with SAFE_OPENER.open(send_req, timeout=60) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as exc:
        return {"ok": False, "to": action.path, "from": from_addr,
                "error": f"sendMail failed HTTP {exc.code}"}
    except OSError as exc:
        return {"ok": False, "to": action.path, "from": from_addr,
                "error": f"sendMail failed: {type(exc).__name__}"}
    return {"ok": status in (200, 202), "to": action.path, "from": from_addr, "status": status}


DEFAULT_SENDERS: Mapping[str, LowLevelSender] = {
    "slack": _default_slack_sender,
    "smtp": _default_smtp_sender,
    "graph-mail": _default_graph_mail_sender,
}


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
def _mail_refusal(connection: ConnectionEntry, action: ConnectionAction, payload: dict):
    """Why this mail may not leave, as (decision, reason code), or None. Runs after the
    addressed recipient has cleared the gate, and holds every other address to the same gate."""
    for key in _ATTACHMENT_KEYS:
        if key in payload:
            return "deny", "attachments-not-supported"
    principal = connection.target.principal.strip().casefold()
    for key in _SENDER_KEYS:
        if key in payload and str(payload[key]).strip().casefold() != principal:
            return "deny", "sender-override-refused"
    try:
        recipients = {"to": _to_addresses(payload, action), "cc": _addresses(payload.get("cc")),
                      "bcc": _addresses(payload.get("bcc"))}
    except ValueError:
        return "deny", "malformed-recipient"
    for addresses in recipients.values():
        for address in addresses:
            if not is_single_addr_spec(address):
                return "deny", "malformed-recipient"
            gate = connection_scope_gate(replace(action, path=address), connection)
            if gate.decision != "allow":
                return gate.decision, "recipient-refused"
    # Every delivery counts against the daily ceiling, not one per message. The gate has
    # already refused an absent count when a ceiling is set.
    cap = connection.blast_radius.max_sends_per_day
    if cap is not None:
        total = len({a.casefold() for addresses in recipients.values() for a in addresses})
        if (action.sends_today or 0) + total > cap:
            decision = "escalate" if connection.approval.escalates_on("blast-radius-exceeded") else "deny"
            return decision, "blast-radius-exceeded"
    return None


def _target(action: ConnectionAction) -> str:
    path = getattr(action, "path", "")
    return path if isinstance(path, str) else ""


def dispatch(connection: ConnectionEntry, action: ConnectionAction, payload: dict, *,
             senders: Optional[Mapping[str, LowLevelSender]] = None,
             resolve: Callable[..., Optional[str]] = credential_for) -> SenderOutcome:
    """Gate -> resolve credential -> dispatch. Nothing leaves this machine unless the scope gate
    returns a clean "allow" for every recipient AND the credential resolves. The outcome holds
    fixed fields only, and any ``Exception`` from any step comes back as an outcome rather than
    being raised (``BaseException`` subclasses that are not ``Exception`` propagate). Only an entry produced by the registry loader is accepted."""
    kind = str(getattr(getattr(connection, "target", None), "kind", ""))
    try:
        return _dispatch(connection, action, payload, kind, senders, resolve)
    except Exception as exc:  # a failure before the wire: nothing was sent
        return SenderOutcome(ok=False, decision="deny", kind=kind, target=_target(action),
                             reason="adapter-error", error_type=error_type_name(exc))


def _dispatch(connection, action, payload, kind, senders, resolve) -> SenderOutcome:
    target = _target(action)
    if not is_registry_loaded(connection):
        return SenderOutcome(ok=False, decision="deny", kind=kind, target=target,
                             reason="entry-not-registry-loaded")
    gate = connection_scope_gate(action, connection)
    if gate.decision != "allow":
        # deny OR escalate: the deterministic gate did not clear it — do not dispatch.
        return SenderOutcome(ok=False, decision=gate.decision, kind=kind, target=target,
                             reason=reason_code(gate.reason))
    if kind in _MAIL_KINDS:
        refusal = _mail_refusal(connection, action, payload)
        if refusal is not None:
            return SenderOutcome(ok=False, decision=refusal[0], kind=kind, target=target,
                                 reason=refusal[1])

    table = senders if senders is not None else DEFAULT_SENDERS
    sender = table.get(kind)
    if sender is None:
        return SenderOutcome(ok=False, decision="allow", kind=kind, target=target,
                             reason="no-wire-for-kind")

    try:
        credential = resolve(connection, required=True)
    except Exception as exc:  # SecretNotFound, or any other resolver failure
        # Fail closed: an approved, scoped action still must not fire without its credential.
        return SenderOutcome(ok=False, decision="allow", kind=kind, target=target,
                             reason="credential-unavailable", error_type=error_type_name(exc))

    try:
        result = sender(payload, credential, connection, action)
    except Exception as exc:  # every wire failure, including http.client and smtplib errors
        return SenderOutcome(ok=False, decision="allow", kind=kind, target=target,
                             reason="wire-raised", error_type=error_type_name(exc))
    if not isinstance(result, dict):
        return SenderOutcome(ok=False, decision="allow", kind=kind, target=target,
                             reason="wire-returned-no-result")
    ok = dict.get(result, "ok") is True  # the base lookup: a dict subclass cannot run code here
    return SenderOutcome(ok=ok, decision="allow", kind=kind, target=target,
                         reason="" if ok else "wire-reported-failure")
