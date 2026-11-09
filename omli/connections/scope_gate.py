"""Connection Scope Gate — deterministic pre-call gate for external actions (Primitive 2).

The selection checklist is only real if something enforces it. This is that something: the
pure, deterministic gate that `sender.py` and `reader.py` ask before their wires run. No LLM.
Order matters — hard denies first.

There is no post-call gate on this path. `python -m omli.connections` dry-runs this gate
against a registry.

Decisions encoded here:
  * forbidden_paths are an UNCONDITIONAL deny — they win over any ticked row and never escalate,
    and they match regardless of letter case. (A "forbidden-path" entry in approval.escalate_on
    is advisory/audit only; it never softens the deny.)
  * mail (smtp, graph-mail): the recipient must be exactly one plain addr-spec
    (`local@domain`, no quotes, comments, routes, `%`, `!` or second `@`); address rows match
    regardless of case; when `max_sends_per_day` is set, the caller must supply `sends_today`
    or the send is denied.
  * READ-BEFORE-CHANGE: a change-class op must land on a selected row that also grants `read`.
  * "escalate on first change-class op" is opt-in via approval.escalate_on containing
    "change-class-op" — otherwise a validly-selected change-class op is allowed.
  * An action with no sensitivity tags is denied: an unclassified action has not shown it is
    compatible with the target, so the absence never reads as a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Tuple
from urllib.parse import urlparse

from omli.connections.globmatch import glob_match
from omli.connections.schema import (
    CHANGE_OPERATIONS,
    SLACK_OPERATIONS,
    ConnectionEntry,
)
from omli.sensitivity import validate_sensitivity

# Which resource kinds a given operation is scoped against.
_BRANCH_OPS = ("write-branch",)
_BRANCH_KINDS = ("branch",)
_PATH_KINDS = ("path-glob", "endpoint", "scope")
_MAIL_KINDS = ("smtp", "graph-mail")

# One plain addr-spec: a dot-atom local part from a deliberately small alphabet, one '@', and
# a dotted host name. Excludes quotes, comments, source routes, '%' and '!' relaying, a second
# '@', whitespace and every separator, so one string can only ever name one mailbox.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ADDR_SPEC = re.compile(
    r"[A-Za-z0-9_+=-]+(?:\.[A-Za-z0-9_+=-]+)*@" + _LABEL + r"(?:\." + _LABEL + r")+",
    re.ASCII,
)


def is_single_addr_spec(value: object) -> bool:
    """True only for a string that is exactly one plain `local@domain` address."""
    return isinstance(value, str) and len(value) <= 254 and _ADDR_SPEC.fullmatch(value) is not None


@dataclass(frozen=True)
class ConnectionAction:
    """One external action seeking permission from a connection."""

    connection_id: str
    operation: str  # one of schema.CONNECTION_OPERATIONS
    path: str = ""  # file/path the action targets; domain for http-browse / container-egress
    branch: Optional[str] = None  # branch the action targets (for write-branch)
    sensitivity_tags: Tuple[str, ...] = field(default_factory=tuple)
    files_in_change: Optional[int] = None
    lines_in_change: Optional[int] = None
    # http-browse session accounting (checked against the browse blast-radius caps)
    pages_in_session: Optional[int] = None
    captures_for_domain: Optional[int] = None
    # mail send accounting, supplied by the caller (checked against the max_sends_per_day ceiling)
    sends_today: Optional[int] = None
    # Optional host: when set, must not land on a forbidden-path host pattern.
    host: Optional[str] = None

    def __post_init__(self) -> None:
        # The same normalization and validation as provenance, so a tag read off an artifact
        # compares equal here, and an unknown tag fails loudly instead of only failing to match.
        object.__setattr__(self, "sensitivity_tags", validate_sensitivity(self.sensitivity_tags))


@dataclass(frozen=True)
class GateDecision:
    decision: str  # "allow" | "deny" | "escalate"
    reason: Optional[str] = None


def _resolve(approval, trigger: str, reason: str) -> GateDecision:
    """Escalate if the approval policy opts into this trigger; otherwise deny."""
    if approval.escalates_on(trigger):
        return GateDecision("escalate", reason)
    return GateDecision("deny", reason)


def connection_scope_gate(action: ConnectionAction, connection: ConnectionEntry) -> GateDecision:
    """Decide allow / deny / escalate for one action against one connection. Pure + deterministic."""
    selection = connection.selection
    blast = connection.blast_radius
    approval = connection.approval
    target = connection.target

    row_key = action.branch or "" if action.operation in _BRANCH_OPS else action.path

    # 0. HARD DENY — an action names the connection it was authorized for. Never evaluate it
    #    against a different entry's selection.
    if action.connection_id != connection.connection_id:
        return GateDecision(
            "deny", f"connection-id-mismatch: {action.connection_id!r} != {connection.connection_id!r}"
        )

    # 1. HARD DENY — forbidden paths win over any ticked row and never escalate.
    for pattern in blast.forbidden_paths:
        if (glob_match(pattern, action.path, ignore_case=True)
                or (action.branch and glob_match(pattern, action.branch, ignore_case=True))):
            return GateDecision("deny", f"forbidden-path: {action.path or action.branch}")
        if action.host and glob_match(pattern, action.host, ignore_case=True):
            return GateDecision("deny", f"forbidden-path: host {action.host}")

    # 1b. HARD DENY — http-browse is read-only structurally; the validator already refuses to
    #     author a change-class grant, this is defense in depth at act time.
    if target.kind == "http-browse" and action.operation != "read":
        return GateDecision("deny", f"browse-read-only: {action.operation}")

    # 1c. HARD DENY — container-egress permits only 'connect' (egress reachability); any other
    #     operation on it is a category error. Defense in depth over the validator.
    if target.kind == "container-egress" and action.operation != "connect":
        return GateDecision("deny", f"egress-connect-only: {action.operation}")

    # 1d. HARD DENY — slack permits only the messaging-layer operations ('post' outbound,
    #     'read-thread' inbound); any other operation on it is a category error. Defense in
    #     depth over the validator. Note this refuses the artifact-layer 'read' specifically:
    #     reading a channel is 'read-thread', and conflating the two would let a messaging
    #     grant satisfy read-before-change on some other connection's rows.
    if target.kind == "slack" and action.operation not in SLACK_OPERATIONS:
        return GateDecision("deny", f"slack-messaging-only: {action.operation}")

    # 1e. HARD DENY — the mail kinds (smtp, graph-mail) permit only 'send-email' (outbound mail
    #     to a recipient); any other operation on them is a category error. Defense in depth over
    #     the validator. The kinds differ only in transport, never in permitted operation.
    if target.kind in _MAIL_KINDS and action.operation != "send-email":
        return GateDecision("deny", f"{target.kind}-send-only: {action.operation}")
    if target.kind in _MAIL_KINDS and not is_single_addr_spec(action.path):
        return GateDecision("deny", "malformed-recipient")

    # 2. Sensitivity must be present and compatible (fail closed).
    if not action.sensitivity_tags:
        return GateDecision("deny", "sensitivity-missing")
    if any(tag not in target.sensitivity_allowed for tag in action.sensitivity_tags):
        return _resolve(approval, "sensitivity-mismatch", "sensitivity-mismatch")

    # 3. SELECTION — default-deny. The action must match a selected row of the right kind whose
    #    operations include this operation. For http-browse the row is the domain allowlist.
    if target.kind in ("http-browse", "container-egress"):
        kinds = ("domain",)
    elif target.kind == "slack":
        kinds = ("channel",)
    elif target.kind in _MAIL_KINDS:
        kinds = ("address",)
    else:
        kinds = _BRANCH_KINDS if action.operation in _BRANCH_OPS else _PATH_KINDS
    fold = target.kind in _MAIL_KINDS  # mail addresses compare without regard to case
    match = next(
        (
            r
            for r in selection.resources
            if r.selected
            and r.kind in kinds
            and glob_match(r.id, row_key, ignore_case=fold)
            and action.operation in r.operations
        ),
        None,
    )
    if match is None:
        return _resolve(approval, "out-of-selection", f"out-of-selection: {action.operation} on {row_key!r}")

    # 4. READ-BEFORE-CHANGE — a change-class op requires read on the SAME row (defense in depth;
    #    the validator already rejects authoring one without the other).
    if action.operation in CHANGE_OPERATIONS and "read" not in match.operations:
        return GateDecision("deny", f"read-required-for-change: {action.operation} on {match.id!r}")

    # 5. BLAST RADIUS — global ceilings override the selection. Checked before the softer
    #    change-class-op escalation so the more precise cap is the surfaced reason.
    if blast.max_files_per_change is not None and (action.files_in_change or 0) > blast.max_files_per_change:
        return _resolve(approval, "blast-radius-exceeded", "blast-radius-exceeded: files")
    if blast.max_lines_per_change is not None and (action.lines_in_change or 0) > blast.max_lines_per_change:
        return _resolve(approval, "blast-radius-exceeded", "blast-radius-exceeded: lines")
    if blast.max_pages_per_session is not None and (action.pages_in_session or 0) > blast.max_pages_per_session:
        return _resolve(approval, "blast-radius-exceeded", "blast-radius-exceeded: pages")
    if blast.max_captures_per_domain is not None and (action.captures_for_domain or 0) > blast.max_captures_per_domain:
        return _resolve(approval, "blast-radius-exceeded", "blast-radius-exceeded: captures")
    if blast.max_sends_per_day is not None and action.operation == "send-email":
        # A ceiling nobody counts against is no ceiling: an unknown count is refused.
        if type(action.sends_today) is not int or action.sends_today < 0:
            return GateDecision("deny", "sends-today-required")
        if action.sends_today >= blast.max_sends_per_day:
            return _resolve(approval, "blast-radius-exceeded", "blast-radius-exceeded: sends")

    # 6. Optional: escalate on the first change-class op, if the policy opts in.
    if action.operation in CHANGE_OPERATIONS and approval.escalates_on("change-class-op"):
        return GateDecision("escalate", f"change-class-op: {action.operation} on {match.id!r}")

    return GateDecision("allow")
