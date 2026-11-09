"""Tests for ed25519 head signing (the durable trust anchor).

Proves the durable half of the seal: a signature over the sealed head that a re-sealer
cannot forge without the off-repo private key. Covers the key ceremony, the sign/verify
round trip, and every fail-closed status — the end-to-end attack (edit + re-seal) is
``invalid``; deleting the sig under a committed key is ``missing``; both are not
trustworthy. Uses ephemeral keys in tmp_path; needs the optional ``cryptography`` dep.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")

from omli.integrity.signer import (
    DEFAULT_PUBKEY,
    generate_keypair,
    read_signature,
    sign_head,
    signing_preflight,
    verify_signature,
)
from omli.integrity.verifier import seal


PW = "correct horse battery staple"


def _log(tmp_path, lines=("alpha", "bravo", "charlie")):
    p = tmp_path / "artifacts.jsonl"
    p.write_text("".join(json.dumps({"x": l}) + "\n" for l in lines), encoding="utf-8")
    return p


def _keys(tmp_path):
    priv = tmp_path / "keys" / "seal.key"
    pub = tmp_path / "integrity" / "omli.head.pub"
    generate_keypair(PW, priv_path=priv, pubkey_path=pub)
    return priv, pub


# --- key ceremony ----------------------------------------------------------
def test_keygen_writes_encrypted_private_and_public(tmp_path):
    priv, pub, pubkey_id = generate_keypair(
        PW, priv_path=tmp_path / "seal.key", pubkey_path=tmp_path / "omli.head.pub")
    assert priv.exists() and pub.exists()
    assert len(pubkey_id) == 16
    assert b"ENCRYPTED" in priv.read_bytes()          # passphrase-protected on disk
    assert b"PUBLIC KEY" in pub.read_bytes()


def test_keygen_refuses_to_clobber_existing_key(tmp_path):
    priv = tmp_path / "seal.key"
    generate_keypair(PW, priv_path=priv, pubkey_path=tmp_path / "a.pub")
    with pytest.raises(FileExistsError):
        generate_keypair(PW, priv_path=priv, pubkey_path=tmp_path / "b.pub")


def test_keygen_requires_passphrase(tmp_path):
    with pytest.raises(ValueError):
        generate_keypair("", priv_path=tmp_path / "seal.key", pubkey_path=tmp_path / "a.pub")


# --- sign / verify round trip ----------------------------------------------
def test_sign_then_verify_is_signed(tmp_path):
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    s = seal(log)
    rep = sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)
    assert rep.status == "signed" and rep.trustworthy

    sigp = s.seal.with_name(s.seal.stem + ".sig.json")
    stored = read_signature(sigp)
    assert stored["head"] == s.head and stored["algo"] == "ed25519"

    got = verify_signature(s.head, s.count, sigp, pub)
    assert got.status == "signed" and got.trustworthy
    assert got.pubkey_id == rep.pubkey_id


def test_reseal_after_edit_is_invalid(tmp_path):
    """The re-seal attack: edit the log, re-seal — but the re-sealed head can't be signed
    without the private key, and the existing signature does not verify against it."""
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    s = seal(log)
    sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")

    lines = log.read_text(encoding="utf-8").splitlines()
    lines[1] = json.dumps({"x": "FORGED"})
    log.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    attacker = seal(log)                              # re-seal to look innocent (no key!)

    got = verify_signature(attacker.head, attacker.count, sigp, pub)
    assert got.status == "invalid" and not got.trustworthy


def test_deleting_signature_under_committed_key_is_missing(tmp_path):
    """Deleting the sidecar must not downgrade to a silent pass: a committed public key
    means signing is expected, so an absent signature is `missing` (not trustworthy)."""
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    s = seal(log)
    sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")
    assert verify_signature(s.head, s.count, sigp, pub).status == "signed"   # it exists
    sigp.unlink()                                                            # then delete it
    got = verify_signature(s.head, s.count, sigp, pub)
    assert got.status == "missing" and not got.trustworthy


def test_present_signature_with_backend_absent_is_unverifiable(tmp_path, monkeypatch):
    """A fail-closed branch: a real signature exists and
    the public key is committed, but the `cryptography` backend is gone at verify time
    (dep uninstalled). It must classify `unverifiable` — never a silent pass — and not be
    trustworthy, while still surfacing the claimed pubkey_id from the sidecar."""
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    s = seal(log)
    sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)   # real sig, backend present
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")

    monkeypatch.setattr("omli.integrity.signer._backend", lambda: None)  # backend unavailable
    got = verify_signature(s.head, s.count, sigp, pub)
    assert got.status == "unverifiable" and not got.trustworthy
    assert got.pubkey_id is not None                  # still reports which key was claimed


def test_reseal_after_truncate_is_invalid(tmp_path):
    """Truncation is a distinct attack from editing (which test_reseal_after_edit covers):
    cut the tail, re-seal the shorter log so the sidecar reads `ok`. The signature covers
    the full (head, count) and cannot be reforged without the off-repo key."""
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    s = seal(log)
    sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")

    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("".join(l + "\n" for l in lines[:-1]), encoding="utf-8")  # drop tail line
    attacker = seal(log)                              # re-seal the truncated log (no key!)

    got = verify_signature(attacker.head, attacker.count, sigp, pub)
    assert got.status == "invalid" and not got.trustworthy


def test_no_committed_key_and_no_signature_is_unsigned(tmp_path):
    log = _log(tmp_path)
    s = seal(log)
    got = verify_signature(s.head, s.count, tmp_path / "nope.sig.json",
                           tmp_path / "absent.pub")
    assert got.status == "unsigned" and got.trustworthy


def test_signature_without_committed_key_is_no_pubkey(tmp_path):
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    s = seal(log)
    sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")

    got = verify_signature(s.head, s.count, sigp, tmp_path / "absent.pub")
    assert got.status == "no-pubkey" and not got.trustworthy


# --- signing-side guards ---------------------------------------------------
def test_sign_returns_none_without_a_key(tmp_path):
    log = _log(tmp_path)
    s = seal(log)
    assert sign_head(s, priv_path=tmp_path / "absent.key") is None


def test_sign_requires_passphrase_when_key_present(tmp_path, monkeypatch):
    log = _log(tmp_path)
    priv, pub = _keys(tmp_path)
    monkeypatch.delenv("OMLI_SEAL_PASSPHRASE", raising=False)
    s = seal(log)
    with pytest.raises(ValueError):
        sign_head(s, priv_path=priv, pubkey_path=pub)


def test_empty_log_signs_and_verifies(tmp_path):
    log = tmp_path / "artifacts.jsonl"
    log.write_text("", encoding="utf-8")
    priv, pub = _keys(tmp_path)
    s = seal(log)
    sign_head(s, priv_path=priv, passphrase=PW, pubkey_path=pub)
    sigp = s.seal.with_name(s.seal.stem + ".sig.json")
    assert verify_signature(s.head, s.count, sigp, pub).status == "signed"


def test_malformed_signature_file_fails_loud(tmp_path):
    from omli.integrity.signer import SignatureFileError

    p = tmp_path / "bad.sig.json"
    p.write_text("not json\n", encoding="utf-8")
    with pytest.raises(SignatureFileError):
        read_signature(p)
    got = verify_signature("h", 1, p, tmp_path / "no.pub")
    assert got.status == "corrupt" and not got.trustworthy


# --- preflight: catch an unusable key before anything is written ------------
def test_preflight_passes_with_no_key_and_with_a_usable_key(tmp_path):
    assert signing_preflight(priv_path=tmp_path / "absent.key") is None   # unsigned is allowed
    priv, _ = _keys(tmp_path)
    assert signing_preflight(priv_path=priv, passphrase=PW) is None


def test_preflight_reports_missing_passphrase(tmp_path, monkeypatch):
    priv, _ = _keys(tmp_path)
    monkeypatch.delenv("OMLI_SEAL_PASSPHRASE", raising=False)
    problem = signing_preflight(priv_path=priv)
    assert problem and "OMLI_SEAL_PASSPHRASE" in problem


def test_cli_seal_refuses_rather_than_leaving_an_unsigned_half_state(tmp_path, monkeypatch):
    """The post-ceremony trap: key present, passphrase forgotten. Sealing first and
    failing to sign would leave a seal + anchor with no signature — `missing` forever.
    The CLI must write nothing and exit non-zero instead."""
    from omli.integrity.__main__ import main

    log = _log(tmp_path)
    priv, _ = _keys(tmp_path)
    monkeypatch.setenv("OMLI_SEAL_KEY", str(priv))
    monkeypatch.delenv("OMLI_SEAL_PASSPHRASE", raising=False)

    sealp, anchorp = tmp_path / "out.seal.json", tmp_path / "heads.jsonl"
    assert main(["seal", str(log), str(sealp), str(anchorp)]) == 2
    assert not sealp.exists() and not anchorp.exists()   # nothing written: no half-state


def test_cli_seal_signs_and_verify_accepts_it(tmp_path, monkeypatch):
    """The happy path the ceremony unlocks: seal signs, verify exits 0 on SIGNED."""
    from omli.integrity.__main__ import main

    log = _log(tmp_path)
    priv = tmp_path / "keys" / "seal.key"
    # the CLI reads the public key from home_root()/DEFAULT_PUBKEY — put it there
    generate_keypair(PW, priv_path=priv, pubkey_path=tmp_path / DEFAULT_PUBKEY)
    monkeypatch.setenv("OMLI_SEAL_KEY", str(priv))
    monkeypatch.setenv("OMLI_SEAL_PASSPHRASE", PW)
    monkeypatch.setattr("omli.integrity.__main__.home_root", lambda: tmp_path)

    sealp, anchorp = tmp_path / "out.seal.json", tmp_path / "heads.jsonl"
    assert main(["seal", str(log), str(sealp), str(anchorp)]) == 0
    assert (tmp_path / "out.seal.sig.json").exists()
    assert main(["verify", str(log), str(sealp), str(anchorp)]) == 0


def test_cli_no_sign_opts_out_deliberately(tmp_path, monkeypatch):
    """--no-sign is the deliberate escape hatch: seals unsigned even with a key present."""
    from omli.integrity.__main__ import main

    log = _log(tmp_path)
    priv, _ = _keys(tmp_path)
    monkeypatch.setenv("OMLI_SEAL_KEY", str(priv))
    monkeypatch.delenv("OMLI_SEAL_PASSPHRASE", raising=False)

    sealp, anchorp = tmp_path / "out.seal.json", tmp_path / "heads.jsonl"
    assert main(["seal", "--no-sign", str(log), str(sealp), str(anchorp)]) == 0
    assert sealp.exists() and not (tmp_path / "out.seal.sig.json").exists()
