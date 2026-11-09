"""Pack loader — read + validate a pack from YAML into an immutable `Pack`.

Loading is the only path from on-disk config to an in-memory pack. There is no writer:
agents apply packs, they never mutate or persist them. The optional `model_selection` /
`model_requirements` blocks are parsed here, and a pack with requirements but no selection is
rejected. Checking them against a model registry is the Router's job
(`omli.core.router`), which runs when an Agent is constructed or `validate_packs` is called.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from omli.packs.schema import (
    Escalation,
    ModelRequirements,
    ModelSelection,
    Pack,
    Scoring,
)

_REQUIRED_KEYS = ("name", "version", "identity", "prompt_template")


def _parse_model_selection(data: Optional[Mapping[str, Any]]) -> Optional[ModelSelection]:
    if data is None:
        return None
    if "model_id" not in data:
        raise ValueError("model_selection requires a 'model_id'")
    escalation = None
    esc = data.get("escalation")
    if esc is not None:
        if "model_id" not in esc:
            raise ValueError("model_selection.escalation requires a 'model_id'")
        escalation = Escalation(
            model_id=str(esc["model_id"]),
            trigger=str(esc.get("trigger", "gate_rejection")),
            max_escalations=int(esc.get("max_escalations", 1)),
        )
    return ModelSelection(model_id=str(data["model_id"]), escalation=escalation)


def _parse_model_requirements(data: Optional[Mapping[str, Any]]) -> Optional[ModelRequirements]:
    if data is None:
        return None
    return ModelRequirements(
        min_context_window=int(data.get("min_context_window", 0)),
        min_max_output_tokens=int(data.get("min_max_output_tokens", 0)),
        native_tool_calling=bool(data.get("native_tool_calling", False)),
        parallel_tool_calls=bool(data.get("parallel_tool_calls", False)),
        thinking_mode=bool(data.get("thinking_mode", False)),
        vision=bool(data.get("vision", False)),
        structured_output=data.get("structured_output"),
    )


def _parse_scoring(data: Optional[Mapping[str, Any]]) -> Optional[Scoring]:
    if data is None:
        return None
    if "score_field" not in data or "threshold" not in data:
        raise ValueError("scoring requires both 'score_field' and 'threshold'")
    return Scoring(score_field=str(data["score_field"]),
                   threshold=_finite_number(data["threshold"], "scoring.threshold"))


def _finite_number(value: Any, where: str) -> float:
    """A threshold as a finite float. NaN would disable a floor (every comparison with it is
    false) and an infinity would make it meaningless, so both are refused at load."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{where} is out of range") from None
    if not math.isfinite(number):
        raise ValueError(f"{where} must be finite")
    return number


def _parse_thresholds(data: Any) -> dict:
    """The pack's numeric thresholds. Every value must be a finite number."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("thresholds must be a mapping")
    out = {}
    for key, value in data.items():
        _finite_number(value, f"thresholds.{key}")  # validates; the value keeps its own type
        out[str(key)] = value
    return out


def load_pack(path: str | Path) -> Pack:
    """Load and validate a pack YAML file into an immutable `Pack`."""
    data: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"pack file {path} must contain a mapping at top level")

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(f"pack {path} missing required keys: {', '.join(missing)}")

    model_selection = _parse_model_selection(data.get("model_selection"))
    model_requirements = _parse_model_requirements(data.get("model_requirements"))
    # Requirements without a selection is meaningless — fail loud rather than silently ignore.
    if model_requirements is not None and model_selection is None:
        raise ValueError(f"pack {path} declares model_requirements without model_selection")

    return Pack(
        name=str(data["name"]),
        version=str(data["version"]),
        identity=str(data["identity"]),
        prompt_template=str(data["prompt_template"]),
        required_output_fields=tuple(data.get("required_output_fields", ()) or ()),
        thresholds=_parse_thresholds(data.get("thresholds")),
        model_selection=model_selection,
        model_requirements=model_requirements,
        scoring=_parse_scoring(data.get("scoring")),
    )
