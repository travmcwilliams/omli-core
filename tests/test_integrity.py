"""Tests for at-rest artifact-log integrity (the on-disk half of Primitive 4).

Checks that the hash chain is deterministic and detects an edited, reordered or truncated
sealed log, while append-only *growth* stays healthy ("appended"). Byte-level edits (line
endings, blank lines, a byte order mark) are covered in test_trust_boundaries.py.
Uses real files in tmp_path; no store, no model, no domain.
"""

from __future__ import annotations

from omli.integrity.ledger import GENESIS, compute_chain, content_id, head, link
from omli.integrity.verifier import seal, verify


def _write(path, lines):
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")


# --- ledger primitives -----------------------------------------------------
def test_chain_is_deterministic_and_position_sensitive():
    a = compute_chain(["x", "y", "z"])
    b = compute_chain(["x", "y", "z"])
    assert a == b
    assert len(a) == 3 and len(set(a)) == 3            # each step distinct
    assert compute_chain(["y", "x", "z"]) != a          # order matters
    assert head([]) == GENESIS                          # empty log head
    assert head(a) == a[-1]


def test_link_folds_in_previous_head():
    h1 = link(GENESIS, "line-1")
    assert link(h1, "line-2") != link("", "line-2")     # depends on history
    assert content_id("line-1") != h1                   # content id is position-free


# --- seal / verify ---------------------------------------------------------
def test_unsealed_then_ok(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    _write(log, ['{"id":"a"}', '{"id":"b"}'])
    assert verify(log).status == "unsealed"

    res = seal(log)
    assert res.count == 2
    rep = verify(log)
    assert rep.status == "ok" and rep.trustworthy
    assert rep.head == res.head


def test_append_only_growth_is_healthy(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    _write(log, ['{"id":"a"}', '{"id":"b"}'])
    seal(log)
    _write(log, ['{"id":"a"}', '{"id":"b"}', '{"id":"c"}'])  # append one
    rep = verify(log)
    assert rep.status == "appended"
    assert rep.appended == 1 and rep.trustworthy
    assert rep.broken_at is None


def test_edit_of_sealed_line_is_detected(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    _write(log, ['{"id":"a"}', '{"id":"b"}', '{"id":"c"}'])
    seal(log)
    _write(log, ['{"id":"a"}', '{"id":"HACKED"}', '{"id":"c"}'])  # edit line index 1
    rep = verify(log)
    assert rep.status == "tampered"
    assert rep.broken_at == 1
    assert not rep.trustworthy


def test_reorder_is_detected(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    _write(log, ['{"id":"a"}', '{"id":"b"}', '{"id":"c"}'])
    seal(log)
    _write(log, ['{"id":"b"}', '{"id":"a"}', '{"id":"c"}'])  # swap first two
    rep = verify(log)
    assert rep.status == "tampered"
    assert rep.broken_at == 0


def test_truncation_is_detected(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    _write(log, ['{"id":"a"}', '{"id":"b"}', '{"id":"c"}'])
    seal(log)
    _write(log, ['{"id":"a"}', '{"id":"b"}'])  # drop the last sealed line
    rep = verify(log)
    assert rep.status == "truncated"
    assert rep.current_count == 2 and rep.sealed_count == 3
    assert not rep.trustworthy


def test_empty_log_seals_and_verifies(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    log.write_text("", encoding="utf-8")
    res = seal(log)
    assert res.count == 0 and res.head == GENESIS
    assert verify(log).status == "ok"
