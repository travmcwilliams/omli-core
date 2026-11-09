"""Reflector — the intended writer of learned memory (Primitive 5).

No agent in this package other than the Reflector writes memory, and none rewrites its own
identity or track record. This is a convention of the call graph: `MemoryStore.append` is a
public method, so code in the same process could call it. The store does refuse a lesson that
arrives already approved. The Reflector only *suggests*: every lesson it records is unapproved,
and approval is a separate, human, append-only act; nothing here approves or tunes itself.

`MemoryStore` is append-only and **durable**: pass a `path` and lessons persist to JSONL (one
line per lesson, plus separate decision markers so a decision never mutates an existing line —
Primitive 4). With no path it is a pure in-memory store.

How a decision is read back:

* Every lesson and consolidation id is unique. A second record with an id already in the store
  is refused by `append` and makes loading the log fail, so a new record can never borrow an
  earlier approval.
* Approvals and rejections are ordered events. For each id the LATEST decision wins, so a
  rejection after an approval withdraws it, and a later approval restores it.
* An approval records a hash of the text it approved. If the record's text no longer matches
  (an edited log line), the approval does not apply.
* A consolidation may supersede only lessons whose latest decision is an approval, and it stays
  in effect only while that is still true for every one of them. A model-written
  consolidation is in effect only while its own latest decision is also an approval of its
  current text. A consolidation of either mode whose latest decision is a rejection is not in
  effect, and `reject()` accepts consolidation ids as well as lesson ids.

The log is not tamper-evident by itself: an actor who can write it can also append decision
markers. Use `omli.integrity` to seal it when that matters.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Tuple

APPROVED = "approved"
REJECTED = "rejected"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_hash(text: str) -> str:
    """The fingerprint an approval binds to: sha256 of the exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Lesson:
    """An immutable, unapproved-by-default suggestion derived from an outcome.

    `project` / `sprint` / `category` / `tags` are optional free-form labels for grouping
    lessons and querying them back. They default empty, so `text` and
    `source_artifact_ids` alone build a lesson. `approved` is never trusted on input: the
    store derives it from decision markers.
    """

    text: str
    source_artifact_ids: Tuple[str, ...]
    created_at: str = field(default_factory=_utc_now_iso)
    approved: bool = False
    project: str = ""
    sprint: str = ""
    category: str = ""
    tags: Tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "type": "lesson", "id": self.id, "text": self.text,
            "source_artifact_ids": list(self.source_artifact_ids),
            "created_at": self.created_at, "approved": self.approved,
            "project": self.project, "sprint": self.sprint,
            "category": self.category, "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lesson":
        return cls(
            text=d.get("text", ""),
            source_artifact_ids=tuple(d.get("source_artifact_ids", []) or []),
            created_at=d.get("created_at", _utc_now_iso()),
            approved=bool(d.get("approved", False)),
            project=d.get("project", ""), sprint=d.get("sprint", ""),
            category=d.get("category", ""), tags=tuple(d.get("tags", []) or []),
            id=d.get("id") or uuid.uuid4().hex,
        )


@dataclass(frozen=True)
class Consolidation:
    """An append-only successor that distills several APPROVED lessons into one.

    Memory compaction, Primitive-4 style: the superseded lessons are never rewritten or
    removed — this record links them (`supersedes`) and the `active()` view prefers it.
    `mode` records how the text was produced and decides activation policy:
    ``deterministic`` text is rendered by the store from the superseded lessons
    (`render_consolidation`, no model call, no caller text), takes effect immediately and stays
    human-vetoable; ``model`` (LLM-written) is a suggestion like any lesson and only takes
    effect once approved (Primitive 5 — no auto-tuning).
    """

    text: str
    supersedes: Tuple[str, ...]
    mode: str = "deterministic"
    actor: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    project: str = ""
    category: str = ""
    tags: Tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "type": "consolidation", "id": self.id, "text": self.text,
            "supersedes": list(self.supersedes), "mode": self.mode,
            "actor": self.actor, "created_at": self.created_at,
            "project": self.project, "category": self.category,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Consolidation":
        return cls(
            text=d.get("text", ""),
            supersedes=tuple(d.get("supersedes", []) or []),
            mode=d.get("mode", "deterministic"),
            actor=d.get("actor", ""),
            created_at=d.get("created_at", _utc_now_iso()),
            project=d.get("project", ""), category=d.get("category", ""),
            tags=tuple(d.get("tags", []) or []),
            id=d.get("id") or uuid.uuid4().hex,
        )


def render_consolidation(lessons: Iterable[Lesson]) -> str:
    """The fixed template for a deterministic consolidation: each lesson's text on its own line.

    It adds no words. Because ids are unique and an approval binds to the approved text, the
    result holds only text whose latest decision is a human approval.
    """
    return "\n".join(lesson.text for lesson in lessons)


_STR, _BOOL, _STR_LIST = "a string", "true or false", "a list of strings"

#: The type each field of a log record must have when it is present, by record type. A record
#: with no ``type`` (or any type not named here) is a lesson. Absent fields take their defaults.
_FIELD_TYPES: dict[str, dict[str, str]] = {
    "lesson": {"id": _STR, "text": _STR, "source_artifact_ids": _STR_LIST, "created_at": _STR,
               "approved": _BOOL, "project": _STR, "sprint": _STR, "category": _STR,
               "tags": _STR_LIST},
    "consolidation": {"id": _STR, "text": _STR, "supersedes": _STR_LIST, "mode": _STR,
                      "actor": _STR, "created_at": _STR, "project": _STR, "category": _STR,
                      "tags": _STR_LIST},
    "approval": {"lesson_id": _STR, "text_sha256": _STR, "actor": _STR},
    "rejection": {"lesson_id": _STR, "text_sha256": _STR, "actor": _STR},
    "revocation": {"consolidation_id": _STR},
}


def _has_type(value: object, kind: str) -> bool:
    if kind == _STR:
        return isinstance(value, str)
    if kind == _BOOL:
        return isinstance(value, bool)
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _record_problem(rec: object) -> Optional[str]:
    """Why a parsed log line is not a usable record, or None when it is. The message names the
    field and the expected type, never the value."""
    if not isinstance(rec, dict):
        return "is not a JSON object"
    kind = rec.get("type", "lesson")
    if not isinstance(kind, str):
        return "has a 'type' that is not a string"
    for name, expected in _FIELD_TYPES.get(kind, _FIELD_TYPES["lesson"]).items():
        if name in rec and not _has_type(rec[name], expected):
            return f"has a '{name}' that is not {expected}"
    return None


class MemoryStore:
    """Append-only store of learned lessons. No deletes, no in-place edits.

    Durable when constructed with a `path`: every `append` writes a JSONL line and every
    decision writes a separate marker, so the file is replayable and nothing is ever
    rewritten. In-memory only when `path` is None. Consolidations and their revocations (the
    human veto) are further marker types in the same log.
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else None
        self._lessons: list[Lesson] = []
        # id -> (latest decision, hash of the text it was made on, actor)
        self._decisions: dict[str, tuple[str, str, str]] = {}
        self._consolidations: list[Consolidation] = []
        self._revoked_ids: set[str] = set()
        if self._path and self._path.exists():
            self._load()

    # -- load / persist -------------------------------------------------------
    def _load(self) -> None:
        """Replay the log. Blank lines are skipped. A line that is not valid UTF-8, is not valid
        JSON, is nested past the parser's limit, is not a JSON object, or has a field of the
        wrong type (see :data:`_FIELD_TYPES`) raises ValueError naming its line number: a log
        that cannot be read in full is never read in part."""
        for n, raw in enumerate(self._path.read_bytes().splitlines(), 1):
            problem = None
            rec = None
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                problem = "is not valid UTF-8"
                line = ""
            else:
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception as exc:  # not JSON, nested too deeply, ...
                    problem = f"cannot be parsed ({type(exc).__name__})"
                else:
                    problem = _record_problem(rec)
            if problem is not None:
                raise ValueError(f"memory log {self._path.name}: line {n} {problem}")
            kind = rec.get("type")
            if kind in ("approval", "rejection"):
                self._decisions[str(rec.get("lesson_id", ""))] = (
                    APPROVED if kind == "approval" else REJECTED,
                    str(rec.get("text_sha256", "")), str(rec.get("actor", "")))
            elif kind == "consolidation":
                cons = Consolidation.from_dict(rec)
                self._refuse_duplicate(cons.id, f"line {n}")
                self._consolidations.append(cons)
            elif kind == "revocation":
                self._revoked_ids.add(rec.get("consolidation_id", ""))
            else:  # default to a lesson record; only a decision marker can approve it
                lesson = replace(Lesson.from_dict(rec), approved=False)
                self._refuse_duplicate(lesson.id, f"line {n}")
                self._lessons.append(lesson)

    def _write(self, rec: dict) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _known(self, record_id: str) -> bool:
        return (any(l.id == record_id for l in self._lessons)
                or any(c.id == record_id for c in self._consolidations))

    def _refuse_duplicate(self, record_id: str, where: str = "") -> None:
        if self._known(record_id):
            suffix = f" ({where})" if where else ""
            raise ValueError(f"memory record id {record_id!r} is already in the store{suffix}; "
                             "ids are unique so a new record cannot inherit an earlier decision")

    def _text_of(self, record_id: str) -> Optional[str]:
        for l in self._lessons:
            if l.id == record_id:
                return l.text
        for c in self._consolidations:
            if c.id == record_id:
                return c.text
        return None

    def _is_approved(self, record_id: str) -> bool:
        """The latest decision is an approval, made on the record's current text."""
        decision = self._decisions.get(record_id)
        text = self._text_of(record_id)
        return (decision is not None and text is not None and decision[0] == APPROVED
                and decision[1] == text_hash(text))

    def _decide(self, record_id: str, decision: str, actor: str, extra: dict) -> None:
        digest = text_hash(self._text_of(record_id) or "")
        current = self._decisions.get(record_id)
        if current is not None and current[:2] == (decision, digest):
            return  # already the latest decision on this exact text
        self._decisions[record_id] = (decision, digest, actor)
        kind, stamp = ("approval", "approved_at") if decision == APPROVED else ("rejection", "rejected_at")
        self._write({"type": kind, "lesson_id": record_id, "text_sha256": digest,
                     stamp: _utc_now_iso(), "actor": actor, **extra})

    # -- write ----------------------------------------------------------------
    def append(self, lesson: Lesson) -> Lesson:
        if lesson.approved:
            raise ValueError("a lesson cannot arrive approved; approval is its own append-only act")
        self._refuse_duplicate(lesson.id)
        self._lessons.append(lesson)
        self._write(lesson.to_dict())
        return lesson

    def approve(self, lesson_id: str, *, actor: str = "human") -> bool:
        """Approve a lesson (or a model-written consolidation). Append-only marker that binds
        to the record's current text; it becomes the latest decision for that id.

        `actor` records who approved, for the audit trail. It is a recorded name, not an
        authenticated identity.
        """
        if not self._known(lesson_id):
            return False
        self._decide(lesson_id, APPROVED, actor, {})
        return True

    def reject(self, lesson_id: str, *, actor: str = "human", reason: str = "") -> bool:
        """Decline a lesson or a consolidation. Append-only rejection marker; it becomes the
        latest decision for that id, so a rejection after an approval withdraws that approval.
        Rejecting a lesson takes every consolidation over it out of effect, whatever its mode;
        rejecting a consolidation takes that consolidation out of effect."""
        if not self._known(lesson_id):
            return False
        self._decide(lesson_id, REJECTED, actor, {"reason": reason})
        return True

    def decision(self, record_id: str) -> Optional[str]:
        """The latest decision recorded for an id ("approved" / "rejected"), or None."""
        found = self._decisions.get(record_id)
        return found[0] if found else None

    def append_consolidation(self, cons: Consolidation) -> Consolidation:
        """Record a consolidation (`Reflector.consolidate` calls this).

        Every superseded id must be a lesson whose latest decision is an approval. A
        deterministic consolidation takes no text: the store renders it here, so it cannot
        bring unapproved text into effect. A model-written one must carry text and stays
        inactive until it is approved itself. Returns the record as stored.
        """
        if not cons.supersedes:
            raise ValueError("a consolidation must supersede at least one lesson")
        if cons.mode not in ("deterministic", "model"):
            raise ValueError(f"unknown consolidation mode {cons.mode!r}")
        self._refuse_duplicate(cons.id)
        approved = {l.id for l in self.approved()}
        missing = [i for i in cons.supersedes if i not in approved]
        if missing:
            raise ValueError(f"consolidation may only supersede approved lessons; "
                             f"not approved: {missing}")
        if cons.mode == "deterministic":
            if cons.text:
                raise ValueError("a deterministic consolidation takes no text; the store renders it")
            cons = replace(cons, text=self._rendered(cons.supersedes))
        elif not cons.text:
            raise ValueError("a model-written consolidation must carry text")
        self._consolidations.append(cons)
        self._write(cons.to_dict())
        return cons

    def revoke(self, consolidation_id: str, *, actor: str = "human",
               reason: str = "") -> bool:
        """The human veto: append a revocation marker. The superseded lessons simply
        become active again — nothing was ever removed, so nothing is restored."""
        if not any(c.id == consolidation_id for c in self._consolidations):
            return False
        if consolidation_id not in self._revoked_ids:
            self._revoked_ids.add(consolidation_id)
            self._write({"type": "revocation", "consolidation_id": consolidation_id,
                         "revoked_at": _utc_now_iso(), "actor": actor,
                         "reason": reason})
        return True

    # -- read -----------------------------------------------------------------
    def _rendered(self, lesson_ids: Tuple[str, ...]) -> Optional[str]:
        """The template over these ids in order, or None unless all are approved lessons."""
        approved = {l.id: l for l in self.approved()}
        ids = tuple(dict.fromkeys(lesson_ids))
        if not ids or any(i not in approved for i in ids):
            return None
        return render_consolidation(approved[i] for i in ids)

    def all(self) -> tuple[Lesson, ...]:
        return tuple(replace(l, approved=self._is_approved(l.id)) for l in self._lessons)

    def approved(self) -> tuple[Lesson, ...]:
        return tuple(l for l in self.all() if l.approved)

    def get(self, lesson_id: str) -> Optional[Lesson]:
        """The lesson with this id, with `approved` derived from the store, or None."""
        return next((l for l in self.all() if l.id == lesson_id), None)

    def pending(self) -> tuple[Lesson, ...]:
        """The review queue: lessons with no decision on their current text."""
        out = []
        for l in self.all():
            found = self._decisions.get(l.id)
            if found is None or found[1] != text_hash(l.text):
                out.append(l)
        return tuple(out)

    def consolidations(self) -> tuple[Consolidation, ...]:
        return tuple(self._consolidations)

    def is_revoked(self, consolidation_id: str) -> bool:
        return consolidation_id in self._revoked_ids

    def _is_live(self, c: Consolidation, approved_lessons: set) -> bool:
        if c.id in self._revoked_ids or self.decision(c.id) == REJECTED:
            return False
        if not c.supersedes or any(i not in approved_lessons for i in c.supersedes):
            return False
        if c.mode == "deterministic":
            return c.text == self._rendered(c.supersedes)
        if c.mode == "model":
            return self._is_approved(c.id)
        return False

    def live_consolidations(self) -> tuple[Consolidation, ...]:
        """Consolidations currently in effect. Each must be not vetoed, not rejected, and over
        lessons whose latest decision is still an approval of their current text. Beyond that,
        a deterministic one must still match the template over those lessons, and a
        model-written one must have an approval of its own current text as its latest decision.
        Rechecked on every read, so a later rejection, or an edited log line (a lesson, a
        consolidation, or both), takes it out of effect instead of putting other text in."""
        approved_lessons = {l.id for l in self.approved()}
        return tuple(c for c in self._consolidations if self._is_live(c, approved_lessons))

    def superseded_ids(self) -> set[str]:
        out: set[str] = set()
        for c in self.live_consolidations():
            out.update(c.supersedes)
        return out

    def active(self) -> tuple:
        """What retrieval should read: approved lessons not superseded by a live
        consolidation, plus the live consolidations themselves."""
        superseded = self.superseded_ids()
        lessons = tuple(l for l in self.approved() if l.id not in superseded)
        return lessons + self.live_consolidations()

    def by_project(self, project: str) -> tuple[Lesson, ...]:
        return tuple(l for l in self.all() if l.project == project)

    def has_text(self, text: str) -> bool:
        """Idempotency helper for seeding: is a lesson with this exact text present?"""
        return any(l.text == text for l in self._lessons)


class Reflector:
    """Records lessons (suggestions) that a caller derived from outcomes. It reads no outcomes
    itself, and it is the only memory writer in this package."""

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def reflect(self, text: str, *, source_artifact_ids: Tuple[str, ...],
                project: str = "", sprint: str = "", category: str = "",
                tags: Tuple[str, ...] = ()) -> Lesson:
        """Record an unapproved lesson linked to the outcomes that prompted it."""
        lesson = Lesson(
            text=text,
            source_artifact_ids=tuple(source_artifact_ids),
            approved=False,
            project=project, sprint=sprint, category=category, tags=tuple(tags),
        )
        return self._memory.append(lesson)

    def consolidate(self, text: Optional[str] = None, *, supersedes: Tuple[str, ...],
                    mode: str = "deterministic", actor: str = "",
                    project: str = "", category: str = "",
                    tags: Tuple[str, ...] = ()) -> Consolidation:
        """Compact approved lessons into one successor record (memory compaction).

        Deterministic consolidations refuse `text`, because the store renders it from the
        lessons; they take effect immediately but stay vetoable (`MemoryStore.revoke`).
        Model-written ones must pass `text` and are suggestions that need approval.
        """
        return self._memory.append_consolidation(Consolidation(
            text=text or "", supersedes=tuple(supersedes), mode=mode, actor=actor,
            project=project, category=category, tags=tuple(tags),
        ))
