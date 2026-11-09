"""Append-only key registry — the durable trust root for signed heads.

`signer.py` signs heads with an ed25519 keypair. A single committed public key cannot express
two events — **rotation** (retire a key on schedule or suspicion and move to a new one) and
**revocation** (a key is compromised; its signatures must stop being trusted *even though the
math still verifies*).

This is the registry that records both, as **append-only, git-tracked data** — the same
immutable-per-version pattern as the Model and Connection registries (Primitive 4: change
by appending a successor, never by editing a trust root in place). Each line is one event:

    {"type":"key","pubkey_id":...,"pubkey_pem":...,"created_at":...,"supersedes":...,"reason":...}
    {"type":"revocation","pubkey_id":...,"revoked_at":...,"reason":...}

Status is DERIVED from the log, never stored (mirrors `MemoryStore.active()`):
  * active   — the latest key record, provided it is not revoked. When the latest key is
               revoked there is NO active key (no fallback to an older one) until a new key
               is added
  * retired  — any other listed key that is not revoked
  * revoked  — a key with a revocation record; untrusted regardless of any valid signature
  * unknown  — a pubkey_id the registry has never seen

Trust rule (active key only): only a signature by the *active* key is trustworthy.
A retired or revoked key's signature is not — so after a rotation the current seal verifies
as `retired` until it is re-sealed with the active key. The registry stores only PUBLIC keys; the
private key stays a single passphrase-protected file off-repo (see `signer.py`). stdlib
only — no crypto here; this module just records and classifies.

The keyring is a trust root only while it is pinned (committed, pushed, and out of reach of
anyone who can edit the log). Whoever can rewrite this file can add their own key.
A file that cannot be read or parsed (any ``Exception``) raises `KeyringError`;
`verify_signature` turns that into a `corrupt` report rather than granting or denying trust.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from omli.home import omli_home

# Meant to be git-tracked, like the anchor log and unlike
# the var/ sidecars. A committed keyring is what tells `verify` that signing is expected.
DEFAULT_KEYRING = "integrity/keyring.jsonl"

ACTIVE = "active"
RETIRED = "retired"
REVOKED = "revoked"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class KeyRecord:
    """One public key in the registry. `supersedes` links a rotation to the key it retired."""

    pubkey_id: str
    pubkey_pem: str
    created_at: str
    supersedes: Optional[str] = None
    reason: str = ""

    def to_line(self) -> str:
        return json.dumps({
            "type": "key",
            "pubkey_id": self.pubkey_id,
            "pubkey_pem": self.pubkey_pem,
            "created_at": self.created_at,
            "supersedes": self.supersedes,
            "reason": self.reason,
        })


class KeyringError(ValueError):
    """The keyring file exists but cannot be read or parsed into key and revocation records."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KeyRing:
    """Read/append view over the append-only key registry. Status is always derived."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else omli_home() / DEFAULT_KEYRING

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def _load(self) -> Tuple[Tuple[KeyRecord, ...], set]:
        """(key records oldest-first, revoked ids). A file that cannot be read, or a line that
        cannot be parsed (any ``Exception``) raises `KeyringError` — a registry that cannot be
        parsed must never quietly grant or deny trust."""
        if not self._path.exists():
            return (), set()
        keys: list[KeyRecord] = []
        revoked: set = set()
        try:
            text = self._path.read_bytes().decode("utf-8")
        except Exception as exc:  # unreadable path, a directory, not UTF-8, ...
            raise KeyringError(f"keyring: the file cannot be read ({type(exc).__name__})") from None
        for n, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception as exc:  # not JSON, too deeply nested, ...
                raise KeyringError(
                    f"keyring: line {n} cannot be parsed ({type(exc).__name__})") from None
            if not isinstance(d, dict):
                raise KeyringError(f"keyring: line {n} is not a JSON object")
            kind = d.get("type")
            if kind == "key":
                pid, pem = d.get("pubkey_id"), d.get("pubkey_pem")
                if not isinstance(pid, str) or not pid or not isinstance(pem, str):
                    raise KeyringError(f"keyring: line {n} is a key record with the wrong shape")
                keys.append(KeyRecord(
                    pubkey_id=pid,
                    pubkey_pem=pem,
                    created_at=str(d.get("created_at", "")),
                    supersedes=d.get("supersedes"),
                    reason=str(d.get("reason", "")),
                ))
            elif kind == "revocation":
                pid = d.get("pubkey_id")
                if not isinstance(pid, str) or not pid:
                    raise KeyringError(f"keyring: line {n} is a revocation with the wrong shape")
                revoked.add(pid)
            else:
                raise KeyringError(f"keyring: line {n} has an unknown record type")
        return tuple(keys), revoked

    def records(self) -> Tuple[KeyRecord, ...]:
        """All key records, oldest first (revocations are separate — see `revoked_ids`)."""
        return self._load()[0]

    def revoked_ids(self) -> set:
        return self._load()[1]

    def get(self, pubkey_id: Optional[str]) -> Optional[KeyRecord]:
        """The key record for an id, or None if the registry has never seen it."""
        if not pubkey_id:
            return None
        for rec in self._load()[0]:
            if rec.pubkey_id == pubkey_id:
                return rec
        return None

    def active(self) -> Optional[KeyRecord]:
        """The current signing key: the LATEST key record, unless it is revoked.

        Deliberately does NOT fall back to an earlier key when the latest is revoked:
        revoking the active key with no successor yields None, a fail-closed 'rotate before
        trusting any new seal', not a silent resurrection of a retired key.
        """
        keys, revoked = self._load()
        if not keys:
            return None
        last = keys[-1]
        return None if last.pubkey_id in revoked else last

    def status_of(self, pubkey_id: Optional[str]) -> str:
        """Classify a key id: active | retired | revoked | unknown (derived, never stored)."""
        keys, revoked = self._load()
        if not pubkey_id or all(k.pubkey_id != pubkey_id for k in keys):
            return UNKNOWN
        if pubkey_id in revoked:
            return REVOKED
        active = self.active()
        return ACTIVE if (active is not None and active.pubkey_id == pubkey_id) else RETIRED

    def append_key(self, record: KeyRecord) -> KeyRecord:
        """Append a key record (append-only). Refuses to re-add an id already present —
        a rotation must introduce a NEW key, never restate a listed one."""
        keys, _ = self._load()
        if any(k.pubkey_id == record.pubkey_id for k in keys):
            raise ValueError(f"keyring: key {record.pubkey_id} is already registered")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_line() + "\n")
        return record

    def append_revocation(self, pubkey_id: str, reason: str = "") -> bool:
        """Append a revocation marker for a key. Returns False (no-op) if the id is unknown
        or already revoked — revocation is append-only and idempotent per id."""
        keys, revoked = self._load()
        if all(k.pubkey_id != pubkey_id for k in keys) or pubkey_id in revoked:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"type": "revocation", "pubkey_id": pubkey_id,
                           "revoked_at": _now(), "reason": reason})
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
