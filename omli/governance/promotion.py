"""propose_promotion — produce a NEW registry version with a model's status changed.

Pure and side-effect-free (Primitive 5 / no auto-adoption): it returns a new registry
mapping for a human to review and save. It does not mutate its input, write any file, or
touch a live registry. The returned mapping is validated so a human never saves a broken
registry.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from omli.models.registry import MODEL_STATUSES, parse_registry


def propose_promotion(
    registry_data: Mapping[str, Any],
    model_id: str,
    new_status: str,
    *,
    new_version: str,
) -> dict:
    """Return a new registry mapping with `model_id` set to `new_status` and a bumped version.

    Raises ValueError if the status is unknown, the model is absent, the version is unchanged, or
    the proposed registry would not validate. The caller (a human) reviews and persists the result.
    """
    if new_status not in MODEL_STATUSES:
        raise ValueError(f"new_status must be one of {MODEL_STATUSES}")

    data = copy.deepcopy(dict(registry_data))
    found = False
    for model in data.get("models", []):
        if model.get("model_id") == model_id:
            model["status"] = new_status
            found = True
    if not found:
        raise ValueError(f"model {model_id!r} not in registry")

    if data.get("metadata", {}).get("registry_version") == new_version:
        raise ValueError("a promotion must name a new registry version")
    data.setdefault("metadata", {})["registry_version"] = new_version

    # Validate the proposal so a human can't be handed a broken registry.
    parse_registry(data)
    return data
