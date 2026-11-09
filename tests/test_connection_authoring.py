"""Authoring surface: canonical validation, config-derived dry-run, draft-only writes."""

import copy

import pytest

from omli.connections import authoring


def _tpl():
    return copy.deepcopy(authoring.template())


def test_template_is_valid():
    res = authoring.validate(_tpl())
    assert res["ok"] is True
    assert res["connection_ids"] == ["example_repo", "web_research", "sandbox_egress", "team_slack",
                                     "team_email", "team_graph_mail", "data_api", "model_api"]


def test_validate_surfaces_errors():
    data = _tpl()
    data["connections"][0]["target"]["credential_ref"] = "inline-token"
    res = authoring.validate(data)
    assert res["ok"] is False
    assert any("secret://" in e for e in res["errors"])


def test_dry_run_reports_an_allowed_read_and_a_denied_probe():
    res = authoring.dry_run(_tpl())
    assert res["ok"] is True
    decisions = {r["decision"] for r in res["results"]}
    assert "allow" in decisions
    assert "deny" in decisions  # the forbidden-path probe
    # a read on a selected read row is allowed
    assert any(r["decision"] == "allow" and r["operation"] == "read" for r in res["results"])


def test_dry_run_on_invalid_registry_reports_errors():
    data = _tpl()
    data["connections"][0]["approval"]["auto_merge"] = True
    res = authoring.dry_run(data)
    assert res["ok"] is False and res["results"] == []


def test_save_draft_forces_draft_and_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(authoring, "DRAFTS_DIR", tmp_path)
    data = _tpl()
    data["metadata"]["status"] = "production"  # attempt to author production from the form
    res = authoring.save_draft(data)
    assert res["ok"] is True
    assert res["status"] == "draft"  # forced
    written = list(tmp_path.glob("*.draft.yaml"))
    assert len(written) == 1
    import yaml
    saved = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert saved["metadata"]["status"] == "draft"


def test_save_draft_refuses_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(authoring, "DRAFTS_DIR", tmp_path)
    data = _tpl()
    data["connections"][0]["target"]["credential_ref"] = "inline-token"
    res = authoring.save_draft(data)
    assert res["ok"] is False
    assert list(tmp_path.glob("*.yaml")) == []
