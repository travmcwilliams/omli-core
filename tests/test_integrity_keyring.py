"""Key rotation + revocation over the append-only keyring.

Two events a single key cannot answer: ROTATION (retire a key, move to a new one) and
REVOCATION (a key is compromised; its signatures must stop being trusted even though the
math still verifies). The keyring answers both as append-only data, and trust
is ACTIVE-KEY-ONLY — a retired or revoked key's signature is never trustworthy.

Unit tests exercise the derived status logic (no crypto); the rest drive real ed25519
rotation/revocation through the signer and the CLI ceremony.
"""

from __future__ import annotations

import json

import pytest

from omli.integrity.keyring import (
    ACTIVE,
    DEFAULT_KEYRING,
    RETIRED,
    REVOKED,
    UNKNOWN,
    KeyRecord,
    KeyRing,
)

PW = "correct horse battery staple"


def _log(tmp_path, lines=("alpha", "bravo", "charlie")):
    p = tmp_path / "artifacts.jsonl"
    p.write_text("".join(json.dumps({"x": l}) + "\n" for l in lines), encoding="utf-8")
    return p


# --- derived status logic (no crypto) ---------------------------------------
def test_status_derivation_across_rotation_and_revocation(tmp_path):
    ring = KeyRing(tmp_path / "keyring.jsonl")
    assert ring.active() is None and ring.status_of("id1") == UNKNOWN

    ring.append_key(KeyRecord("id1", "pem1", "t1"))
    assert ring.active().pubkey_id == "id1" and ring.status_of("id1") == ACTIVE

    ring.append_key(KeyRecord("id2", "pem2", "t2", supersedes="id1"))   # rotation
    assert ring.active().pubkey_id == "id2"
    assert ring.status_of("id1") == RETIRED and ring.status_of("id2") == ACTIVE

    ring.append_revocation("id2", "compromised")                        # revoke the active
    assert ring.status_of("id2") == REVOKED
    # No silent resurrection of the retired key: revoking the active leaves NO active key.
    assert ring.active() is None
    assert ring.status_of("id1") == RETIRED


def test_keyring_is_append_only_and_replays_from_disk(tmp_path):
    p = tmp_path / "keyring.jsonl"
    ring = KeyRing(p)
    ring.append_key(KeyRecord("id1", "pem1", "t1"))
    ring.append_key(KeyRecord("id2", "pem2", "t2", supersedes="id1"))
    ring.append_revocation("id1", "retired")

    reloaded = KeyRing(p)
    assert [k.pubkey_id for k in reloaded.records()] == ["id1", "id2"]
    assert reloaded.active().pubkey_id == "id2"
    assert reloaded.status_of("id1") == REVOKED
    # every line preserved, nothing rewritten
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert [l["type"] for l in lines] == ["key", "key", "revocation"]


def test_append_guards(tmp_path):
    ring = KeyRing(tmp_path / "k.jsonl")
    ring.append_key(KeyRecord("id1", "pem", "t"))
    with pytest.raises(ValueError, match="already registered"):
        ring.append_key(KeyRecord("id1", "pem2", "t2"))            # no restating a key
    assert ring.append_revocation("id1") is True
    assert ring.append_revocation("id1") is False                 # idempotent
    assert ring.append_revocation("nope") is False                # unknown id


def test_malformed_keyring_fails_loud(tmp_path):
    from omli.integrity.keyring import KeyringError

    p = tmp_path / "k.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    with pytest.raises(KeyringError):
        KeyRing(p).records()


# --- real ed25519 rotation / revocation -------------------------------------
def _keys(tmp_path):
    return tmp_path / "keyring.jsonl", tmp_path / "keys" / "seal.key", tmp_path / "omli.head.pub"


def test_rotation_retires_the_superseded_key_then_resigning_restores_trust(tmp_path):
    pytest.importorskip("cryptography")
    from omli.integrity.signer import generate_keypair, sign_head, verify_signature
    from omli.integrity.verifier import seal

    ring, priv, pub = _keys(tmp_path)
    log = _log(tmp_path)
    _, _, id1 = generate_keypair(PW, priv_path=priv, pubkey_path=pub,
                                 keyring_path=ring, reason="bootstrap")
    s = seal(log)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")
    sign_head(s, priv_path=priv, passphrase=PW)
    assert verify_signature(s.head, s.count, sigp, pub, keyring_path=ring).status == "signed"

    # rotate: a fresh active key; the superseded key is retired and its signature untrusted
    _, _, id2 = generate_keypair(PW, priv_path=priv, pubkey_path=pub, keyring_path=ring,
                                 supersedes=id1, reason="rotation", replace=True)
    assert id2 != id1
    retired = verify_signature(s.head, s.count, sigp, pub, keyring_path=ring)
    assert retired.status == "retired" and not retired.trustworthy

    # re-sign with the active key -> trusted again, stamped with the new key
    sign_head(s, priv_path=priv, passphrase=PW)
    resigned = verify_signature(s.head, s.count, sigp, pub, keyring_path=ring)
    assert resigned.status == "signed" and resigned.trustworthy and resigned.pubkey_id == id2


def test_revocation_untrusts_a_cryptographically_valid_signature(tmp_path):
    pytest.importorskip("cryptography")
    from omli.integrity.signer import generate_keypair, sign_head, verify_signature
    from omli.integrity.verifier import seal

    ring, priv, pub = _keys(tmp_path)
    log = _log(tmp_path)
    _, _, id1 = generate_keypair(PW, priv_path=priv, pubkey_path=pub,
                                 keyring_path=ring, reason="bootstrap")
    s = seal(log)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")
    sign_head(s, priv_path=priv, passphrase=PW)
    assert verify_signature(s.head, s.count, sigp, pub, keyring_path=ring).status == "signed"

    KeyRing(ring).append_revocation(id1, "compromised")               # the sig still verifies!
    revoked = verify_signature(s.head, s.count, sigp, pub, keyring_path=ring)
    assert revoked.status == "revoked" and not revoked.trustworthy


def test_no_keyring_falls_back_to_single_pubkey(tmp_path):
    """With a pubkey file and no keyring file, a signature verifies against that single key
    and is trustworthy."""
    pytest.importorskip("cryptography")
    from omli.integrity.signer import generate_keypair, sign_head, verify_signature
    from omli.integrity.verifier import seal

    priv, pub = tmp_path / "seal.key", tmp_path / "omli.head.pub"
    log = _log(tmp_path)
    generate_keypair(PW, priv_path=priv, pubkey_path=pub)             # no keyring_path
    s = seal(log)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")
    sign_head(s, priv_path=priv, passphrase=PW)
    got = verify_signature(s.head, s.count, sigp, pub, keyring_path=tmp_path / "absent.jsonl")
    assert got.status == "signed" and got.trustworthy


# --- CLI ceremony end to end ------------------------------------------------
def test_cli_lifecycle_keygen_seal_rotate_revoke(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from omli.integrity import __main__ as cli

    monkeypatch.setattr(cli, "home_root", lambda: tmp_path)
    priv = tmp_path / "keys" / "seal.key"
    monkeypatch.setenv("OMLI_SEAL_KEY", str(priv))
    monkeypatch.setenv("OMLI_SEAL_PASSPHRASE", PW)
    log = _log(tmp_path)
    sealp, anchorp = tmp_path / "a.seal.json", tmp_path / "heads.jsonl"
    ring = KeyRing(tmp_path / DEFAULT_KEYRING)

    # bootstrap -> seal -> verify SIGNED (rc 0)
    assert cli.main(["keygen"]) == 0
    id1 = ring.active().pubkey_id
    assert cli.main(["seal", str(log), str(sealp), str(anchorp)]) == 0
    assert cli.main(["verify", str(log), str(sealp), str(anchorp)]) == 0

    # rotate -> the un-re-sealed head is signed by a RETIRED key -> verify fails (rc 1)
    assert cli.main(["rotate"]) == 0
    id2 = ring.active().pubkey_id
    assert id2 != id1 and ring.status_of(id1) == RETIRED
    assert cli.main(["verify", str(log), str(sealp), str(anchorp)]) == 1

    # re-seal with the active key -> trusted again
    assert cli.main(["seal", str(log), str(sealp), str(anchorp)]) == 0
    assert cli.main(["verify", str(log), str(sealp), str(anchorp)]) == 0

    # revoke the active key -> verify fails, and no active key remains
    assert cli.main(["revoke", id2, "--reason", "compromised"]) == 0
    assert ring.active() is None
    assert cli.main(["verify", str(log), str(sealp), str(anchorp)]) == 1
