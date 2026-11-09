"""An unknown connection kind is refused loudly at parse."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

import omli.connections.schema as schema

EXAMPLE = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "connection_registry.example.yaml"


def test_unknown_kind_is_rejected_at_parse():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    bad = copy.deepcopy(data)
    bad["connections"][0]["target"]["kind"] = "not-a-kind"
    with pytest.raises(ValueError, match="target.kind must be one of"):
        schema.parse_registry(bad)
