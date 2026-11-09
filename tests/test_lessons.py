"""Durable lessons memory: persists, reloads, dedups, and approves append-only."""

from omli.agents.reflector import Lesson, MemoryStore, Reflector


def test_store_without_a_path_holds_lessons_in_memory():
    mem = MemoryStore()
    r = Reflector(mem)
    r.reflect("a lesson", source_artifact_ids=("x",))
    assert len(mem.all()) == 1
    assert mem.all()[0].approved is False


def test_lessons_persist_and_reload(tmp_path):
    log = tmp_path / "lessons.jsonl"
    mem = MemoryStore(log)
    Reflector(mem).reflect("sample lesson text one",
                           source_artifact_ids=("run:1",),
                           project="omli", sprint="demo", category="bug",
                           tags=("encoding", "windows"))
    assert log.exists()

    # A fresh store over the same file sees the lesson with all context intact.
    reloaded = MemoryStore(log)
    got = reloaded.all()
    assert len(got) == 1
    assert got[0].project == "omli" and got[0].sprint == "demo"
    assert got[0].tags == ("encoding", "windows")
    assert got[0].approved is False


def test_approval_is_append_only_and_survives_reload(tmp_path):
    log = tmp_path / "lessons.jsonl"
    mem = MemoryStore(log)
    lesson = Reflector(mem).reflect("sample lesson text three", source_artifact_ids=())
    assert mem.approve(lesson.id) is True
    assert mem.approve("nonexistent") is False

    reloaded = MemoryStore(log)
    approved = reloaded.approved()
    assert len(approved) == 1 and approved[0].id == lesson.id
    # The original lesson line was never rewritten — approval is a separate marker.
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2  # one lesson + one approval


def test_has_text_supports_idempotent_seeding(tmp_path):
    log = tmp_path / "lessons.jsonl"
    mem = MemoryStore(log)
    text = "sample lesson text two"
    if not mem.has_text(text):
        mem.append(Lesson(text=text, source_artifact_ids=()))
    # Second pass must not duplicate.
    if not mem.has_text(text):
        mem.append(Lesson(text=text, source_artifact_ids=()))
    assert sum(1 for l in mem.all() if l.text == text) == 1


def test_by_project_filters(tmp_path):
    mem = MemoryStore(tmp_path / "lessons.jsonl")
    mem.append(Lesson(text="x", source_artifact_ids=(), project="omli"))
    mem.append(Lesson(text="y", source_artifact_ids=(), project="other"))
    assert {l.text for l in mem.by_project("omli")} == {"x"}


# --- review verbs — reject, pending, actor stamps ---

def test_reject_is_append_only_and_survives_reload(tmp_path):
    log = tmp_path / "lessons.jsonl"
    mem = MemoryStore(log)
    kept = mem.append(Lesson(text="keep me", source_artifact_ids=()))
    dropped = mem.append(Lesson(text="drop me", source_artifact_ids=()))
    assert mem.reject(dropped.id, actor="test-reviewer", reason="stale")
    # the original lesson line is never rewritten; a rejection marker is appended
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    import json as _json
    marker = _json.loads(lines[-1])
    assert marker["type"] == "rejection"
    assert marker["actor"] == "test-reviewer"
    # reload: rejection sticks; pending shows only the untouched lesson
    mem2 = MemoryStore(log)
    assert {l.text for l in mem2.pending()} == {"keep me"}
    assert kept.id in {l.id for l in mem2.pending()}


def test_pending_excludes_approved_and_rejected(tmp_path):
    mem = MemoryStore(tmp_path / "lessons.jsonl")
    a = mem.append(Lesson(text="a", source_artifact_ids=()))
    b = mem.append(Lesson(text="b", source_artifact_ids=()))
    c = mem.append(Lesson(text="c", source_artifact_ids=()))
    mem.approve(a.id, actor="human")
    mem.reject(b.id, actor="human")
    assert {l.id for l in mem.pending()} == {c.id}
    # approved() unchanged by the rejection verb
    assert {l.id for l in mem.approved()} == {a.id}


def test_approval_marker_carries_actor(tmp_path):
    log = tmp_path / "lessons.jsonl"
    mem = MemoryStore(log)
    lesson = mem.append(Lesson(text="x", source_artifact_ids=()))
    mem.approve(lesson.id, actor="test-reviewer")
    import json as _json
    marker = _json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert marker["type"] == "approval"
    assert marker["actor"] == "test-reviewer"


def test_approve_without_actor_records_the_approval(tmp_path):
    mem = MemoryStore(tmp_path / "lessons.jsonl")
    lesson = mem.append(Lesson(text="x", source_artifact_ids=()))
    assert mem.approve(lesson.id)          # `actor` is optional
    assert lesson.id in {l.id for l in mem.approved()}


def test_reject_unknown_lesson_returns_false(tmp_path):
    mem = MemoryStore(tmp_path / "lessons.jsonl")
    assert mem.reject("nope", actor="human") is False
