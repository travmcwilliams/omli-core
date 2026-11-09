"""Router: resolve model_id -> entry -> adapter; validate requirements at load (fail loud)."""

from pathlib import Path

import pytest
import yaml

from omli.core.router import Router, validate_packs
from omli.models.registry import load_registry
from omli.packs.loader import load_pack
from omli.packs.schema import Escalation, ModelRequirements, ModelSelection, Pack

ROOT = Path(__file__).resolve().parent.parent
REG = load_registry(ROOT / "omli" / "defaults" / "model_registry.example.yaml")
DECISION_PACK = ROOT / "packs" / "decision_pack.model_block.example.yaml"
PLAIN_PACK = ROOT / "omli" / "defaults" / "example_pack.yaml"


def _pack(**kw) -> Pack:
    base = dict(name="t", version="1", identity="i", prompt_template="{input}")
    base.update(kw)
    return Pack(**base)


def test_example_decision_pack_resolves():
    pack = load_pack(DECISION_PACK)
    resolved = Router(REG).resolve(pack)
    assert resolved.entry.model_id == "example-local-4b"
    assert resolved.adapter == "ollama"
    assert resolved.escalation_entry is not None
    assert resolved.escalation_entry.model_id == "hosted-burst"
    # Shipped example registry is staging; both targets resolvable.
    assert resolved.entry.status in ("staging", "production")
    assert resolved.escalation_entry.status in ("staging", "production")


def test_draft_model_is_rejected_by_status_gate():
    """Draft is catalog-only; Router.resolve must not run it."""
    data = yaml.safe_load((ROOT / "omli" / "defaults" / "model_registry.example.yaml").read_text(
        encoding="utf-8"))
    for model in data["models"]:
        if model["model_id"] == "example-local-4b":
            model["status"] = "draft"
    from omli.models.registry import parse_registry
    reg = parse_registry(data)
    pack = _pack(model_selection=ModelSelection(model_id="example-local-4b"))
    with pytest.raises(ValueError, match=r"status 'draft'.*staging"):
        Router(reg).resolve(pack)


def test_deprecated_model_is_rejected_by_status_gate():
    data = yaml.safe_load((ROOT / "omli" / "defaults" / "model_registry.example.yaml").read_text(
        encoding="utf-8"))
    for model in data["models"]:
        if model["model_id"] == "example-local-4b":
            model["status"] = "deprecated"
    from omli.models.registry import parse_registry
    reg = parse_registry(data)
    pack = _pack(model_selection=ModelSelection(model_id="example-local-4b"))
    with pytest.raises(ValueError, match=r"status 'deprecated'"):
        Router(reg).resolve(pack)


def test_escalation_target_is_status_gated_too():
    data = yaml.safe_load((ROOT / "omli" / "defaults" / "model_registry.example.yaml").read_text(
        encoding="utf-8"))
    for model in data["models"]:
        if model["model_id"] == "hosted-burst":
            model["status"] = "draft"
    from omli.models.registry import parse_registry
    reg = parse_registry(data)
    pack = _pack(
        model_selection=ModelSelection(
            model_id="example-local-4b",
            escalation=Escalation(model_id="hosted-burst", trigger="gate_rejection"),
        )
    )
    with pytest.raises(ValueError, match=r"escalation model.*status 'draft'"):
        Router(reg).resolve(pack)


def test_requirements_not_met_fails_loud():
    # The local model has no vision; requiring it must fail at resolve time.
    pack = _pack(
        model_selection=ModelSelection(model_id="example-local-4b"),
        model_requirements=ModelRequirements(vision=True),
    )
    with pytest.raises(ValueError, match="does not meet model_requirements"):
        Router(REG).resolve(pack)


def test_unknown_selected_model_fails():
    pack = _pack(model_selection=ModelSelection(model_id="nope"))
    with pytest.raises(ValueError, match="not in registry"):
        Router(REG).resolve(pack)


def test_unknown_escalation_model_fails():
    pack = _pack(
        model_selection=ModelSelection(
            model_id="example-local-4b",
            escalation=Escalation(model_id="nope", trigger="gate_rejection"),
        )
    )
    with pytest.raises(ValueError, match="escalation model_id 'nope' is not in registry"):
        Router(REG).resolve(pack)


def test_validate_packs_skips_packs_without_selection():
    plain = load_pack(PLAIN_PACK)  # the original example pack has no model block
    assert plain.model_selection is None
    validate_packs(REG, [plain, load_pack(DECISION_PACK)])  # must not raise


def test_requirements_without_selection_rejected_by_loader_and_validator(tmp_path):
    # The loader guards this for pack files, and validate_packs guards it for in-code packs.
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump({"name": "t", "version": "1", "identity": "i",
                                    "prompt_template": "{input}",
                                    "model_requirements": {"vision": True}}), encoding="utf-8")
    with pytest.raises(ValueError, match="without model_selection"):
        load_pack(path)
    pack = _pack(model_requirements=ModelRequirements(vision=True))
    with pytest.raises(ValueError, match="without model_selection"):
        validate_packs(REG, [pack])
