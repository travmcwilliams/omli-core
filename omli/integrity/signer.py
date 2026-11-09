"""ed25519 head signing: the durable trust anchor.

The git anchor (``anchor.py``) forces a re-sealer to also forge visible git history.
Signing adds a second requirement: a re-sealer cannot produce a valid *signature* over the
new head, because signing needs a private key that is kept outside the repo. Only the
**public** key is committed (``integrity/omli.head.pub``), and verification needs the public
key alone. Custody is a passphrase-protected keyfile on
disk **outside** the repo: ``~/.omli/seal.key`` by default, encrypted
PKCS8 PEM, passphrase supplied out-of-band (``OMLI_SEAL_PASSPHRASE``).

The signature sidecar sits beside the seal under ``var/`` — safe there, unlike the seal
itself, because forging it needs the private key. The presence of the committed public key
or keyring is what tells ``verify`` that signing is *expected*: with a
committed key, an absent signature is ``missing`` (not trustworthy — the delete-the-sig
dodge), so a forger would additionally have to remove the public key from tracked git
history. That is the tamper-evidence the seal alone does not give.

Scope: tamper-evidence applies to a **signed** seal (status ``signed``). An
unsigned seal with no committed key is corruption detection only (``unsigned``).
The signature proves only what its trust root proves: the keyring and the public key must be
pinned (committed, pushed, and out of reach of anyone who can edit the log), because an actor
who can rewrite them can install a key of their own, or delete both files so that signing
reads as not in use. A signature covers the sealed head and count, so records appended after
the seal are not covered by it.
``cryptography`` is an optional dependency (``omli[sign]``); absent it, a present
signature reports ``unverifiable`` — fail closed, never a silent pass. See
``anchor.py`` (the git anchor it complements).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from omli.home import omli_home
from omli.integrity.keyring import (
    DEFAULT_KEYRING,
    REVOKED,
    RETIRED,
    KeyRecord,
    KeyRing,
    KeyringError,
)
from omli.integrity.ledger import ALGO, atomic_write_text
from omli.integrity.verifier import SealResult

# Git-tracked public key (verification only). The committed key is also the signal that
# signing is active — see the module docstring / verify_signature.
DEFAULT_PUBKEY = "integrity/omli.head.pub"
# Private key path lives OUTSIDE the repo; env override, else the per-user default.
# This is the integrity CLI's only default outside OMLI_HOME, by design.
KEY_PATH_ENV = "OMLI_SEAL_KEY"
DEFAULT_KEY_PATH = "~/.omli/seal.key"
# Passphrase supplied out-of-band (never on argv, never committed).
PASSPHRASE_ENV = "OMLI_SEAL_PASSPHRASE"


@dataclass(frozen=True)
class SignatureReport:
    """How a seal's head relates to its ed25519 signature and the committed public key.

    * ``signed``       — public key committed, signature present and verifies (healthy)
    * ``invalid``      — signature present but does NOT verify against the current head
                         (a re-seal without the key, corruption, or a forgery attempt)
    * ``unsigned``     — no committed public key and no signature: feature not in use
    * ``missing``      — public key committed but the signature is absent: signing is
                         expected yet this seal is unsigned (the delete-the-sig dodge)
    * ``no-pubkey``    — a signature exists but no committed public key to check it with
    * ``unknown-key``  — a keyring exists and does not list the key the signature names. It
                         never falls back to the public key file, which a revocation leaves
                         in place: renaming the key id must not escape a revocation
    * ``unverifiable`` — a signature exists but ``cryptography`` is not installed
    * ``retired``      — verifies, but the signing key was rotated out (keyring): not the
                         active key, so a re-seal with the current key is required
    * ``revoked``      — the signing key was revoked (keyring): untrusted no matter how the
                         math checks out — the compromise response
    * ``corrupt``      — the signature file, the keyring, a keyring record's public-key PEM
                         or the public-key file cannot be read or parsed; fails closed
    """

    status: str
    signed_head: Optional[str]
    pubkey_id: Optional[str]
    detail: str = ""

    @property
    def trustworthy(self) -> bool:
        # Only a genuinely-off feature or a verified signature is trustworthy; every
        # other state fails closed.
        return self.status in ("signed", "unsigned")


def _backend():
    """Lazy optional import of the ed25519 primitives; None when unavailable."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        return serialization, Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:
        return None


def _pubkey_file(pubkey_path: str | Path | None) -> Path:
    return Path(pubkey_path) if pubkey_path is not None else omli_home() / DEFAULT_PUBKEY


def key_path(path: str | Path | None = None) -> Path:
    """Resolve the private-key path: explicit arg, else env, else the per-user default.

    Always expanded/resolved so the secret is looked for outside the repo tree. The per-user
    default is the integrity CLI's only default outside OMLI_HOME, by design.
    """
    raw = str(path) if path is not None else os.environ.get(KEY_PATH_ENV) or DEFAULT_KEY_PATH
    return Path(raw).expanduser().resolve()


def _sig_path(sig_path: Optional[str | Path], seal_path: str | Path) -> Path:
    """Signature sidecar beside the seal: ``artifacts.seal.json`` -> ``artifacts.seal.sig.json``."""
    if sig_path is not None:
        return Path(sig_path)
    sp = Path(seal_path)
    return sp.with_name(sp.stem + ".sig.json")


def _message(head: str, count: int) -> bytes:
    """Canonical signed message — binds algo + line count + head so a signature cannot
    be replayed against a differently-shaped log."""
    return f"omli-head-v1\n{ALGO}\n{count}\n{head}".encode("utf-8")


def _fingerprint(public_bytes: bytes) -> str:
    """Short, stable id for a public key (first 16 hex of sha256 over its raw bytes)."""
    return hashlib.sha256(public_bytes).hexdigest()[:16]


class PublicKeyError(ValueError):
    """A public key (the committed key file, or a keyring record's PEM) cannot be parsed."""


class SignatureFileError(ValueError):
    """The signature sidecar exists but cannot be read or parsed into a JSON object."""


def read_signature(sig_path: str | Path) -> Optional[dict]:
    """The signature sidecar as a dict, or None if absent. A file that cannot be read or
    parsed (any ``Exception``) raises `SignatureFileError` — it must never quietly verify."""
    p = Path(sig_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
    except Exception as exc:  # unreadable, not UTF-8, not JSON, too deeply nested, ...
        raise SignatureFileError(
            f"the signature file cannot be read as JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise SignatureFileError("the signature file is not a JSON object")
    return data


def generate_keypair(passphrase: str,
                     priv_path: str | Path | None = None,
                     pubkey_path: str | Path | None = None,
                     *,
                     keyring_path: str | Path | None = None,
                     supersedes: Optional[str] = None,
                     reason: str = "",
                     replace: bool = False) -> tuple[Path, Path, str]:
    """The key ceremony (run by the human): create an ed25519 keypair, write the
    passphrase-encrypted private key OUTSIDE the repo and the public key to the tracked
    path. Returns (private_path, public_path, pubkey_id).

    Refuses to clobber an existing private key unless ``replace=True`` — a plain re-run
    must be a deliberate delete first, never a silent overwrite; ``replace`` is the
    rotation ceremony deliberately retiring the current key. When ``keyring_path`` is given the
    new public key is appended to the append-only keyring (``supersedes`` links a rotation
    to the key it retires), making the keyring the durable trust root.
    """
    backend = _backend()
    if backend is None:
        raise RuntimeError("ed25519 signing needs the 'cryptography' package "
                           "(pip install 'omli[sign]')")
    if not passphrase:
        raise ValueError("a non-empty passphrase is required to protect the private key")
    serialization, Ed25519PrivateKey, _ = backend

    priv = key_path(priv_path)
    if priv.exists() and not replace:
        raise FileExistsError(f"private key already exists at {priv}; "
                              "delete it deliberately (or rotate) before regenerating")
    pub = _pubkey_file(pubkey_path)

    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_bytes(priv_pem)
    try:  # best-effort lock-down; POSIX honors it, Windows ignores the mode bits
        priv.chmod(0o600)
    except OSError:
        pass
    pub.parent.mkdir(parents=True, exist_ok=True)
    pub.write_bytes(pub_pem)

    pubkey_id = _fingerprint(pub_raw)
    if keyring_path is not None:
        KeyRing(keyring_path).append_key(KeyRecord(
            pubkey_id=pubkey_id,
            pubkey_pem=pub_pem.decode("utf-8"),
            created_at=datetime.now(timezone.utc).isoformat(),
            supersedes=supersedes,
            reason=reason,
        ))
    return priv, pub, pubkey_id


def signing_preflight(priv_path: str | Path | None = None,
                      passphrase: Optional[str] = None) -> Optional[str]:
    """Why signing would fail, checked BEFORE anything is written; None when no problem was
    found (or when there is no key at all, since sealing unsigned is allowed). It does not try
    the passphrase, so a wrong passphrase or a corrupt key still raises later, in ``sign_head``.

    ``sign_head`` raises on a present-but-unusable key — right, but callers that seal and
    anchor *first* would raise having already written those: a half-state (seal + anchor
    on disk, signature absent) that ``verify`` then reports as ``missing`` until someone
    re-seals with the passphrase. Covers sign_head's missing-backend and missing-passphrase
    refusals so a caller can decline to start instead of stopping half-way.
    """
    priv = key_path(priv_path)
    if not priv.exists():
        return None
    if _backend() is None:
        return (f"a seal key exists at {priv} but the 'cryptography' package is not "
                "installed (pip install 'omli[sign]')")
    pw = passphrase if passphrase is not None else os.environ.get(PASSPHRASE_ENV)
    if not pw:
        return f"seal key at {priv} is passphrase-protected; set {PASSPHRASE_ENV}"
    return None


def sign_head(seal: SealResult,
              sig_path: Optional[str | Path] = None,
              priv_path: str | Path | None = None,
              passphrase: Optional[str] = None,
              pubkey_path: str | Path | None = None) -> Optional[SignatureReport]:
    """Sign the seal's head with the private key and write the signature sidecar.

    Returns None (nothing signed) when no private key is present — sealing on a machine
    without the key is allowed; the seal simply stays unsigned. When the key IS present
    but cannot be used (no passphrase / no crypto backend), raises loudly rather than
    silently leaving the seal unsigned.
    """
    priv = key_path(priv_path)
    if not priv.exists():
        return None
    backend = _backend()
    if backend is None:
        raise RuntimeError("a seal key exists but the 'cryptography' package is not "
                           "installed (pip install 'omli[sign]')")
    serialization, _, _ = backend

    pw = passphrase if passphrase is not None else os.environ.get(PASSPHRASE_ENV)
    if not pw:
        raise ValueError(f"seal key at {priv} is passphrase-protected; set {PASSPHRASE_ENV}")

    private_key = serialization.load_pem_private_key(priv.read_bytes(), password=pw.encode("utf-8"))
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    pubkey_id = _fingerprint(pub_raw)

    signature = private_key.sign(_message(seal.head, seal.count))
    sp = _sig_path(sig_path, seal.seal)
    payload = {
        "algo": "ed25519",
        "head": seal.head,
        "count": seal.count,
        "sig": base64.b64encode(signature).decode("ascii"),
        "pubkey_id": pubkey_id,
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_text(sp, json.dumps(payload, indent=2) + "\n")
    return SignatureReport("signed", seal.head, pubkey_id, f"signed -> {sp.name}")


def _verify_sig_math(pubkey_pem: bytes, sig: dict, sealed_head: Optional[str],
                     sealed_count: int) -> Optional[str]:
    """Check the signature math against a public key PEM. Returns None when it verifies, else
    a failure detail. A PEM that cannot be parsed raises `PublicKeyError` (the caller reports
    it as ``corrupt``); a signature field that cannot be decoded or does not verify is a
    failure detail (``invalid``). The caller must have confirmed the crypto backend is available.
    """
    serialization, _, _ = _backend()
    try:
        public_key = serialization.load_pem_public_key(pubkey_pem)
    except Exception as exc:
        problem = f"the public key cannot be parsed ({type(exc).__name__})"
    else:
        problem = None
    if problem is not None:
        raise PublicKeyError(problem)
    try:
        from cryptography.exceptions import InvalidSignature

        signature = base64.b64decode(sig["sig"])
        public_key.verify(signature, _message(sealed_head or "", sealed_count))
    except InvalidSignature:
        return "signature does not verify against the current head"
    except Exception as exc:  # malformed key/sig — fail closed, never a silent pass
        return f"signature could not be checked: {exc}"
    return None


def verify_signature(sealed_head: Optional[str],
                     sealed_count: int,
                     sig_path: str | Path,
                     pubkey_path: str | Path | None = None,
                     *,
                     keyring_path: str | Path | None = None) -> SignatureReport:
    """Classify a seal's head against its signature and the committed trust root.

    The trust root is the append-only **keyring** (rotation/revocation-aware) when present,
    else the single committed public key file. Either existing is the "signing is on" signal:
    an absent signature is then ``missing`` (not trustworthy), so a forger cannot delete the
    sidecar to look innocent without also removing those files. Trust is ACTIVE-KEY-ONLY: a
    signature by a retired (rotated-out), revoked or unlisted key is not trustworthy even when
    the math verifies. A signature file, keyring, keyring PEM or public-key file that cannot be
    read or parsed (any ``Exception``) gives ``corrupt``.
    """
    try:
        return _verify_signature(sealed_head, sealed_count, sig_path, pubkey_path,
                                 keyring_path=keyring_path)
    except (SignatureFileError, KeyringError, PublicKeyError) as exc:
        return SignatureReport("corrupt", sealed_head, None, str(exc))
    except Exception as exc:  # any other failure fails closed, never a pass or a traceback
        return SignatureReport("corrupt", sealed_head, None,
                               f"a trust file cannot be read ({type(exc).__name__})")


def _verify_signature(sealed_head: Optional[str],
                      sealed_count: int,
                      sig_path: str | Path,
                      pubkey_path: str | Path | None = None,
                      *,
                      keyring_path: str | Path | None = None) -> SignatureReport:
    sig = read_signature(sig_path)
    ring = KeyRing(keyring_path)
    pub_exists = _pubkey_file(pubkey_path).exists()

    if sig is None:
        # No signature. Off entirely (no committed key at all) is fine; expected-but-absent
        # is not — a keyring, even one with no active key left, or a pubkey file means signing
        # is expected.
        if ring.exists() or pub_exists:
            return SignatureReport("missing", sealed_head, None,
                                   "signing expected (committed key) but no signature for this seal")
        return SignatureReport("unsigned", sealed_head, None, "signing not in use")

    claimed_id = sig.get("pubkey_id")

    # With a keyring, it alone decides. A key it does not list is refused outright.
    if ring.exists():
        known = ring.get(claimed_id)
        if known is None:
            return SignatureReport("unknown-key", sealed_head, claimed_id,
                                   "the keyring does not list the key this signature names")
        status = ring.status_of(claimed_id)
        if status == REVOKED:
            return SignatureReport("revoked", sealed_head, claimed_id,
                                   "signed by a revoked key — untrusted regardless of validity")
        if _backend() is None:
            return SignatureReport("unverifiable", sealed_head, claimed_id,
                                   "signature present but 'cryptography' is not installed")
        detail = _verify_sig_math(known.pubkey_pem.encode("utf-8"), sig, sealed_head, sealed_count)
        if detail is not None:
            return SignatureReport("invalid", sealed_head, claimed_id, detail)
        if status == RETIRED:
            return SignatureReport("retired", sealed_head, claimed_id,
                                   "verifies, but the key was rotated out; re-seal with the active key")
        return SignatureReport("signed", sealed_head, claimed_id, "signature verified (active key)")

    # No keyring at all: the single committed pubkey file is the trust root.
    if not pub_exists:
        return SignatureReport("no-pubkey", sealed_head, claimed_id,
                               "signature present but no committed key (keyring or pubkey file) to verify it")
    if _backend() is None:
        return SignatureReport("unverifiable", sealed_head, claimed_id,
                               "signature present but 'cryptography' is not installed")
    detail = _verify_sig_math(_pubkey_file(pubkey_path).read_bytes(), sig, sealed_head, sealed_count)
    if detail is not None:
        return SignatureReport("invalid", sealed_head, claimed_id, detail)
    return SignatureReport("signed", sealed_head, claimed_id, "signature verified")
