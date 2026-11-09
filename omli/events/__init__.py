"""Event bus — choreography, not orchestration (there is no central coordinator).

Agents react to events; nothing centrally drives them.
"""

from omli.events.bus import Event, EventBus

__all__ = ["Event", "EventBus"]
