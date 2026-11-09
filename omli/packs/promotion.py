"""Lesson promotion — an APPROVED lesson drafts a NEW pack version, capped at staging.

The closing arc of the learning loop: the Reflector
*suggests* (unapproved lessons), a reviewer *approves* (append-only marker in the
`MemoryStore`, actor stamped), and this module turns a lesson the store reports as approved
into a proposed successor pack version. Four invariants keep it primitive-safe:

- **Approval is read from the store.** The caller passes the store and a lesson id, never a
  lesson object whose `approved` flag it could set itself.

- **Never edits the published pack.** The draft is a NEW file under ``packs/staging/``
  (or a caller-supplied dir); the base pack stays byte-identical (immutable per version).
- **Staging cap, structurally.** This module can only produce ``*.staging.yaml`` drafts
  with ``promotion.stage: staging`` stamped inside. Adopting a draft into production —
  replacing the file agents actually load — remains a human, reviewed-commit act.
- **Full provenance.** The draft records the lesson id, its source artifacts, who
  promoted, when, and the exact base version, so an auditor can walk staging pack →
  approval marker → lesson → the outcomes that prompted it.

The delta itself is generic (zero domain strings): the lesson's text is appended to the
pack's ``prompt_template`` as adopted guidance — a real, observable behavior change for
any agent that loads the draft — and the version gets a patch bump.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from omli.agents.reflector import MemoryStore
from omli.home import omli_home
from omli.packs.loader import load_pack


def bump_patch(version: str) -> str:
    """'0.1.0' -> '0.1.1'; anything non-semver gets '.1' appended (still a new version)."""
    parts = version.strip().split(".")
    if parts and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    return f"{version}.1"


def draft_staging_pack(
    base_pack_path: str | Path,
    memory: MemoryStore,
    lesson_id: str,
    *,
    promoted_by: str,
    out_dir: str | Path | None = None,
) -> Path:
    """Write a staging draft of ``base_pack_path`` that adopts the lesson ``lesson_id`` from
    ``memory``. Returns the path.

    Fails closed: the store itself must hold an approval as the latest decision on the
    lesson's current text (a caller cannot assert approval by passing a flag); the draft must
    pass the pack loader; an existing draft file is never overwritten (immutable per version).
    """
    lesson = memory.get(lesson_id)
    if lesson is None:
        raise ValueError(f"lesson {lesson_id} is not in the memory store")
    if not lesson.approved:
        raise ValueError(
            f"lesson {lesson.id} is not approved in the memory store — only approved lessons may promote")

    base_path = Path(base_pack_path)
    raw = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "prompt_template" not in raw:
        raise ValueError(f"{base_path}: not an agent pack (no prompt_template)")

    from_version = str(raw.get("version", "0.0.0"))
    new_version = bump_patch(from_version)
    raw["version"] = new_version
    raw["prompt_template"] = (
        str(raw["prompt_template"]).rstrip("\n")
        + f"\n\nLearned guidance (approved lesson {lesson.id[:8]}):\n- {lesson.text}\n"
    )
    raw["promotion"] = {
        "stage": "staging",                      # the cap, stamped in the artifact
        "lesson_id": lesson.id,
        "source_artifact_ids": list(lesson.source_artifact_ids),
        "promoted_by": promoted_by,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "from_version": from_version,
        "base_pack": base_path.name,
    }

    target_dir = Path(out_dir) if out_dir else omli_home() / "packs" / "staging"
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{raw.get('name', base_path.stem)}.v{new_version}.staging.yaml"
    if out_path.exists():
        raise FileExistsError(
            f"{out_path} already exists — staging drafts are immutable; a new promotion "
            "needs a new base version or a fresh bump")
    out_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")

    try:
        load_pack(out_path)      # loud validation: an invalid draft must not survive
    except Exception:
        out_path.unlink()
        raise
    return out_path
