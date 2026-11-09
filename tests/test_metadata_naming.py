"""Shipped metadata names the project, not an organization."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_registries_are_authored_by_omli():
    paths = sorted((ROOT / "omli" / "defaults").glob("*registry*.yaml")) + [ROOT / "tests" / "data" / "registry.yaml"]
    assert paths
    for path in paths:
        meta = yaml.safe_load(path.read_text(encoding="utf-8"))["metadata"]
        assert meta["authored_by"] == "omli", path.name
