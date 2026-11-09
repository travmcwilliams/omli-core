"""Deep-freeze and thaw JSON-shaped data (Primitive 4).

`freeze` makes every mapping read-only and every list or tuple a tuple, all the way down, and
copies as it goes, so neither the record nor the caller's original input can change it later.
`thaw` gives back the plain, JSON-able form (dicts and lists) for logs, rows and prompts.

Artifacts, events and governance findings all freeze their payloads with this one function.
Sets and bytearrays are not JSON-shaped and are left as they are.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping


def freeze(value: Any) -> Any:
    """Deep-freeze JSON-shaped data: mappings become read-only, lists and tuples become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """The plain, JSON-able form of frozen data, for logs, rows and prompts."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value
