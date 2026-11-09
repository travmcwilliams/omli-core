"""The registries in this repository hold no draft and no production models.

Router.resolve refusing draft and deprecated entries is tested in test_router_validation.py.
This module checks the data side: the example registry that ships in the package and the test
fixture registry are both at staging, and every entry in them is staging or deprecated.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omli.models.registry import load_registry, parse_registry

ROOT = Path(__file__).resolve().parents[1]
MODEL_REGISTRIES = [ROOT / "omli" / "defaults" / "model_registry.example.yaml",
                    ROOT / "tests" / "data" / "registry.yaml"]


def test_every_model_registry_in_the_repo_loads():
    for path in MODEL_REGISTRIES:
        assert path.is_file(), path
        reg = load_registry(path)
        assert reg.models, path


def test_no_draft_or_production_entries_in_the_repo_registries():
    """Zero draft model entries, and zero production entries either."""
    for path in MODEL_REGISTRIES:
        reg = load_registry(path)
        for entry in reg.models:
            assert entry.status != "draft", f"{path.name}: {entry.model_id} is draft"
            assert entry.status != "production", (
                f"{path.name}: {entry.model_id} reached production "
                f"(production is a human act)"
            )
            assert entry.status in ("staging", "deprecated"), (
                f"{path.name}: {entry.model_id} status={entry.status!r}"
            )


def test_the_repo_registries_are_themselves_staging_not_production():
    for path in MODEL_REGISTRIES:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["metadata"]["status"] in ("staging", "deprecated"), path.name
        assert data["metadata"]["status"] != "production", path.name
        # The raw data also passes the loader's validation.
        parse_registry(data)
