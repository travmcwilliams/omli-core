"""Append-only event log + synchronous pub/sub (Primitive 4 / choreography).

Events are how status changes are recorded — as appended facts, not mutations — and
how one agent's output triggers the next agent. There is no central orchestrator:
subscribers choreograph themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from omli.frozen import freeze

Handler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """An immutable, ordered fact that something happened. The payload is deep-frozen (nested
    mappings are read-only, lists become tuples) and copied, so neither a subscriber nor the
    emitter's own dict can change it afterwards."""

    name: str
    payload: Mapping[str, Any]
    seq: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze(dict(self.payload)))


class EventBus:
    """Append-only event log with synchronous subscribers."""

    def __init__(self) -> None:
        self._log: list[Event] = []
        self._subscribers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._subscribers.append(handler)

    def emit(self, name: str, payload: Mapping[str, Any] | None = None) -> Event:
        event = Event(name=name, payload=payload or {}, seq=len(self._log))
        self._log.append(event)
        # Single tap for telemetry: every architectural event (gates, model calls,
        # escalations, artifact writes) passes through here, so
        # the whole system is instrumented from one place. Best-effort — never breaks a run.
        try:
            from omli import metrics
            metrics.record_event(name, event.payload)
        except Exception:
            pass
        # Same tap, durable half: when a caller has enabled the sink,
        # the architectural events also land in its directory as append-only facts.
        try:
            from omli.events import sink
            sink.record(name, event.payload, event.seq)
        except Exception:
            pass
        for handler in tuple(self._subscribers):
            handler(event)
        return event

    def log(self) -> tuple[Event, ...]:
        """The full append-only event log."""
        return tuple(self._log)

    def names(self) -> tuple[str, ...]:
        return tuple(e.name for e in self._log)
