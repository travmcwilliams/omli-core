"""Git-anchored head: append-only anchor log + re-seal detection.

The seal sidecar is re-sealable; the anchor log is meant to be git-tracked, so a seal whose head
was never anchored is the re-seal signature. These tests prove: anchoring is
append-only and idempotent, and verify_anchor classifies anchored / behind /
unanchored / no-anchor — including the end-to-end attack: edit the log, re-seal,
and the fresh (attacker) seal head verifies as ``unanchored``.
"""

from __future__ import annotations

import json

import pytest

from omli.integrity.anchor import anchor_head, read_anchor, verify_anchor
from omli.integrity.verifier import seal, verify


def _log(tmp_path, lines=("alpha", "bravo", "charlie")):
    p = tmp_path / "artifacts.jsonl"
    p.write_text("".join(json.dumps({"x": l}) + "\n" for l in lines), encoding="utf-8")
    return p


def test_anchor_appends_and_is_idempotent_per_head(tmp_path):
    log, anchor = _log(tmp_path), tmp_path / "heads.log"
    first = anchor_head(seal(log), anchor)
    again = anchor_head(seal(log), anchor)          # unchanged log -> same head
    assert again == first                            # no duplicate line
    assert len(read_anchor(anchor)) == 1

    log.open("a", encoding="utf-8").write(json.dumps({"x": "delta"}) + "\n")
    second = anchor_head(seal(log), anchor)
    entries = read_anchor(anchor)
    assert len(entries) == 2
    assert entries == (first, second)                # append-only: prefix preserved
    assert entries[-1].count == 4


def test_verify_anchor_statuses(tmp_path):
    log, anchor = _log(tmp_path), tmp_path / "heads.log"
    assert verify_anchor("whatever", anchor).status == "no-anchor"

    s1 = seal(log)
    anchor_head(s1, anchor)
    assert verify_anchor(s1.head, anchor).status == "anchored"
    assert verify_anchor(s1.head, anchor).trustworthy

    log.open("a", encoding="utf-8").write(json.dumps({"x": "delta"}) + "\n")
    s2 = seal(log)
    anchor_head(s2, anchor)
    assert verify_anchor(s2.head, anchor).status == "anchored"
    behind = verify_anchor(s1.head, anchor)          # an earlier seal restored
    assert behind.status == "behind" and not behind.trustworthy


def test_reseal_after_edit_is_unanchored(tmp_path):
    """The attack the anchor exists for: edit the log, re-seal, look innocent."""
    log, anchor = _log(tmp_path), tmp_path / "heads.log"
    anchor_head(seal(log), anchor)

    lines = log.read_text(encoding="utf-8").splitlines()
    lines[1] = json.dumps({"x": "FORGED"})           # edit a sealed line
    log.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    attacker_seal = seal(log)                        # re-seal to cover the tracks
    report = verify(log, attacker_seal.seal)
    assert report.status == "ok"                     # the sidecar alone is fooled...
    anchored = verify_anchor(report.sealed_head, anchor)
    assert anchored.status == "unanchored"           # ...the anchor is not
    assert not anchored.trustworthy


def test_reseal_after_truncate_is_unanchored(tmp_path):
    """A distinct attack from editing (test_reseal_after_edit covers that): cut tail lines,
    then re-seal the shorter log so the sidecar reads `ok`. The truncated head was never
    anchored, so the anchor log catches it."""
    log, anchor = _log(tmp_path), tmp_path / "heads.log"
    anchor_head(seal(log), anchor)                   # anchor the full 3-line head

    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("".join(l + "\n" for l in lines[:-1]), encoding="utf-8")  # drop last line

    attacker_seal = seal(log)                        # re-seal the truncated log
    report = verify(log, attacker_seal.seal)
    assert report.status == "ok"                     # the sidecar alone is fooled...
    anchored = verify_anchor(report.sealed_head, anchor)
    assert anchored.status == "unanchored"           # ...the anchor is not
    assert not anchored.trustworthy


def test_malformed_anchor_fails_loud(tmp_path):
    from omli.integrity.anchor import AnchorFileError

    anchor = tmp_path / "heads.log"
    anchor.write_text("not json\n", encoding="utf-8")
    with pytest.raises(AnchorFileError):
        read_anchor(anchor)
    report = verify_anchor("h", anchor)
    assert report.status == "corrupt" and not report.trustworthy
