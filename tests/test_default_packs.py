"""The example packs ship inside the package; a deployment's own packs come from elsewhere."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omli
from omli.connections import authoring
from omli.connections import __main__ as dry_run
from omli.connections.reader import default_registry_path
from omli.connections.schema import load_registry as load_connections
from omli.defaults import default_pack_path
from omli.models.registry import load_registry as load_models
from omli.packs.loader import load_pack
from omli.sensitivity import load_policy

ROOT = Path(__file__).resolve().parent.parent
LOADERS = {
    "connection_registry.example.yaml": load_connections,
    "model_registry.example.yaml": load_models,
    "sensitivity_policy.example.yaml": load_policy,
    "example_pack.yaml": load_pack,
}


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv("OMLI_HOME", raising=False)
    monkeypatch.delenv("OMLI_CONNECTION_REGISTRY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("name", sorted(LOADERS))
def test_default_pack_ships_inside_the_package_and_loads(name):
    path = default_pack_path(name)
    assert path.resolve().is_relative_to(Path(omli.__file__).resolve().parent)
    assert LOADERS[name](path)


def test_missing_default_pack_fails_loud():
    with pytest.raises(FileNotFoundError):
        default_pack_path("no_such_pack.yaml")


def test_pyproject_declares_the_default_packs_as_package_data():
    assert '"omli.defaults" = ["*.yaml"]' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_connection_defaults_read_the_shipped_example(clean):
    shipped = default_pack_path("connection_registry.example.yaml")
    assert default_registry_path() == shipped
    assert authoring.template() == yaml.safe_load(shipped.read_text(encoding="utf-8"))


def test_dry_run_with_no_arguments_uses_the_shipped_example(clean, capsys):
    assert dry_run.main([]) == 0
    assert "connection registry:" in capsys.readouterr().out


def test_explicit_variable_beats_every_other_registry(clean, monkeypatch):
    own = clean / "own.yaml"
    home_registry = clean / "packs" / "connection_registry.yaml"
    home_registry.parent.mkdir()
    home_registry.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("OMLI_CONNECTION_REGISTRY", str(own))
    monkeypatch.setenv("OMLI_HOME", str(clean))
    assert default_registry_path() == own


def test_deployment_registry_under_omli_home_is_used_when_present(clean, monkeypatch):
    home = clean / "home"
    monkeypatch.setenv("OMLI_HOME", str(home))
    assert default_registry_path() == default_pack_path("connection_registry.example.yaml")
    own = home / "packs" / "connection_registry.yaml"
    own.parent.mkdir(parents=True)
    own.write_text("placeholder", encoding="utf-8")
    assert default_registry_path() == own


def test_registry_planted_in_the_current_directory_is_ignored(clean):
    planted = clean / "packs" / "connection_registry.yaml"
    planted.parent.mkdir()
    planted.write_text("placeholder", encoding="utf-8")
    assert default_registry_path() == default_pack_path("connection_registry.example.yaml")
