"""Sensitivity tag helpers for the artifact spine and deterministic gates.

Sensitivity tags are stored on artifact provenance and accumulate down the chain.
Some tags are route controls, not just labels: if present, they constrain what model
or egress route may see the payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Tuple

import yaml

LOCAL_ONLY_TAG = "local-only"
LOCAL_ONLY_PREFIX = "local-only:"
# Labels that carry no route control. Everything a tag may be is listed or matched here, so a typo
# or a case variant cannot silently fail open.
CORE_LABELS = frozenset({"public", "internal"})
_LOCAL_ONLY_SCOPE = re.compile(r"[a-z0-9][a-z0-9._-]*")


_POLICY_KEYS = ("policy", "version", "known_labels", "local_model_only_tags")
_POLICY_TAG = re.compile(r"[a-z0-9][a-z0-9._:-]*")


@dataclass(frozen=True)
class SensitivityPolicy:
    """Tags a deployment adds from pack config. The core rules above cannot be removed by one."""

    version: str = "core"
    known_labels: Tuple[str, ...] = ()
    local_model_only_tags: Tuple[str, ...] = ()


_active = SensitivityPolicy()


def parse_policy(data: Mapping[str, Any]) -> SensitivityPolicy:
    """Validate a sensitivity-policy mapping loudly. An unknown key or malformed tag raises."""
    if not isinstance(data, Mapping):
        raise ValueError("sensitivity policy must be a mapping at top level")
    if set(data) != set(_POLICY_KEYS):
        raise ValueError(f"sensitivity policy keys must be exactly {_POLICY_KEYS}, got {sorted(data)}")
    if data["policy"] != "sensitivity":
        raise ValueError("sensitivity policy must declare policy: sensitivity")
    for key in ("known_labels", "local_model_only_tags"):
        tags = data[key]
        if not isinstance(tags, list):
            raise ValueError(f"sensitivity policy {key} must be a list")
        for tag in tags:
            if not isinstance(tag, str) or not _POLICY_TAG.fullmatch(tag):
                raise ValueError(f"sensitivity policy {key}: invalid tag {tag!r} (lowercase, no spaces)")
    return SensitivityPolicy(
        version=str(data["version"]),
        known_labels=tuple(data["known_labels"]),
        local_model_only_tags=tuple(data["local_model_only_tags"]),
    )


def load_policy(path: str | Path) -> SensitivityPolicy:
    """Load and validate a sensitivity-policy pack from YAML."""
    return parse_policy(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def activate_policy(policy: SensitivityPolicy) -> SensitivityPolicy:
    """Add a policy's tags to this process and return the result. ACCUMULATE-ONLY: there is no
    way to drop a blocking tag once active, the same rule the tags follow down the chain."""
    global _active
    _active = SensitivityPolicy(
        version=policy.version,
        known_labels=tuple(dict.fromkeys(_active.known_labels + policy.known_labels)),
        local_model_only_tags=tuple(dict.fromkeys(_active.local_model_only_tags + policy.local_model_only_tags)),
    )
    return _active


def normalize_sensitivity(tags: Iterable[str] | str | None) -> Tuple[str, ...]:
    """Return first-seen, deduplicated, immutable, lowercase sensitivity tags."""
    if tags is None:
        return ()
    raw = (tags,) if isinstance(tags, str) else tuple(tags)
    out: list[str] = []
    for tag in raw:
        value = str(tag).strip().lower()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _is_known_tag(tag: str) -> bool:
    if tag == LOCAL_ONLY_TAG or tag in CORE_LABELS:
        return True
    if tag in _active.known_labels or tag in _active.local_model_only_tags:
        return True
    return tag.startswith(LOCAL_ONLY_PREFIX) and bool(_LOCAL_ONLY_SCOPE.fullmatch(tag[len(LOCAL_ONLY_PREFIX):]))


def validate_sensitivity(tags: Iterable[str] | str | None) -> Tuple[str, ...]:
    """Normalize `tags` and raise ValueError naming any tag that is not known."""
    normalized = normalize_sensitivity(tags)
    unknown = [tag for tag in normalized if not _is_known_tag(tag)]
    if unknown:
        raise ValueError(
            f"unknown sensitivity tag(s) {unknown}: expected {LOCAL_ONLY_TAG!r}, "
            f"{LOCAL_ONLY_PREFIX + '<scope>'!r}, a core label {sorted(CORE_LABELS)}, or a tag from "
            f"the active sensitivity policy (version {_active.version})"
        )
    return normalized


def merge_sensitivity(
    inherited: Iterable[str] | str | None,
    added: Iterable[str] | str | None,
) -> Tuple[str, ...]:
    """Accumulate tags from predecessor and successor without allowing narrowing."""
    return normalize_sensitivity(normalize_sensitivity(inherited) + normalize_sensitivity(added))


def local_model_only_tags(tags: Iterable[str] | str | None) -> Tuple[str, ...]:
    """Tags that make any non-local model route illegal."""
    return tuple(
        tag
        for tag in normalize_sensitivity(tags)
        if tag == LOCAL_ONLY_TAG or tag.startswith(LOCAL_ONLY_PREFIX) or tag in _active.local_model_only_tags
    )


def is_local_provider_kind(provider_kind: str | None) -> bool:
    """Model Registry provider kinds beginning with ``local-`` execute on local hardware."""
    return bool(provider_kind) and str(provider_kind).startswith("local-")


def blocked_nonlocal_model_tags(
    tags: Iterable[str] | str | None,
    provider_kind: str | None,
) -> Tuple[str, ...]:
    """Return route-control tags that block this model provider kind.

    Missing provider_kind is treated as non-local. A caller that cannot identify the
    model route has not proved the route is local, so fail closed.
    """
    blocked = local_model_only_tags(tags)
    if blocked and not is_local_provider_kind(provider_kind):
        return blocked
    return ()
