"""Contract-sync: the TS spec must not drift from the Python enforcement.

Python is canonical. docs/connection-registry.ts is a readable TypeScript statement of the
same contract, written with zod, for a TypeScript consumer of the registry; no TypeScript
consumer ships here, and nothing in this repository runs the TS file or installs zod. This test fails if the two enumerate different
operations or approval escalate-triggers — the security-critical enums. It parses the two
`z.enum([...])` blocks out of the TS by the declaration text that precedes each, so a
rename on either side without the other is caught.
"""

import re
from pathlib import Path

from omli.connections.schema import CONNECTION_OPERATIONS, ESCALATION_TRIGGERS

TS_PATH = Path(__file__).resolve().parent.parent / "docs" / "connection-registry.ts"


def _enum_after(anchor: str) -> set[str]:
    """Return the string members of the first z.enum([...]) that follows `anchor` in the TS."""
    text = TS_PATH.read_text(encoding="utf-8")
    start = text.index(anchor)
    block = text[start : text.index("]", start)]
    return set(re.findall(r'"([^"]+)"', block))


def test_ts_file_exists():
    assert TS_PATH.exists(), "the TS contract spec must exist"


def test_operations_match():
    ts_ops = _enum_after("export const ConnectionOperation")
    assert ts_ops == set(CONNECTION_OPERATIONS), (
        f"TS operations {ts_ops} != Python {set(CONNECTION_OPERATIONS)} — update the canonical "
        f"Python schema and this spec together"
    )


def test_escalate_triggers_match():
    ts_triggers = _enum_after("escalate_on: z.array(")
    assert ts_triggers == set(ESCALATION_TRIGGERS), (
        f"TS escalate triggers {ts_triggers} != Python {set(ESCALATION_TRIGGERS)}"
    )
