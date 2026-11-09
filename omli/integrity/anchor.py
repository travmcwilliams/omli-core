"""Git-anchored head: the first trust anchor.

The seal sidecar lives under ``var/`` beside the log it protects, so an actor
who can edit the log can also re-seal it. This module appends every seal's head to a
GIT-TRACKED, append-only anchor log: a re-sealer must also forge visible,
attributable (and once pushed, remote) git history. ``verify_anchor`` cross-checks a
seal against the anchored history — a seal whose head was never anchored is the
re-seal signature.

Rollback: restoring an older log, seal and signature together reads as ``behind`` only
when this anchor log is in use and pinned. With ``--no-anchor``, or once the anchor log is
deleted, a rollback is not detected.

Scope: alone, this raises the bar; it is not full tamper-evidence. The durable
trust anchor is the ed25519 signer (`signer.py`). This module never runs
git itself: committing/pushing the anchor file is the operator's seal workflow, kept
out of core so the tool stays pure and testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from omli.home import omli_home
from omli.integrity.verifier import SealResult

# Meant to be git-tracked, unlike the var/ sidecar. It is .jsonl, not .log, because ignore rules
# commonly exclude *.log, and an ignored anchor would defeat the whole point.
DEFAULT_ANCHOR = "integrity/artifacts.heads.jsonl"


class AnchorFileError(ValueError):
    """The anchor log exists but cannot be read or parsed."""


def _anchor_file(anchor_path: str | Path | None) -> Path:
    return Path(anchor_path) if anchor_path is not None else omli_home() / DEFAULT_ANCHOR


@dataclass(frozen=True)
class AnchorEntry:
    """One anchored seal: when, how many lines, and the chain head it committed to."""

    sealed_at: str
    count: int
    head: str

    def to_line(self) -> str:
        return json.dumps({"sealed_at": self.sealed_at, "count": self.count,
                           "head": self.head})


@dataclass(frozen=True)
class AnchorReport:
    """How a seal relates to the anchored history.

    * ``anchored``   — the seal's head is the latest anchored head (healthy)
    * ``behind``     — the head exists in the history but newer anchors follow
                       (an earlier seal restored after newer heads were anchored)
    * ``unanchored`` — the head appears nowhere in the anchor log: a seal was
                       (re)written without anchoring — the re-seal signature
    * ``no-anchor``  — no anchor log exists yet (feature not in use)
    * ``corrupt``    — the anchor log cannot be read or parsed (any ``Exception``; fails closed)

    ``no-anchor`` is trustworthy because the feature is optional, so a rollback is caught
    only while the anchor log is in use, pinned, and not passed over with ``--no-anchor``.
    """

    status: str
    entries: int
    latest_head: Optional[str]

    @property
    def trustworthy(self) -> bool:
        return self.status in ("anchored", "no-anchor")


def read_anchor(anchor_path: str | Path | None = None) -> Tuple[AnchorEntry, ...]:
    """All anchored entries, oldest first. A file that cannot be read, or a line that cannot
    be parsed (any ``Exception``) raises `AnchorFileError` — an anchor log that cannot be parsed
    must never quietly verify."""
    p = _anchor_file(anchor_path)
    if not p.exists():
        return ()
    entries = []
    try:
        text = p.read_bytes().decode("utf-8")
    except Exception as exc:  # unreadable path, a directory, not UTF-8, ...
        raise AnchorFileError(f"anchor: the file cannot be read ({type(exc).__name__})") from None
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception as exc:  # not JSON, too deeply nested, ...
            raise AnchorFileError(
                f"anchor: line {i} cannot be parsed ({type(exc).__name__})") from None
        if (not isinstance(d, dict) or type(d.get("count")) is not int
                or not isinstance(d.get("head"), str)):
            raise AnchorFileError(f"anchor: line {i} has the wrong shape")
        entries.append(AnchorEntry(sealed_at=str(d.get("sealed_at", "")),
                                   count=d["count"], head=d["head"]))
    return tuple(entries)


def anchor_head(seal: SealResult,
                anchor_path: str | Path | None = None) -> AnchorEntry:
    """Append the seal's head to the anchor log (append-only; idempotent per head).

    Re-anchoring an unchanged head is a no-op returning the existing entry, so a
    routine re-seal of an unmodified log never spams the git history.
    """
    p = _anchor_file(anchor_path)
    existing = read_anchor(p)
    if existing and existing[-1].head == seal.head and existing[-1].count == seal.count:
        return existing[-1]
    from datetime import datetime, timezone
    entry = AnchorEntry(sealed_at=datetime.now(timezone.utc).isoformat(),
                        count=seal.count, head=seal.head)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(entry.to_line() + "\n")
    return entry


def verify_anchor(sealed_head: Optional[str],
                  anchor_path: str | Path | None = None) -> AnchorReport:
    """Classify a seal's head against the anchored history (see AnchorReport)."""
    try:
        entries = read_anchor(anchor_path)
    except Exception:  # AnchorFileError, or any other failure: fail closed
        return AnchorReport("corrupt", 0, None)
    if not entries:
        return AnchorReport("no-anchor", 0, None)
    latest = entries[-1].head
    if sealed_head == latest:
        return AnchorReport("anchored", len(entries), latest)
    if any(e.head == sealed_head for e in entries):
        return AnchorReport("behind", len(entries), latest)
    return AnchorReport("unanchored", len(entries), latest)
