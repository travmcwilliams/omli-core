"""The Mind-map vocabulary shipped as pack data (``omli/defaults/mindmap.yaml``), read-only.

Relation glosses, icon rules, dump file types and limits, retrieval limits and the offline
auto-design questions all live there, so this package names no concept itself (Primitive 1).
"""

from __future__ import annotations

from functools import lru_cache
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from omli.defaults import default_pack_path

VOCAB_FILE = "mindmap.yaml"
_SECTIONS = ("relations", "icons", "dump", "retrieval", "autodesign")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@lru_cache(maxsize=1)
def vocab() -> Mapping[str, Any]:
    """The whole vocabulary, frozen. Fails loud when a section is missing."""
    data = yaml.safe_load(default_pack_path(VOCAB_FILE).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{VOCAB_FILE} must hold a mapping")
    missing = [s for s in ("name", "version", *_SECTIONS) if s not in data]
    if missing:
        raise ValueError(f"{VOCAB_FILE} is missing: {', '.join(missing)}")
    return _freeze(data)


def section(name: str) -> Mapping[str, Any]:
    return vocab()[name]


def identity() -> tuple[str, str]:
    """(name, version) stamped into provenance for deterministic Mind-map artifacts."""
    v = vocab()
    return str(v["name"]), str(v["version"])


def plain(value: Any) -> Any:
    """A frozen vocabulary value as plain JSON-ready dicts and lists."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [plain(v) for v in value]
    return value
