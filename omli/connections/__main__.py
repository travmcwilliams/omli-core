"""Dry-run the connection scope gate against a registry.

    python -m omli.connections                       # the default registry, see reader.default_registry_path
    python -m omli.connections path/to/registry.yaml  # any connection registry

Loads + validates the registry, then evaluates a handful of actions derived from the first
connection's own selection and prints allow / deny / escalate with the reason. The samples are
the first eight of: one blocked action per unselected row, and the read / propose-pr /
write-branch operations its selected rows grant; then a forbidden-path probe when it names forbidden paths, and a sensitivity
probe when a selected row grants read. No sample uses connect, post, read-thread or send-email,
so for an egress, slack or mail connection the selected rows get no sample.
"""

from __future__ import annotations

import sys
from pathlib import Path

from omli.connections.authoring import default_samples
from omli.connections.reader import default_registry_path
from omli.connections.scope_gate import connection_scope_gate
from omli.connections.schema import load_registry

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = Path(argv[0]) if argv else default_registry_path()
    registry = load_registry(path)
    print(f"connection registry: {registry.metadata.registry_id} "
          f"v{registry.metadata.registry_version} [{registry.metadata.status}]")
    print(f"connections: {', '.join(registry.ids())}\n")

    connection = registry.connections[0]
    print(f"dry-running gate against connection {connection.connection_id!r} "
          f"(target {connection.target.identifier})\n")
    for label, action in default_samples(connection):
        decision = connection_scope_gate(action, connection)
        reason = f" -> {decision.reason}" if decision.reason else ""
        print(f"  [{decision.decision.upper():>8}] {label}{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
