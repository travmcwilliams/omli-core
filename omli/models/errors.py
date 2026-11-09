"""Typed model-transport errors (Primitive 2: failures route deterministically).

A model being *unavailable* or *slow* is a different event from a model returning
garbage. These types let the Agent route an unreachable or slow model to a distinct
escape hatch instead of conflating it with a schema violation.
"""

from __future__ import annotations


class ModelError(Exception):
    """Base class for any model-transport failure."""


class ModelUnavailable(ModelError):
    """The model endpoint could not be reached (connection refused, DNS, etc.)."""


class ModelTimeout(ModelError):
    """The model endpoint was reached but did not respond in time."""
