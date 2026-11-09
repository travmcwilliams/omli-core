"""Seal atomicity: no torn writes, and no published seal ever missing its signature.

`signing_preflight` refuses to START when the key is unusable, but it cannot cover a
crash / SIGKILL / disk-full AFTER the seal is written but before the signature is. These
tests pin the two guarantees that close that window: (1) integrity sidecars are written
atomically (temp + os.replace), so a reader never sees a torn file; (2) the CLI stages the
seal and publishes it LAST, so if signing dies mid-way NO seal is published — verify sees
the prior state, never the `missing` half-state.
"""

from __future__ import annotations

import json

import pytest

from omli.integrity.ledger import atomic_write_text

pytest.importorskip("cryptography")

from omli.integrity import __main__ as cli
from omli.integrity.signer import DEFAULT_PUBKEY, generate_keypair

PW = "correct horse battery staple"


def _log(tmp_path, lines=("alpha", "bravo", "charlie")):
    p = tmp_path / "artifacts.jsonl"
    p.write_text("".join(json.dumps({"x": l}) + "\n" for l in lines), encoding="utf-8")
    return p


# --- piece 1: atomic write --------------------------------------------------
def test_atomic_write_leaves_full_content_and_no_tmp(tmp_path):
    p = tmp_path / "sub" / "x.json"
    atomic_write_text(p, '{"a": 1}\n')                 # also creates the parent dir
    assert p.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert not (p.parent / "x.json.tmp").exists()      # temp cleaned up by the rename


# --- piece 2: stage-then-publish, seal published last -----------------------
def test_crash_during_signing_leaves_no_published_seal(tmp_path, monkeypatch):
    """If signing blows up after the seal is staged on a first seal (a stand-in for crash /
    disk-full mid sign), neither a seal nor a signature is published."""
    log = _log(tmp_path)
    priv = tmp_path / "keys" / "seal.key"
    generate_keypair(PW, priv_path=priv, pubkey_path=tmp_path / DEFAULT_PUBKEY)
    monkeypatch.setenv("OMLI_SEAL_KEY", str(priv))
    monkeypatch.setenv("OMLI_SEAL_PASSPHRASE", PW)
    monkeypatch.setattr(cli, "home_root", lambda: tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("signing crashed mid-write")

    monkeypatch.setattr(cli, "sign_head", _boom)

    sealp, anchorp = tmp_path / "out.seal.json", tmp_path / "heads.jsonl"
    # The CLI reports any failure as CORRUPT with exit 1 rather than a traceback.
    assert cli.main(["seal", str(log), str(sealp), str(anchorp)]) == 1
    assert not sealp.exists()                           # seal NOT published: no half-state
    assert not (tmp_path / "out.seal.sig.json").exists()


def test_successful_seal_publishes_seal_and_sig_with_no_residue(tmp_path, monkeypatch):
    """A successful seal publishes seal + signature, `verify` then exits 0, and no `.staged`
    / `.tmp` residue is left behind."""
    log = _log(tmp_path)
    priv = tmp_path / "keys" / "seal.key"
    generate_keypair(PW, priv_path=priv, pubkey_path=tmp_path / DEFAULT_PUBKEY)
    monkeypatch.setenv("OMLI_SEAL_KEY", str(priv))
    monkeypatch.setenv("OMLI_SEAL_PASSPHRASE", PW)
    monkeypatch.setattr(cli, "home_root", lambda: tmp_path)

    sealp, anchorp = tmp_path / "out.seal.json", tmp_path / "heads.jsonl"
    assert cli.main(["seal", str(log), str(sealp), str(anchorp)]) == 0
    assert sealp.exists() and (tmp_path / "out.seal.sig.json").exists()
    assert not (tmp_path / "out.seal.json.staged").exists()      # no staged residue
    assert not (tmp_path / "out.seal.json.tmp").exists()         # no temp residue
    assert cli.main(["verify", str(log), str(sealp), str(anchorp)]) == 0
