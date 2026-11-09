"""Governance Findings — append-only evidence; promotion stays human (Primitive 5).

An evaluation report or shadow-replay result is recorded as an immutable `Finding` with
`status="proposed"`. Recording a finding changes nothing operationally: it is evidence a
human weighs to decide a promotion. Nothing here adopts a model or edits the registry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from omli.frozen import freeze, thaw


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


FINDING_KINDS = ("model_evaluation", "shadow_replay")


@dataclass(frozen=True)
class Finding:
    """An immutable governance record. The constructor accepts only 'proposed' as its status
    and only `FINDING_KINDS` as its kind."""

    kind: str
    model_id: str
    registry_version: str
    summary: str
    evidence: Mapping[str, Any]
    status: str = "proposed"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        if self.kind not in FINDING_KINDS:
            raise ValueError(f"finding kind must be one of {FINDING_KINDS}")
        if self.status != "proposed":
            raise ValueError("a finding is always written as 'proposed'; adoption is a human act")
        # Deep-frozen and copied, like an artifact payload: nested evidence cannot change.
        object.__setattr__(self, "evidence", freeze(dict(self.evidence)))


class FindingStore:
    """Append-only store of findings. No update/delete — findings are immutable evidence."""

    def __init__(self) -> None:
        self._items: list[Finding] = []

    def record(self, finding: Finding) -> Finding:
        self._items.append(finding)
        return finding

    def all(self) -> tuple[Finding, ...]:
        return tuple(self._items)

    def by_model(self, model_id: str) -> tuple[Finding, ...]:
        return tuple(f for f in self._items if f.model_id == model_id)


def finding_to_row(finding: Finding) -> dict:
    """The append-only JSONL row shape for a Finding — the line `append_finding` writes.

    One place defines the on-disk shape, so every producer writes the same row.
    """
    return {
        "type": "finding",
        "id": finding.id,
        "kind": finding.kind,
        "model_id": finding.model_id,
        "registry_version": finding.registry_version,
        "summary": finding.summary,
        "evidence": thaw(finding.evidence),
        "status": finding.status,
        "created_at": finding.created_at,
    }


def append_finding(finding: Finding, path) -> None:
    """Append a Finding as one JSONL line (append-only — findings are immutable evidence).

    Recording changes nothing operationally; it is evidence a human weighs. Nothing here adopts
    a model or edits a registry (Primitive 5).
    """
    import json
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(finding_to_row(finding), ensure_ascii=False, default=str) + "\n")


def finding_from_evaluation(report, *, registry_version: str = "") -> Finding:
    """Build a 'proposed' Finding from an evaluation report: any object with the attributes read
    below. No report type ships in this package."""
    verdict = "PASS" if report.passed else "FAIL"
    return Finding(
        kind="model_evaluation",
        model_id=report.model_id,
        registry_version=registry_version,
        summary=f"{report.role}/{report.metric}: mean {report.mean_score:.3f} -> {verdict}",
        evidence={
            "role": report.role,
            "metric": report.metric,
            "mean_score": report.mean_score,
            "passed": report.passed,
            "served_model_id": report.served_model_id,
            "per_case": [{"id": c.id, "score": c.score} for c in report.per_case],
            "ran_at": report.ran_at,
        },
    )


def finding_from_shadow(shadow, *, registry_version: str = "") -> Finding:
    """Build a 'proposed' Finding from a shadow-replay result: any object with the attributes
    read below. No replay tool ships in this package."""
    return Finding(
        kind="shadow_replay",
        model_id=shadow.model_id,
        registry_version=registry_version,
        summary=(
            f"shadow replay n={shadow.sample_size}: agreement {shadow.agreement_rate:.3f}, "
            f"gate_rejection {shadow.gate_rejection_rate:.3f}"
        ),
        evidence={
            "sample_size": shadow.sample_size,
            "agreement_rate": shadow.agreement_rate,
            "gate_rejection_rate": shadow.gate_rejection_rate,
            "ran_at": shadow.ran_at,
        },
    )
