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


def test_the_package_version_is_stated_once():
    """pyproject.toml and omli.__version__ name the same release, and it is this release."""
    import re

    import omli

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'(?m)^version = "([^"]+)"$', text).group(1)
    assert omli.__version__ == declared
    assert declared == "0.2.0"
