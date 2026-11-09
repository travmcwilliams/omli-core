"""Probe: detector attention as WorkResult payload envelopes.

Shows that tool and repo/CI detector metadata ride the artifact chain without:
  - adding an ARTIFACT_KINDS entry
  - using governance Finding (would require a fake model_id)
  - mutating any artifact after write

Status is intentionally absent from the payload as mutable truth: a status change would be an
append-only event.
"""

from __future__ import annotations

import pytest

from omli.artifacts.schema import ARTIFACT_KINDS, new_artifact, succeed
from omli.artifacts.provenance import Provenance
from omli.artifacts.store import ArtifactStore
from omli.governance.findings import Finding as GovernanceFinding


# The envelope keys this test proposes for a detector's output (all optional at the
# Artifact level). The detectors and packs named in the samples below are made-up examples.
_ENVELOPE_KEYS = frozenset(
    {
        "role",
        "detector_id",
        "pack_id",
        "pack_version",
        "rule_id",
        "severity",
        "dedup_key",
        "subject_refs",
        "evidence",
    }
)


def _chain_to_work_result(evidence_payload: dict, *, agent: str, pack: str, ver: str):
    """CanonicalInput → Spec → DecisionRecord → WorkResult with detector envelope."""
    ci = new_artifact(
        "CanonicalInput",
        {
            "channel": "detector_probe",
            "summary": evidence_payload["rule_id"],
            "sensitivity_tags": ["internal_research"],
        },
        Provenance(
            agent=agent,
            pack_name=pack,
            pack_version=ver,
            source_refs=("probe:test_detector_finding_envelope",),
            sensitivity=("internal",),
        ),
    )
    spec = succeed(
        ci,
        "Spec",
        {
            "objective": "carry detector attention in a WorkResult payload",
            "acceptance": ["envelope fits WorkResult.payload", "no new artifact kind"],
        },
        agent=agent,
        pack_name=pack,
        pack_version=ver,
    )
    decision = succeed(
        spec,
        "DecisionRecord",
        {
            "choice": "workresult_envelope",
            "rejected": ["governance_finding", "new_artifact_kind"],
        },
        agent=agent,
        pack_name=pack,
        pack_version=ver,
    )
    work = succeed(
        decision,
        "WorkResult",
        evidence_payload,
        agent=agent,
        pack_name=pack,
        pack_version=ver,
    )
    return ci, spec, decision, work


def _quality_envelope() -> dict:
    return {
        "role": "detector_attention",
        "detector_id": "data_quality_sentinel",
        "pack_id": "data_quality_sensor",
        "pack_version": "0.0.0-probe",
        "rule_id": "sentinel_value_detected",
        "severity": "high",
        "dedup_key": "orders.customer_id::-999",
        "subject_refs": ["dataset:orders", "column:customer_id"],
        "evidence": {
            "column": "customer_id",
            "sentinel_value": -999,
            "affected_row_count": 1284,
            "sample_row_ids": ["r17", "r88", "r401"],
            "stage": "input_contract",
        },
    }


def _repo_ci_envelope() -> dict:
    return {
        "role": "detector_attention",
        "detector_id": "repo_ci_sensor",
        "pack_id": "repo_ci_sensor",
        "pack_version": "0.1.0",
        "rule_id": "task_flapping",
        "severity": "normal",
        "dedup_key": "omli-ci::test",
        "subject_refs": [
            "cicd.pipeline.name:omli-ci",
            "cicd.pipeline.task.name:test",
            "cicd.pipeline.task.run.id:run-probe-1",
        ],
        "evidence": {
            "pipeline_name": "omli-ci",
            "task_name": "test",
            "window_runs": 6,
            "alternations": 3,
            "results": ["success", "failure", "success", "failure", "success", "failure"],
        },
    }


def test_artifact_kinds_are_the_five_chain_kinds():
    """Guardrail: the detector envelope needs no sixth artifact kind."""
    assert ARTIFACT_KINDS == (
        "CanonicalInput",
        "Spec",
        "DecisionRecord",
        "WorkResult",
        "OutcomeRecord",
    )


def test_quality_detector_envelope_fits_a_work_result():
    ci, spec, decision, work = _chain_to_work_result(
        _quality_envelope(),
        agent="ProbeDetector",
        pack="data_quality_sensor",
        ver="0.0.0-probe",
    )
    assert work.kind == "WorkResult"
    assert work.payload["role"] == "detector_attention"
    assert work.payload["evidence"]["sentinel_value"] == -999
    assert work.payload["evidence"]["affected_row_count"] == 1284
    assert set(work.payload.keys()) <= _ENVELOPE_KEYS
    # status must not be smuggled in as mutable truth on the artifact
    assert "status" not in work.payload
    assert work.provenance.predecessor_ids == (decision.id,)
    assert decision.provenance.predecessor_ids == (spec.id,)
    assert spec.provenance.predecessor_ids == (ci.id,)


def test_repo_ci_detector_envelope_fits_a_work_result():
    *_, work = _chain_to_work_result(
        _repo_ci_envelope(),
        agent="ProbeDetector",
        pack="repo_ci_sensor",
        ver="0.1.0",
    )
    assert work.payload["rule_id"] == "task_flapping"
    assert work.payload["evidence"]["alternations"] == 3
    assert "cicd.pipeline.name:omli-ci" in work.payload["subject_refs"]
    assert "status" not in work.payload


def test_both_envelopes_roundtrip_append_only_store(tmp_path):
    store = ArtifactStore(path=tmp_path / "artifacts.jsonl")
    for payload, pack, ver in (
        (_quality_envelope(), "data_quality_sensor", "0.0.0-probe"),
        (_repo_ci_envelope(), "repo_ci_sensor", "0.1.0"),
    ):
        for art in _chain_to_work_result(payload, agent="ProbeDetector", pack=pack, ver=ver):
            store.write(art)

    reloaded = ArtifactStore.from_log(tmp_path / "artifacts.jsonl")
    works = reloaded.by_kind("WorkResult")
    assert len(works) == 2
    roles = {w.payload["role"] for w in works}
    assert roles == {"detector_attention"}
    rules = {w.payload["rule_id"] for w in works}
    assert rules == {"sentinel_value_detected", "task_flapping"}

    # append-only: re-writing an existing id must fail (same id, new write)
    with pytest.raises(ValueError, match="already written"):
        store.write(works[0])


def test_governance_finding_refuses_a_detector_kind_and_any_status_but_proposed():
    """Negative control: governance Finding is the wrong speech act for detectors."""
    evidence = {"column": "customer_id", "sentinel_value": -999}
    with pytest.raises(ValueError, match="finding kind"):
        GovernanceFinding(kind="detector", model_id="not-a-model", registry_version="none",
                          summary="sentinel on customer_id", evidence=evidence)
    for status in ("adopted", "rejected", "resolved", "", "Proposed"):
        with pytest.raises(ValueError, match="proposed"):
            GovernanceFinding(kind="model_evaluation", model_id="m", registry_version="none",
                              summary="s", evidence=evidence, status=status)
    assert GovernanceFinding(kind="model_evaluation", model_id="m", registry_version="none",
                             summary="s", evidence=evidence).status == "proposed"
