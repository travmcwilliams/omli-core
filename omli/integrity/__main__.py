"""CLI for at-rest artifact-log integrity — `python -m omli.integrity`.

    python -m omli.integrity keygen   # one-time key ceremony (writes the signing keypair)
    python -m omli.integrity seal     # snapshot the chain into sidecar seal + anchor + signature
    python -m omli.integrity verify   # recompute + classify the log against seal + anchor + signature
    python -m omli.integrity rotate   # retire the active key and append a new one to the keyring
    python -m omli.integrity revoke <pubkey_id> [--reason TEXT]   # append a revocation

Defaults operate on `var/artifacts.jsonl` + `var/artifacts.seal.json` under $OMLI_HOME, or the
current directory when it is unset, with the head anchored to
`integrity/artifacts.heads.jsonl` and, once the key ceremony has run, signed with the
ed25519 seal key, the signature written beside the seal and checked against the git-committed public key
`integrity/omli.head.pub`. A re-sealer must forge git history AND forge a
signature it cannot make without the off-repo private key. `--no-anchor` / `--no-sign`
skip those layers; `seal` pre-flights the key and refuses to start when it is present but
unusable (no passphrase / no crypto dep), rather than writing a seal + anchor it cannot
then sign. `verify` exits 1 when the sealed history is not provably intact
(tampered/truncated), the log or a seal, signature, keyring or anchor file cannot be read or
parsed (corrupt; any other ``Exception`` raised by the command is reported the same way),
the head was never anchored (the re-seal signature) or is behind the latest anchored head,
OR a committed key's signature is
missing, invalid, retired, revoked or from an unlisted key, so a script can gate on the exit
code. This tool never runs git: commit + push the anchor, keyring and public-key files to pin
them.

What a passing `verify` does and does not prove:
  * the seal covers the sealed prefix only; records added after it read as `appended` and
    are vouched for by nothing until the next seal;
  * a rollback (older log, seal and signature restored together) reads as `behind` only
    when the anchor log is in use and pinned, and is missed under `--no-anchor`;
  * the signature is only as good as its trust root: the keyring and public key must be
    pinned out of reach of anyone who can edit the log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from omli.home import omli_home
from omli.integrity.anchor import DEFAULT_ANCHOR, AnchorFileError, anchor_head, verify_anchor
from omli.integrity.keyring import DEFAULT_KEYRING, KeyRing, KeyringError
from omli.integrity.signer import (
    DEFAULT_PUBKEY,
    PASSPHRASE_ENV,
    SignatureFileError,
    generate_keypair,
    sign_head,
    signing_preflight,
    verify_signature,
)
from omli.integrity.verifier import render, seal, verify


def home_root() -> Path:
    """The CLI's root: $OMLI_HOME if set, else the current working directory."""
    return omli_home()


def _passphrase(prompt: str) -> Optional[str]:
    """The seal passphrase from the env, else an interactive prompt (None if neither)."""
    pw = os.environ.get(PASSPHRASE_ENV)
    if pw:
        return pw
    try:
        import getpass

        return getpass.getpass(prompt)
    except Exception:
        return None


def _paths(args: Sequence[str]) -> tuple[Path, Optional[Path], Path]:
    home = home_root()
    log = Path(args[0]) if args else home / "var" / "artifacts.jsonl"
    sealp = Path(args[1]) if len(args) > 1 else None
    anchorp = Path(args[2]) if len(args) > 2 else home / DEFAULT_ANCHOR
    return log, sealp, anchorp


def _keygen(home: Path) -> int:
    pw = _passphrase("passphrase for the new seal key: ")
    if not pw:
        print(f"omli.integrity: set {PASSPHRASE_ENV} to run keygen non-interactively",
              file=sys.stderr)
        return 2
    try:
        priv, pub, pubkey_id = generate_keypair(
            pw, pubkey_path=home / DEFAULT_PUBKEY, keyring_path=home / DEFAULT_KEYRING,
            reason="bootstrap")
    except Exception as exc:
        print(f"omli.integrity: keygen failed: {exc}", file=sys.stderr)
        return 2
    print(f"private key (KEEP OFF-REPO): {priv}")
    print(f"public key  (COMMIT THIS):   {pub}")
    print(f"keyring     (COMMIT THIS):   {home / DEFAULT_KEYRING}")
    print(f"pubkey id: {pubkey_id} (active)")
    print("commit + push the public key and keyring; keep the private key and passphrase off-repo")
    return 0


def _rotate(home: Path) -> int:
    """Retire the active key and move to a new one (append-only). The private key file is
    overwritten with the new key; the retired public key stays in the keyring as `retired`.
    Until a re-seal, the current seal is signed by a retired key (verify -> `retired`, not
    trustworthy)."""
    ring = KeyRing(home / DEFAULT_KEYRING)
    active = ring.active()
    if active is None:
        print("omli.integrity: no active key to rotate; run `keygen` first", file=sys.stderr)
        return 2
    pw = _passphrase("passphrase for the NEW seal key: ")
    if not pw:
        print(f"omli.integrity: set {PASSPHRASE_ENV} to rotate non-interactively", file=sys.stderr)
        return 2
    try:
        priv, pub, new_id = generate_keypair(
            pw, pubkey_path=home / DEFAULT_PUBKEY, keyring_path=home / DEFAULT_KEYRING,
            supersedes=active.pubkey_id, reason="rotation", replace=True)
    except Exception as exc:
        print(f"omli.integrity: rotate failed: {exc}", file=sys.stderr)
        return 2
    print(f"rotated: {active.pubkey_id} (retired) -> {new_id} (active)")
    print("RE-SEAL so the current head is signed by the active key:")
    print("  python -m omli.integrity seal")
    print("until then, verify reports the current seal as `retired` (not trustworthy).")
    print("commit + push the public key and keyring; keep the new private key off-repo")
    return 0


def _revoke(home: Path, rest: Sequence[str]) -> int:
    """Mark a key compromised: its signatures become untrusted regardless of validity."""
    reason = ""
    args = list(rest)
    if "--reason" in args:
        i = args.index("--reason")
        reason = args[i + 1] if i + 1 < len(args) else ""
        args = args[:i] + args[i + 2:]
    if not args:
        print("usage: python -m omli.integrity revoke <pubkey_id> [--reason TEXT]", file=sys.stderr)
        return 2
    pubkey_id = args[0]
    ring = KeyRing(home / DEFAULT_KEYRING)
    if not ring.append_revocation(pubkey_id, reason):
        print(f"omli.integrity: {pubkey_id} is unknown to the keyring or already revoked",
              file=sys.stderr)
        return 2
    print(f"revoked {pubkey_id}. Signatures by it are untrusted from this point (verify -> `revoked`).")
    if ring.active() is None:
        print("WARNING: no active key remains — rotate to a new key before trusting new seals.")
    print("commit + push the keyring")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return _main(argv)
    except (AnchorFileError, KeyringError, SignatureFileError) as exc:
        # A trust file that cannot be parsed: report it, never a traceback, never a pass.
        print(f"integrity: CORRUPT - {exc}")
        return 1
    except Exception as exc:
        # Any other ``Exception`` (an unreadable path, a directory, OSError, RecursionError,
        # MemoryError, ...) is reported the same way, by type name only. ``BaseException``
        # subclasses that are not ``Exception`` propagate.
        print(f"integrity: CORRUPT - {type(exc).__name__}")
        return 1


def _main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    no_anchor = "--no-anchor" in argv
    no_sign = "--no-sign" in argv
    argv = [a for a in argv if a not in ("--no-anchor", "--no-sign")]
    if not argv or argv[0] not in ("seal", "verify", "keygen", "rotate", "revoke"):
        print("usage: python -m omli.integrity {seal|verify|keygen|rotate|revoke} "
              "[--no-anchor] [--no-sign] [ARGS]", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]

    if cmd == "keygen":
        return _keygen(home_root())
    if cmd == "rotate":
        return _rotate(home_root())
    if cmd == "revoke":
        return _revoke(home_root(), rest)

    log, sealp, anchorp = _paths(rest)
    pubkeyp = home_root() / DEFAULT_PUBKEY
    keyringp = home_root() / DEFAULT_KEYRING

    if cmd == "seal":
        if not log.exists():
            print(f"omli.integrity: no log at {log}", file=sys.stderr)
            return 2
        # Check the key BEFORE writing: sealing and then failing to sign would leave a
        # seal and an anchor with no signature — which `verify` reports as `missing`
        # until someone re-seals. Refuse to start instead (`--no-sign` opts out).
        if not no_sign:
            problem = signing_preflight()
            if problem:
                print(f"omli.integrity: {problem}", file=sys.stderr)
                print("omli.integrity: refusing to seal - an unsigned seal under a "
                      "committed public key verifies as `missing`. Correct the above, or "
                      "pass --no-sign to seal unsigned on purpose.", file=sys.stderr)
                return 2
        # Stage-then-publish: seal to a temp path, produce the signature, then publish the
        # seal LAST via an atomic rename. A crash/kill after preflight therefore never leaves a
        # new seal without its signature. On a first seal it leaves no seal at all; on a re-seal
        # it can leave the earlier seal beside the new signature, which verifies as `invalid`,
        # so it fails closed.
        final_seal = sealp if sealp is not None else log.with_suffix(".seal.json")
        staged = final_seal.with_name(final_seal.name + ".staged")
        final_sig = final_seal.with_name(final_seal.stem + ".sig.json")
        res = seal(log, staged)
        sig = sign_head(res, sig_path=final_sig, pubkey_path=pubkeyp) if not no_sign else None
        os.replace(staged, final_seal)  # publish the seal atomically, last
        print(f"sealed {res.count} lines -> {final_seal}")
        print(f"head: {res.head[:16] or '(empty)'}")
        if not no_anchor:
            entry = anchor_head(res, anchorp)
            print(f"anchored head @ {entry.sealed_at} -> {anchorp}")
        if not no_sign:
            if sig is not None:
                print(f"signed head [{sig.pubkey_id}] -> {final_sig.name}")
            else:
                print("unsigned (no seal key found; run `keygen` and set OMLI_SEAL_KEY to sign)")
        print("commit + push the anchor and public-key files to pin them (the tool never runs git)")
        return 0

    report = verify(log, sealp)
    print(render(report))
    rc = 1 if report.status in ("tampered", "truncated", "corrupt") else 0
    if not no_anchor:
        anchor = verify_anchor(report.sealed_head, anchorp)
        print(f"anchor: {anchor.status.upper()} ({anchor.entries} anchored head(s))")
        if not anchor.trustworthy:
            rc = 1
    if not no_sign:
        sigp = (sealp if sealp is not None else log.with_suffix(".seal.json"))
        sigp = sigp.with_name(sigp.stem + ".sig.json")
        signature = verify_signature(report.sealed_head, report.sealed_count, sigp, pubkeyp,
                                     keyring_path=keyringp)
        detail = f" - {signature.detail}" if signature.detail else ""
        print(f"signature: {signature.status.upper()}{detail}")
        if not signature.trustworthy:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
