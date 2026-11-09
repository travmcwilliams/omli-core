"""The fixed vocabulary of a wire outcome, shared by the sender and the reader adapters.

An outcome holds the gate decision, the connection's kind, the target the action addressed,
one reason code from :data:`REASONS`, and the name of a raised exception's class from
:func:`error_type_name`. No text that a wire, a sender or a resolver returns, and no exception
message, is copied into an outcome, so a credential echoed back by a remote service does not
reach one.

Trust boundary. The boundary is the wire, not the process. Code running in this process (the
caller, an injected sender, reader or resolver, and anything they import) is trusted: it chooses
the action, so the action's path appears in ``target`` as given, and it can define exception
classes, so ``error_type`` is not a defence against such code. The adapters catch ``Exception``,
not ``BaseException``: ``KeyboardInterrupt``, ``SystemExit``, ``GeneratorExit`` and other
``BaseException`` subclasses that are not ``Exception`` propagate unchanged.
"""

from __future__ import annotations

import sys
from typing import Optional

#: Reason codes for a refusal or a failure. ``""`` means success.
GATE_REASONS = frozenset({
    "entry-not-registry-loaded",
    "forbidden-path",
    "browse-read-only",
    "egress-connect-only",
    "slack-messaging-only",
    "send-only",
    "malformed-recipient",
    "sensitivity-missing",
    "sensitivity-mismatch",
    "out-of-selection",
    "read-required-for-change",
    "blast-radius-exceeded",
    "sends-today-required",
    "change-class-op",
    "gate-refused",
})
ADAPTER_REASONS = frozenset({
    "attachments-not-supported",
    "sender-override-refused",
    "recipient-refused",
    "no-wire-for-kind",
    "credential-unavailable",
    "wire-raised",
    "wire-returned-no-result",
    "wire-reported-failure",
    "adapter-error",
})
REASONS = frozenset({""}) | GATE_REASONS | ADAPTER_REASONS


def reason_code(gate_reason: Optional[str]) -> str:
    """The closed-set code for a scope-gate reason. The gate's free text (which can repeat the
    action's path) is reduced to its leading code; anything unrecognised is ``gate-refused``."""
    code = str(gate_reason or "").split(":", 1)[0].strip()
    if code.endswith("-send-only"):
        code = "send-only"
    return code if code in GATE_REASONS else "gate-refused"


def error_type_name(exc: BaseException) -> str:
    """The raised class's name (for example ``TimeoutError`` or ``IncompleteRead``), taken from
    the nearest class in its lineage that its module exposes under that name. A class that its
    module does not expose under its own name (for example one made with ``type()`` inside a
    function) is reported by the name of its nearest base that is. This is a naming rule, not a
    security check: code in the process is trusted (see the module docstring)."""
    for cls in type(exc).__mro__:
        module = sys.modules.get(getattr(cls, "__module__", ""), None)
        name = getattr(cls, "__qualname__", "")
        if (module is not None and name.isidentifier()
                and getattr(module, name, None) is cls):
            return name
    return "Exception"
