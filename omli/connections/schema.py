"""ConnectionRegistry — validated, versioned, default-deny catalog of external targets.

The ONLY place a concrete external target + `credential_ref` is named. Packs/agents reference
a connection by `connection_id`; core never names one. Immutable per `registry_version`:
changing the selection, the credential, the blast radius, or the approval policy is a NEW
version + human sign-off (draft -> staging -> production), never an edit to a published one.

Validation is hand-rolled and loud, matching `omli/models/registry.py` (the Python package
does not use zod; the TypeScript contract in `docs/connection-registry.ts` does).
Hard, structural invariants (not defaults that can flip):

  * credential_ref is a `secret://` reference only — never an inline token.
  * DEFAULT-DENY: an unselected resource grants nothing (its `operations` must be empty).
  * READ-BEFORE-CHANGE: a change-class op (propose-pr / write-branch) on a
    resource requires `read` on that same resource. `read` never implies any write.
  * PR-only: approval.auto_merge is False; blast_radius.direct_push / destructive_ops are False.
  * Every boolean field (`selected`, `auto_merge`, `direct_push`, `destructive_ops`) must be a
    YAML boolean: a quoted "true" or "false" is refused.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import yaml

from omli.connections.secrets import validate_ref
from omli.sensitivity import validate_sensitivity

CONNECTION_STATUSES = ("draft", "staging", "production", "deprecated")
TARGET_KINDS = (
    "git-repo",
    "http-service",
    "http-browse",
    "container-egress",
    "slack",
    "smtp",
    "graph-mail",
)
RESOURCE_KINDS = ("path-glob", "branch", "endpoint", "scope", "domain", "channel", "address")

# Operations a connection may permit, per selected resource. Deliberately NO "merge" /
# "write-main": the human merges on the target side. "connect" is egress reachability for a
# container-egress target (the sandbox may open a socket to this domain) — NOT a change-class
# op, and valid ONLY on container-egress rows. "post" is an outbound message to a slack
# channel — NOT a change-class op (no read-before-change), and valid ONLY on slack rows; the
# human approval act for an outbound message lives with the caller, not here.
# "send-email" is an outbound email to a recipient address — NOT change-class, valid ONLY on
# smtp and graph-mail rows, and the per-message human approval likewise lives with the caller.
# "read-thread" is the INBOUND half of a slack channel — reading messages and thread replies
# back off it (conversations.replies / conversations.history), for example the replies to a
# message posted earlier. Valid ONLY on slack rows. It is deliberately NOT the artifact-layer "read":
# that one is the read-before-change partner of propose-pr/write-branch, and letting a
# messaging grant satisfy that check would quietly widen a git connection. Two names, because
# they authorize two unrelated things. Read-class and therefore never change-class, so
# read-before-change does not apply to it either.
CONNECTION_OPERATIONS = ("read", "propose-pr", "write-branch", "connect", "post", "send-email",
                         "read-thread")
# The change-class subset — these require `read` on the same resource.
CHANGE_OPERATIONS = ("propose-pr", "write-branch")
# The messaging-layer subset — the only operations a slack row may grant, and valid nowhere else.
SLACK_OPERATIONS = ("post", "read-thread")

# Approval escalation triggers a connection may name. "change-class-op" makes "escalate on the
# first change-class op" a CONFIGURABLE approval trigger rather than hardcoded.
ESCALATION_TRIGGERS = (
    "out-of-selection",
    "blast-radius-exceeded",
    "forbidden-path",
    "sensitivity-mismatch",
    "change-class-op",
)

_SECRET_PREFIX = "secret://"

_META_REQUIRED = ("registry_id", "registry_version", "authored_at", "authored_by", "status")
# credential_ref is required for every kind EXCEPT the anonymous kinds below (an anonymous
# public-web browse profile, or a container's network-egress boundary, carries no credential;
# if one IS named it must still be a secret:// ref).
_ANONYMOUS_KINDS = ("http-browse", "container-egress")
_TARGET_REQUIRED = ("kind", "identifier", "principal")
_ENTRY_REQUIRED = ("connection_id", "target", "selection", "blast_radius", "approval")
# Change-shaped caps for git/http-service targets; http-browse uses the browse caps instead.
_BLAST_REQUIRED = (
    "max_files_per_change",
    "max_lines_per_change",
    "forbidden_paths",
    "direct_push",
    "destructive_ops",
)
_BLAST_REQUIRED_BROWSE = (
    "max_pages_per_session",
    "max_captures_per_domain",
    "forbidden_paths",
    "direct_push",
    "destructive_ops",
)
# Egress caps: a container-egress boundary needs only the deny overlay + the structural
# falses. The reachability list itself is the selection allowlist, not a numeric cap. A slack
# connection is the same shape: the channel allowlist is the scope, not a numeric cap. A mail
# connection (smtp, graph-mail) is the same base shape — the recipient allowlist is the scope — plus
# an OPTIONAL max_sends_per_day send ceiling (the outbound actuator cap).
_BLAST_REQUIRED_EGRESS = ("forbidden_paths", "direct_push", "destructive_ops")
# Which numeric caps each kind may set. A cap set on any other kind is refused at load, so a
# cap is always None where it does not apply.
_CHANGE_CAPS = frozenset({"max_files_per_change", "max_lines_per_change"})
_CAPS_BY_KIND: Mapping[str, frozenset] = {
    "git-repo": _CHANGE_CAPS,
    "http-service": _CHANGE_CAPS,
    "http-browse": frozenset({"max_pages_per_session", "max_captures_per_domain"}),
    "container-egress": frozenset(),
    "slack": frozenset(),
    "smtp": frozenset({"max_sends_per_day"}),
    "graph-mail": frozenset({"max_sends_per_day"}),
}
_APPROVAL_REQUIRED = ("mode", "required_reviewers", "auto_merge", "escalate_on")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


# -- value objects -----------------------------------------------------------


@dataclass(frozen=True)
class ConnectionRegistryMetadata:
    registry_id: str
    registry_version: str
    authored_at: str
    authored_by: str
    status: str
    description: Optional[str] = None


@dataclass(frozen=True)
class ConnectionTarget:
    kind: str
    identifier: str
    credential_ref: Optional[str]  # None only for the anonymous kinds (_ANONYMOUS_KINDS)
    principal: str
    default_branch: Optional[str] = None
    sensitivity_allowed: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SelectableResource:
    id: str
    kind: str
    selected: bool
    operations: Tuple[str, ...] = field(default_factory=tuple)
    label: Optional[str] = None


@dataclass(frozen=True)
class ConnectionSelection:
    mode: str  # "allowlist"
    resources: Tuple[SelectableResource, ...]

    def selected_resources(self) -> Tuple[SelectableResource, ...]:
        return tuple(r for r in self.resources if r.selected)


@dataclass(frozen=True)
class BlastRadius:
    # Each numeric cap may be set only on the kinds named here; the loader refuses it on any
    # other kind, so it is None there.
    # Change-shaped caps (git-repo / http-service).
    max_files_per_change: Optional[int]
    max_lines_per_change: Optional[int]
    forbidden_paths: Tuple[str, ...]
    direct_push: bool  # structurally False
    destructive_ops: bool  # structurally False
    # Browse-shaped caps (http-browse).
    max_pages_per_session: Optional[int] = None
    max_captures_per_domain: Optional[int] = None
    # Send ceiling (smtp, graph-mail), the outbound-actuator cap.
    max_sends_per_day: Optional[int] = None


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str  # "pr-only"
    required_reviewers: Tuple[str, ...]
    auto_merge: bool  # structurally False
    escalate_on: Tuple[str, ...]

    def escalates_on(self, trigger: str) -> bool:
        return trigger in self.escalate_on


@dataclass(frozen=True)
class ConnectionEntry:
    connection_id: str
    target: ConnectionTarget
    selection: ConnectionSelection
    blast_radius: BlastRadius
    approval: ApprovalPolicy


@dataclass(frozen=True)
class ConnectionRegistry:
    metadata: ConnectionRegistryMetadata
    connections: Tuple[ConnectionEntry, ...]

    def get(self, connection_id: str) -> ConnectionEntry:
        for entry in self.connections:
            if entry.connection_id == connection_id:
                return entry
        raise KeyError(connection_id)

    def ids(self) -> Tuple[str, ...]:
        return tuple(c.connection_id for c in self.connections)

    def __iter__(self):
        return iter(self.connections)


# -- parsing / validation ----------------------------------------------------


def _parse_target(data: Mapping[str, Any], cid: str) -> ConnectionTarget:
    _require(isinstance(data, Mapping), f"connection {cid!r}: target must be a mapping")
    missing = [k for k in _TARGET_REQUIRED if k not in data]
    _require(not missing, f"connection {cid!r}: target missing {', '.join(missing)}")
    kind = str(data["kind"])
    _require(kind in TARGET_KINDS, f"connection {cid!r}: target.kind must be one of {TARGET_KINDS}")

    credential_ref = str(data["credential_ref"]) if data.get("credential_ref") else None
    if kind not in _ANONYMOUS_KINDS:
        _require(
            credential_ref is not None,
            f"connection {cid!r}: target missing credential_ref (required for {kind!r})",
        )
    if credential_ref is not None:
        _require(
            credential_ref.startswith(_SECRET_PREFIX),
            f"connection {cid!r}: credential_ref must be a {_SECRET_PREFIX!r} reference, never an "
            f"inline token (value not shown)",
        )
        try:
            validate_ref(credential_ref)
        except ValueError as exc:
            raise ValueError(f"connection {cid!r}: {exc}") from exc
    try:
        sensitivity_allowed = validate_sensitivity(data.get("sensitivity_allowed") or ())
    except ValueError as exc:
        raise ValueError(f"connection {cid!r}: sensitivity_allowed: {exc}") from exc
    return ConnectionTarget(
        kind=kind,
        identifier=str(data["identifier"]),
        credential_ref=credential_ref,
        principal=str(data["principal"]),
        default_branch=(str(data["default_branch"]) if data.get("default_branch") else None),
        sensitivity_allowed=sensitivity_allowed,
    )


def _parse_resource(data: Mapping[str, Any], cid: str) -> SelectableResource:
    _require(isinstance(data, Mapping), f"connection {cid!r}: each resource must be a mapping")
    _require("id" in data and "kind" in data, f"connection {cid!r}: resource needs 'id' and 'kind'")
    _require("selected" in data, f"connection {cid!r}: resource {data.get('id')!r} needs 'selected'")
    kind = str(data["kind"])
    _require(kind in RESOURCE_KINDS, f"connection {cid!r}: resource kind must be one of {RESOURCE_KINDS}")

    selected = data["selected"]
    # A YAML boolean only: a quoted "false" is a non-empty string, and would read as ticked.
    _require(isinstance(selected, bool),
             f"connection {cid!r}: resource {data.get('id')!r} 'selected' must be true or false "
             "(a YAML boolean)")
    operations = tuple(str(op) for op in (data.get("operations") or ()))
    rid = str(data["id"])
    for op in operations:
        _require(op in CONNECTION_OPERATIONS, f"connection {cid!r}: resource {rid!r} has unknown operation {op!r}")

    # DEFAULT-DENY: an unselected resource grants nothing.
    if not selected:
        _require(
            not operations,
            f"connection {cid!r}: unselected resource {rid!r} must not declare operations (it grants nothing)",
        )
    # READ-BEFORE-CHANGE: a change-class op requires read on the same resource.
    if any(op in CHANGE_OPERATIONS for op in operations):
        _require(
            "read" in operations,
            f"connection {cid!r}: resource {rid!r} grants a change-class op (propose-pr/write-branch) "
            f"without 'read' — read is required to change what you cannot see",
        )
    return SelectableResource(
        id=rid,
        kind=kind,
        selected=selected,
        operations=operations,
        label=(str(data["label"]) if data.get("label") else None),
    )


def _parse_selection(data: Mapping[str, Any], cid: str, status: str) -> ConnectionSelection:
    _require(isinstance(data, Mapping), f"connection {cid!r}: selection must be a mapping")
    mode = str(data.get("mode", ""))
    _require(mode == "allowlist", f"connection {cid!r}: selection.mode must be 'allowlist' (default-deny)")
    resources_data = data.get("resources")
    _require(isinstance(resources_data, list), f"connection {cid!r}: selection.resources must be a list")
    resources = tuple(_parse_resource(r, cid) for r in resources_data)

    seen: set[str] = set()
    for r in resources:
        _require(r.id not in seen, f"connection {cid!r}: duplicate resource id {r.id!r}")
        seen.add(r.id)

    # A live connection with zero selected resources grants nothing — author it deprecated instead.
    if status != "deprecated":
        _require(
            any(r.selected for r in resources),
            f"connection {cid!r}: no selected resources — grants nothing; author it as 'deprecated' instead",
        )
    return ConnectionSelection(mode=mode, resources=resources)


def _parse_blast_radius(data: Mapping[str, Any], cid: str, target_kind: str) -> BlastRadius:
    _require(isinstance(data, Mapping), f"connection {cid!r}: blast_radius must be a mapping")
    required: tuple[str, ...]
    if target_kind == "http-browse":
        required = _BLAST_REQUIRED_BROWSE
    elif target_kind in ("container-egress", "slack", "smtp", "graph-mail"):
        required = _BLAST_REQUIRED_EGRESS
    else:
        required = _BLAST_REQUIRED
    missing = [k for k in required if k not in data]
    _require(not missing, f"connection {cid!r}: blast_radius missing {', '.join(missing)}")

    allowed = _CAPS_BY_KIND.get(target_kind, _CHANGE_CAPS)

    def _cap(key: str) -> Optional[int]:
        if key not in data or data[key] is None:
            return None
        _require(key in allowed,
                 f"connection {cid!r}: blast_radius.{key} does not apply to kind {target_kind!r}")
        value = data[key]
        _require(type(value) is int and value > 0,
                 f"connection {cid!r}: blast_radius caps must be positive integers ({key})")
        return value

    _require(data["direct_push"] is False, f"connection {cid!r}: blast_radius.direct_push must be false (PR-only)")
    _require(
        data["destructive_ops"] is False,
        f"connection {cid!r}: blast_radius.destructive_ops must be false (no deletes / history rewrite)",
    )
    return BlastRadius(
        max_files_per_change=_cap("max_files_per_change"),
        max_lines_per_change=_cap("max_lines_per_change"),
        forbidden_paths=tuple(str(p) for p in (data.get("forbidden_paths") or ())),
        direct_push=False,
        destructive_ops=False,
        max_pages_per_session=_cap("max_pages_per_session"),
        max_captures_per_domain=_cap("max_captures_per_domain"),
        max_sends_per_day=_cap("max_sends_per_day"),
    )


def _parse_approval(data: Mapping[str, Any], cid: str) -> ApprovalPolicy:
    _require(isinstance(data, Mapping), f"connection {cid!r}: approval must be a mapping")
    missing = [k for k in _APPROVAL_REQUIRED if k not in data]
    _require(not missing, f"connection {cid!r}: approval missing {', '.join(missing)}")
    _require(str(data["mode"]) == "pr-only", f"connection {cid!r}: approval.mode must be 'pr-only'")
    _require(
        data["auto_merge"] is False,
        f"connection {cid!r}: approval.auto_merge must be false (merging must stay a human act)",
    )
    reviewers = tuple(str(r) for r in (data.get("required_reviewers") or ()))
    _require(len(reviewers) >= 1, f"connection {cid!r}: approval.required_reviewers needs at least one human")
    escalate_on = tuple(str(t) for t in (data.get("escalate_on") or ()))
    for t in escalate_on:
        _require(t in ESCALATION_TRIGGERS, f"connection {cid!r}: unknown escalate_on trigger {t!r}")
    return ApprovalPolicy(
        mode="pr-only",
        required_reviewers=reviewers,
        auto_merge=False,
        escalate_on=escalate_on,
    )


def _parse_entry(data: Mapping[str, Any]) -> ConnectionEntry:
    _require(isinstance(data, Mapping), "each connection must be a mapping")
    missing = [k for k in _ENTRY_REQUIRED if k not in data]
    _require(not missing, f"connection entry missing keys: {', '.join(missing)}")
    cid = str(data["connection_id"])
    target = _parse_target(data["target"], cid)
    # Status governs the "no selected resources" rule; it lives on the registry metadata.
    selection = _parse_selection(data["selection"], cid, str(data.get("_registry_status", "draft")))
    if target.kind == "http-browse":
        # Read-only browsing is enforced by the registry, not the prompt: a browse connection cannot
        # even be authored with a change-class op, and its rows are domain rows.
        for r in selection.resources:
            _require(
                r.kind == "domain",
                f"connection {cid!r}: http-browse resources must be 'domain' rows (got {r.kind!r} on {r.id!r})",
            )
            _require(
                all(op == "read" for op in r.operations),
                f"connection {cid!r}: http-browse is read-only — resource {r.id!r} may only grant 'read'",
            )
    elif target.kind == "container-egress":
        # An egress boundary names WHICH domains a sandbox may open a socket to. Rows are
        # domain rows and may grant only 'connect' — never a read/change-class op (those are
        # artifact-layer, not network-layer).
        for r in selection.resources:
            _require(
                r.kind == "domain",
                f"connection {cid!r}: container-egress resources must be 'domain' rows (got {r.kind!r} on {r.id!r})",
            )
            _require(
                all(op == "connect" for op in r.operations),
                f"connection {cid!r}: container-egress rows may only grant 'connect' — resource {r.id!r} has {list(r.operations)}",
            )
    elif target.kind == "slack":
        # A slack connection names WHICH channels the bot may post to and read back off. Rows
        # are channel rows and may grant only the two messaging-layer ops — never the
        # artifact-layer read/change-class ones. Neither is change-class, so read-before-change
        # does not apply; the human approval act for an outbound message lives with the
        # caller, while this registry is the deterministic channel-scope.
        #
        # The two are granted INDEPENDENTLY on purpose: 'post' does not imply 'read-thread'.
        # A channel that only receives announcements needs no inbound grant, and a blanket
        # one would hand the bot the contents of every channel it posts to.
        for r in selection.resources:
            _require(
                r.kind == "channel",
                f"connection {cid!r}: slack resources must be 'channel' rows (got {r.kind!r} on {r.id!r})",
            )
            _require(
                all(op in SLACK_OPERATIONS for op in r.operations),
                f"connection {cid!r}: slack channel rows may only grant {list(SLACK_OPERATIONS)} — resource {r.id!r} has {list(r.operations)}",
            )
    elif target.kind in ("smtp", "graph-mail"):
        # An smtp / graph-mail connection names WHICH recipient addresses the send-mailer may
        # email. The two kinds differ ONLY in how OMLi authenticates and dials (smtplib +
        # password vs Graph sendMail + app token), never in what a row means, so they share this
        # validation. Rows are address rows (an exact address or a glob like '*@example.com') and
        # may grant only 'send-email' — never read/change-class (artifact-layer, not mail-layer).
        # 'send-email' is not change-class, so read-before-change does not apply; the
        # per-message human approval act lives with the caller, while
        # this registry is the deterministic recipient-scope + the max_sends_per_day ceiling. The
        # gate compares that ceiling with a count the caller supplies; nothing here keeps one.
        for r in selection.resources:
            _require(
                r.kind == "address",
                f"connection {cid!r}: {target.kind} resources must be 'address' rows (got {r.kind!r} on {r.id!r})",
            )
            _require(
                all(op == "send-email" for op in r.operations),
                f"connection {cid!r}: {target.kind} address rows may only grant 'send-email' — resource {r.id!r} has {list(r.operations)}",
            )
    else:
        # 'connect' is container-egress-only, 'post'/'read-thread' are slack-only, 'send-email'
        # is mail-only (smtp / graph-mail); git-repo / http-service must not use any of them.
        for r in selection.resources:
            _require(
                "connect" not in r.operations,
                f"connection {cid!r}: 'connect' is a container-egress-only operation (resource {r.id!r})",
            )
            for op in SLACK_OPERATIONS:
                _require(
                    op not in r.operations,
                    f"connection {cid!r}: {op!r} is a slack-only operation (resource {r.id!r})",
                )
            _require(
                "send-email" not in r.operations,
                f"connection {cid!r}: 'send-email' is a mail-only operation (smtp / graph-mail) (resource {r.id!r})",
            )
    blast_radius = _parse_blast_radius(data["blast_radius"], cid, target.kind)
    approval = _parse_approval(data["approval"], cid)
    return ConnectionEntry(
        connection_id=cid,
        target=target,
        selection=selection,
        blast_radius=blast_radius,
        approval=approval,
    )


# Loader-issued marker. It is held here, not on the entry, so it cannot be copied or passed to a
# constructor: dataclasses.replace(), copy.deepcopy() and ConnectionEntry(...) all make an object
# with no record. The stored repr() also catches an issued entry changed in place. This stops a
# hand-built entry reaching the wire; it is not a boundary against code that edits this module.
_ISSUED: dict[int, tuple[weakref.ref, str]] = {}


def _issue(entry: ConnectionEntry) -> None:
    key = id(entry)

    def forget(ref: weakref.ref, key: int = key) -> None:
        record = _ISSUED.get(key)
        if record is not None and record[0] is ref:
            del _ISSUED[key]

    _ISSUED[key] = (weakref.ref(entry, forget), repr(entry))


def is_registry_loaded(entry: Any) -> bool:
    """True only for the exact, unchanged object a validated registry parse produced."""
    record = _ISSUED.get(id(entry))
    return record is not None and record[0]() is entry and record[1] == repr(entry)


def parse_registry(data: Mapping[str, Any]) -> ConnectionRegistry:
    """Validate a connection-registry mapping and build an immutable ConnectionRegistry."""
    _require(isinstance(data, Mapping), "connection registry must be a mapping at top level")
    meta = data.get("metadata")
    _require(isinstance(meta, Mapping), "connection registry missing 'metadata' mapping")
    missing = [k for k in _META_REQUIRED if k not in meta]
    _require(not missing, f"registry metadata missing keys: {', '.join(missing)}")
    status = str(meta["status"])
    _require(status in CONNECTION_STATUSES, f"registry status must be one of {CONNECTION_STATUSES}")

    connections_data = data.get("connections")
    _require(isinstance(connections_data, list), "connection registry must list 'connections'")

    # Thread the registry status down so a deprecated registry may hold connections with no
    # selected resources, without mutating caller data.
    _require(all(isinstance(c, Mapping) for c in connections_data),
             "each connection must be a mapping")
    entries = tuple(_parse_entry({**c, "_registry_status": status}) for c in connections_data)
    seen: set[str] = set()
    for e in entries:
        _require(e.connection_id not in seen, f"duplicate connection_id {e.connection_id!r} in registry")
        seen.add(e.connection_id)

    metadata = ConnectionRegistryMetadata(
        registry_id=str(meta["registry_id"]),
        registry_version=str(meta["registry_version"]),
        authored_at=str(meta["authored_at"]),
        authored_by=str(meta["authored_by"]),
        status=status,
        description=(str(meta["description"]) if meta.get("description") else None),
    )
    for entry in entries:
        _issue(entry)
    return ConnectionRegistry(metadata=metadata, connections=entries)


def load_registry(path: str | Path) -> ConnectionRegistry:
    """Load and validate a connection registry from YAML."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return parse_registry(data)
