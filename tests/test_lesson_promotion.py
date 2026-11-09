"""Lesson promotion: an APPROVED lesson drafts a NEW staging pack
version — never editing the published pack, never reaching production autonomously."""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from omli.agents.reflector import Lesson, MemoryStore
from omli.packs.loader import load_pack
from omli.packs.promotion import draft_staging_pack

BASE = """\
name: example
version: "0.1.0"
identity: neutral demo
prompt_template: |
  Summarize the following canonical input in one line:
  {input}
required_output_fields: [text]
"""

SAMPLE = "keep each summary to a single sentence"


@pytest.fixture
def base_pack(tmp_path):
    p = tmp_path / "pack.yaml"
    p.write_text(BASE, encoding="utf-8")
    return p


def _approved(tmp_path, text=SAMPLE):
    mem = MemoryStore(tmp_path / "lessons.jsonl")
    lesson = mem.append(Lesson(text=text, source_artifact_ids=("art-9",)))
    mem.approve(lesson.id, actor="test-reviewer")
    return mem, lesson.id


def test_approved_lesson_drafts_valid_staging_pack(base_pack, tmp_path):
    mem, lesson_id = _approved(tmp_path)
    out = draft_staging_pack(base_pack, mem, lesson_id,
                             promoted_by="test-reviewer",
                             out_dir=tmp_path / "staging")
    pack = load_pack(out)                      # loud validation must pass
    assert pack.version == "0.1.1"
    assert SAMPLE in pack.prompt_template
    assert "{input}" in pack.prompt_template   # placeholder survives
    # rendered behavior observably changes
    assert SAMPLE in pack.render_prompt("hello")
    # the published pack is untouched (immutable per version)
    assert yaml.safe_load(base_pack.read_text(encoding="utf-8"))["version"] == "0.1.0"


def test_unapproved_lesson_is_refused(base_pack, tmp_path):
    mem = MemoryStore()
    lesson = mem.append(Lesson(text="never adopted", source_artifact_ids=()))
    with pytest.raises(ValueError, match="approved"):
        draft_staging_pack(base_pack, mem, lesson.id, promoted_by="x",
                           out_dir=tmp_path / "staging")
    assert not (tmp_path / "staging").exists() or list((tmp_path / "staging").iterdir()) == []


def test_a_caller_set_approved_flag_is_not_trusted(base_pack, tmp_path):
    """The approval must exist in the store. A lesson object flagged approved by its caller,
    and an id the store has never seen, are both refused."""
    mem = MemoryStore()
    lesson = mem.append(Lesson(text="flagged by the caller", source_artifact_ids=()))
    flagged = dataclasses.replace(lesson, approved=True)
    assert flagged.approved
    with pytest.raises(ValueError, match="approved"):
        draft_staging_pack(base_pack, mem, flagged.id, promoted_by="x", out_dir=tmp_path / "staging")
    with pytest.raises(ValueError, match="not in the memory store"):
        draft_staging_pack(base_pack, mem, "no-such-id", promoted_by="x", out_dir=tmp_path / "staging")


def test_a_rejection_after_approval_blocks_promotion(base_pack, tmp_path):
    mem, lesson_id = _approved(tmp_path)
    mem.reject(lesson_id)
    with pytest.raises(ValueError, match="approved"):
        draft_staging_pack(base_pack, mem, lesson_id, promoted_by="x", out_dir=tmp_path / "staging")


def test_provenance_records_the_full_chain(base_pack, tmp_path):
    mem, lesson_id = _approved(tmp_path)
    out = draft_staging_pack(base_pack, mem, lesson_id,
                             promoted_by="test-reviewer",
                             out_dir=tmp_path / "staging")
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    prov = raw["promotion"]
    assert prov["lesson_id"] == lesson_id
    assert prov["promoted_by"] == "test-reviewer"
    assert prov["stage"] == "staging"          # the cap, stamped in the artifact
    assert prov["from_version"] == "0.1.0"
    assert prov["source_artifact_ids"] == ["art-9"]


def test_staging_files_are_immutable(base_pack, tmp_path):
    mem, lesson_id = _approved(tmp_path)
    out_dir = tmp_path / "staging"
    draft_staging_pack(base_pack, mem, lesson_id, promoted_by="x", out_dir=out_dir)
    with pytest.raises(FileExistsError):
        draft_staging_pack(base_pack, mem, lesson_id, promoted_by="x", out_dir=out_dir)


def test_filename_encodes_pack_version_and_stage(base_pack, tmp_path):
    mem, lesson_id = _approved(tmp_path)
    out = draft_staging_pack(base_pack, mem, lesson_id, promoted_by="x",
                             out_dir=tmp_path / "staging")
    assert out.name == "example.v0.1.1.staging.yaml"
