"""Connection Registry — the ONLY place a concrete EXTERNAL target + credential is named.

Names the *external* targets OMLi may mirror or act on (a git repo, an http service). Modeled on the Model
Registry (`omli/models/registry.py`): one immutable-per-version catalog, referenced by
`connection_id`; core and agents never name a target directly.

Scope boundary: local subprocess tools are NOT connections. A connection is an external target reached over a network/git boundary, with a `secret://` credential ref, a
default-deny selection checklist, blast-radius caps, and a PR-only approval policy.

Python is the enforcement (the Python package does not use zod). `docs/connection-registry.ts`
is a subordinate TypeScript contract written with zod, kept in sync by
`tests/test_connection_contract_sync.py`.
"""

from __future__ import annotations

from omli.connections.schema import (
    ApprovalPolicy,
    BlastRadius,
    ConnectionEntry,
    ConnectionRegistry,
    ConnectionSelection,
    ConnectionTarget,
    SelectableResource,
    CONNECTION_OPERATIONS,
    CHANGE_OPERATIONS,
    ESCALATION_TRIGGERS,
    is_registry_loaded,
    load_registry,
    parse_registry,
)
from omli.connections.scope_gate import (
    ConnectionAction,
    GateDecision,
    connection_scope_gate,
)
from omli.connections.secrets import (
    SecretNotFound,
    SecretStatus,
    credential_for,
    resolve_secret,
    secret_status,
)
from omli.connections.sender import (
    DEFAULT_SENDERS,
    LowLevelSender,
    SenderOutcome,
    dispatch,
)
from omli.connections.reader import (
    DEFAULT_READERS,
    READ_OPERATION,
    LowLevelReader,
    ReaderOutcome,
    ReadRefused,
    check_read,
    read,
    slack_fetcher,
)

__all__ = [
    "ApprovalPolicy",
    "BlastRadius",
    "ConnectionEntry",
    "ConnectionRegistry",
    "ConnectionSelection",
    "ConnectionTarget",
    "SelectableResource",
    "CONNECTION_OPERATIONS",
    "CHANGE_OPERATIONS",
    "ESCALATION_TRIGGERS",
    "is_registry_loaded",
    "load_registry",
    "parse_registry",
    "ConnectionAction",
    "GateDecision",
    "connection_scope_gate",
    "SecretNotFound",
    "SecretStatus",
    "credential_for",
    "resolve_secret",
    "secret_status",
    "DEFAULT_SENDERS",
    "LowLevelSender",
    "SenderOutcome",
    "dispatch",
    "DEFAULT_READERS",
    "READ_OPERATION",
    "LowLevelReader",
    "ReaderOutcome",
    "ReadRefused",
    "check_read",
    "read",
    "slack_fetcher",
]
