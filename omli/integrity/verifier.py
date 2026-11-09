"""Seal + verify the artifact log against its hash chain (at-rest Primitive 4).

`seal` snapshots the current chain into a sidecar (`var/artifacts.seal.json`); it never
touches the log itself (the log stays the single source of truth — Primitive 3). `verify`
recomputes the chain from the live log's exact bytes and compares it to the seal,
classifying the log:

  * unsealed  — no seal exists yet (can't judge tamper, only that the chain is well-formed)
  * ok        — byte-identical to what was sealed
  * appended  — the sealed prefix is intact and new records follow it
  * tampered  — a sealed record changed or was reordered (`broken_at` is the first bad index),
                or the seal's own chain, head and count disagree (`broken_at` is None)
  * truncated — sealed records were removed (the log is shorter than the seal)
  * corrupt   — the log or the seal file cannot be read, the seal cannot be parsed
                (any ``Exception``), or the seal has the wrong
                shape, or was written by another chain format; nothing can be judged, so it
                fails closed

`verify` is pure and deterministic. Before comparing, it requires the seal's chain, head and
count to agree with each other, because the anchor and the signature cover only the head and
count: a seal whose chain was rewritten under an unchanged head would otherwise pass both.

Trust model. A seal covers only the sealed prefix: records added after it read as
`appended`, and nothing vouches for them until the next seal. `verify` alone, over an
unsigned seal that anyone can rewrite, detects corruption and naive edits, not a re-sealing
adversary. `anchor.py` makes a re-seal or a rollback visible only when the anchor log is in
use and pinned (committed and pushed). `signer.py` makes a re-seal unforgeable only while the
keyring and the public key are pinned out of the editor's reach; an actor who can rewrite
those files can also replace the trust root.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from omli.integrity.ledger import ALGO, atomic_write_text, compute_chain, head, read_log_records

DEFAULT_LOG = "var/artifacts.jsonl"
DEFAULT_SEAL = "var/artifacts.seal.json"


@dataclass(frozen=True)
class SealResult:
    log: Path
    seal: Path
    count: int
    head: str


@dataclass(frozen=True)
class IntegrityReport:
    status: str               # unsealed | ok | appended | tampered | truncated | corrupt
    sealed_count: int
    current_count: int
    appended: int
    broken_at: Optional[int]  # first divergent record index (tampered/truncated)
    head: str                 # current head
    sealed_head: Optional[str]
    detail: str = ""          # why, when the seal itself is inconsistent or unreadable

    @property
    def trustworthy(self) -> bool:
        """True when the sealed prefix is intact. `appended` records are not vouched for."""
        return self.status in ("ok", "appended")


def _seal_path(seal_path: Optional[str | Path], log_path: str | Path) -> Path:
    if seal_path is not None:
        return Path(seal_path)
    return Path(log_path).with_suffix(".seal.json")


def seal(log_path: str | Path = DEFAULT_LOG,
         seal_path: Optional[str | Path] = None) -> SealResult:
    """Write a fresh seal capturing the current chain. Re-sealing advances the head."""
    log_path = Path(log_path)
    sp = _seal_path(seal_path, log_path)
    records = read_log_records(log_path)
    chain = compute_chain(records)
    data = {
        "algo": ALGO,
        "log": log_path.name,
        "count": len(records),
        "head": head(chain),
        "sealed_at": datetime.now(timezone.utc).isoformat(),
        "chain": chain,
    }
    atomic_write_text(sp, json.dumps(data, indent=2) + "\n")
    return SealResult(log=log_path, seal=sp, count=len(records), head=head(chain))


def verify(log_path: str | Path = DEFAULT_LOG,
           seal_path: Optional[str | Path] = None) -> IntegrityReport:
    """Recompute the chain and classify the log against its seal."""
    log_path = Path(log_path)
    sp = _seal_path(seal_path, log_path)
    try:
        records = read_log_records(log_path)
    except Exception as exc:  # the log itself cannot be read: nothing can be judged
        return IntegrityReport("corrupt", 0, 0, 0, None, "", None,
                               detail=f"the log cannot be read ({type(exc).__name__})")
    chain = compute_chain(records)
    cur_count = len(records)
    cur_head = head(chain)

    if not sp.exists():
        return IntegrityReport("unsealed", 0, cur_count, 0, None, cur_head, None)

    def corrupt(why: str) -> IntegrityReport:
        return IntegrityReport("corrupt", 0, cur_count, 0, None, cur_head, None, detail=why)

    try:
        sealed = json.loads(sp.read_bytes().decode("utf-8"))
    except Exception as exc:  # unreadable, a directory, not UTF-8, not JSON, too deeply nested
        return corrupt(f"the seal file cannot be read as JSON ({type(exc).__name__})")
    if not isinstance(sealed, dict):
        return corrupt("the seal file is not a JSON object")
    if sealed.get("algo") != ALGO:
        return corrupt("the seal was written by another chain format; re-seal")
    sealed_chain = sealed.get("chain")
    sealed_count = sealed.get("count")
    sealed_head = sealed.get("head")
    if (not isinstance(sealed_chain, list)
            or not all(isinstance(h, str) for h in sealed_chain)
            or type(sealed_count) is not int
            or not isinstance(sealed_head, str)):
        return corrupt("the seal file has the wrong shape")

    # The seal must agree with itself first. The anchor and the signature cover only its head
    # and count, and the comparison below reads only its chain, so all three must be one record.
    if sealed_count != len(sealed_chain) or sealed_head != head(sealed_chain):
        return IntegrityReport("tampered", sealed_count, cur_count, 0, None, cur_head, sealed_head,
                               detail="the seal's chain, head and count disagree")

    # First divergence within the overlapping prefix = the tamper point.
    overlap = min(sealed_count, cur_count)
    broken_at: Optional[int] = None
    for i in range(overlap):
        if chain[i] != sealed_chain[i]:
            broken_at = i
            break

    if broken_at is not None:
        return IntegrityReport("tampered", sealed_count, cur_count,
                               max(0, cur_count - sealed_count), broken_at,
                               cur_head, sealed_head)
    if cur_count < sealed_count:
        return IntegrityReport("truncated", sealed_count, cur_count, 0, cur_count,
                               cur_head, sealed_head)
    if cur_count == sealed_count:
        return IntegrityReport("ok", sealed_count, cur_count, 0, None,
                               cur_head, sealed_head)
    return IntegrityReport("appended", sealed_count, cur_count,
                           cur_count - sealed_count, None, cur_head, sealed_head,
                           detail="records after the sealed prefix are not covered until re-sealed")


def render(report: IntegrityReport) -> str:
    badge = {
        "ok": "OK", "appended": "OK (+appended, not yet sealed)", "unsealed": "UNSEALED",
        "tampered": "TAMPERED", "truncated": "TRUNCATED", "corrupt": "CORRUPT",
    }.get(report.status, report.status.upper())
    lines = [
        f"integrity: {badge}",
        f"  sealed records : {report.sealed_count}",
        f"  current records: {report.current_count}"
        + (f"  (+{report.appended} appended)" if report.appended else ""),
        f"  head           : {report.head[:16] or '(empty)'}",
    ]
    if report.sealed_head is not None and report.sealed_head != report.head:
        lines.append(f"  sealed head    : {report.sealed_head[:16]}")
    if report.broken_at is not None:
        verb = "removed at" if report.status == "truncated" else "first altered at"
        lines.append(f"  !! {verb} record index {report.broken_at}")
    if report.detail:
        lines.append(f"  !! {report.detail}")
    return "\n".join(lines)
