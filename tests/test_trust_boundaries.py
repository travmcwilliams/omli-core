"""Trust boundaries: integrity bytes, mail recipients, numeric gates, learned memory, frozen
records, credentials in messages, and the hosted adapter.

Every test docstring starts with its kind and then its topic in parentheses:

* REGRESSION   — reproduces an attack or a fail-open input and asserts the behaviour that
  refuses it (the right verdict, no send, no uncaught exception, no leaked value).
* PRESERVATION — pins a safe behaviour that sits next to those refusals, such as an input
  that passes or a related input the same check refuses.

No test here opens a socket.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from omli.agents.reflector import Consolidation, Lesson, MemoryStore
from omli.connections import secrets
from omli.connections.schema import load_registry
from omli.connections.scope_gate import ConnectionAction, connection_scope_gate
from omli.connections.sender import dispatch
from omli.events.bus import EventBus
from omli.gates.escalation import parse_signals
from omli.gates.post import PostGate
from omli.governance.findings import Finding, finding_to_row
from omli.integrity import seal, verify
from omli.integrity.__main__ import main as integrity_cli
from omli.integrity.keyring import DEFAULT_KEYRING
from omli.integrity.signer import DEFAULT_PUBKEY, PASSPHRASE_ENV, generate_keypair
from omli.models import ollama
from omli.models.errors import ModelError
from omli.models.provider import Message, ModelRequest, OpenAICompatibleProvider
from omli.packs.loader import load_pack
from test_provider import _hosted_entry
from test_score_threshold_gate import _SCORING_PACK, _CannedProvider, _agent, _seed_decision

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "omli" / "defaults" / "connection_registry.example.yaml"
PW = "throwaway-passphrase"
LINES = [json.dumps({"x": v}) for v in ("alpha", "bravo", "charlie")]


# =========================================================================== integrity
@pytest.fixture
def signed(tmp_path, monkeypatch):
    """A signed, anchored three-line log in a throwaway home with a throwaway key."""
    pytest.importorskip("cryptography")
    monkeypatch.setenv("OMLI_HOME", str(tmp_path))
    monkeypatch.setenv("OMLI_SEAL_KEY", str(tmp_path / "seal.key"))
    monkeypatch.setenv(PASSPHRASE_ENV, PW)
    generate_keypair(PW, tmp_path / "seal.key", tmp_path / DEFAULT_PUBKEY,
                     keyring_path=tmp_path / DEFAULT_KEYRING)
    log = tmp_path / "var" / "artifacts.jsonl"
    log.parent.mkdir()
    log.write_bytes(("\n".join(LINES) + "\n").encode("utf-8"))
    assert integrity_cli(["seal"]) == 0
    assert integrity_cli(["verify"]) == 0
    return {"home": tmp_path, "log": log, "seal": log.with_suffix(".seal.json"),
            "sig": log.with_name("artifacts.seal.sig.json")}


def _assert_rejected(signed, capsys, word):
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert word in capsys.readouterr().out


@pytest.mark.parametrize("body", [
    "\r\n".join(LINES) + "\r\n",                                # every line ending changed
    LINES[0] + "\n\n" + LINES[1] + "\n" + LINES[2] + "\n",      # a blank line inserted
    LINES[0] + "\n\f\n" + LINES[1] + "\n" + LINES[2] + "\n",    # a form-feed line inserted
    LINES[0] + "\n" + LINES[1] + "\r\n" + LINES[2] + "\n",      # one CR added
], ids=["crlf", "blank-line", "form-feed", "one-cr"])
def test_byte_level_edits_inside_the_sealed_prefix_are_detected(signed, capsys, body):
    """REGRESSION (integrity): the chain hashes exact bytes, so whitespace-only edits are tampering."""
    signed["log"].write_bytes(body.encode("utf-8"))
    report = verify(signed["log"], signed["seal"])
    assert report.status == "tampered" and not report.trustworthy
    _assert_rejected(signed, capsys, "TAMPERED")


def test_a_byte_order_mark_is_detected(signed, capsys):
    """PRESERVATION (integrity): a BOM added at the start of the log is detected as tampering."""
    signed["log"].write_bytes(b"\xef\xbb\xbf" + signed["log"].read_bytes())
    assert verify(signed["log"], signed["seal"]).status == "tampered"
    _assert_rejected(signed, capsys, "TAMPERED")


def test_records_appended_after_the_seal_read_as_appended(signed):
    """PRESERVATION (integrity): growth after the seal is reported as `appended`, not as tampering."""
    with signed["log"].open("ab") as fh:
        fh.write(b'{"x": "delta"}\n')
    report = verify(signed["log"], signed["seal"])
    assert report.status == "appended" and report.appended == 1


@pytest.mark.parametrize("which,content", [
    ("seal", "not json"), ("seal", "[1, 2]"), ("seal", '{"algo": "x"'),
    ("sig", "not json"), ("sig", "[]"),
    ("keyring", "not json\n"), ("keyring", '{"type": "key"}\n'),
    ("anchor", "not json\n"), ("anchor", '{"count": "3", "head": "h"}\n'),
])
def test_a_malformed_trust_file_is_a_clean_corrupt_report_with_exit_1(signed, capsys, which, content):
    """REGRESSION (integrity): a seal, signature, keyring or anchor file that cannot be parsed gives
    `CORRUPT` and exit 1, never an uncaught exception."""
    path = {"seal": signed["seal"], "sig": signed["sig"],
            "keyring": signed["home"] / DEFAULT_KEYRING,
            "anchor": signed["home"] / "integrity" / "artifacts.heads.jsonl"}[which]
    path.write_text(content, encoding="utf-8")
    _assert_rejected(signed, capsys, "CORRUPT")


@pytest.mark.parametrize("count", ["3", 3.0, True])
def test_a_seal_count_of_the_wrong_type_is_not_coerced(signed, count):
    """REGRESSION (integrity): the seal's count must be an integer; a string or float is not coerced."""
    data = json.loads(signed["seal"].read_text(encoding="utf-8"))
    data["count"] = count
    signed["seal"].write_text(json.dumps(data), encoding="utf-8")
    report = verify(signed["log"], signed["seal"])
    assert not report.trustworthy and report.status == "corrupt"


def test_a_seal_from_another_chain_format_is_not_trusted(tmp_path):
    """REGRESSION (integrity): a seal whose `algo` is not the chain format `verify` uses is reported corrupt."""
    log = tmp_path / "a.jsonl"
    log.write_bytes(b'{"x": 1}\n')
    s = seal(log)
    data = json.loads(s.seal.read_text(encoding="utf-8"))
    data["algo"] = "sha256"
    s.seal.write_text(json.dumps(data), encoding="utf-8")
    assert verify(log).status == "corrupt"


# =========================================================================== wires (mail)
def _mail(kind="smtp"):
    return next(c for c in load_registry(EXAMPLE).connections if c.target.kind == kind)


def _dispatch(path, payload=None, *, kind="smtp", sends_today=0, sender=None, credential="fake-credential"):
    connection = _mail(kind)
    calls = []

    def record(p, cred, conn, act):
        calls.append(p)
        return {"ok": True}

    action = ConnectionAction(connection.connection_id, "send-email", path=path,
                              sensitivity_tags=("public",), sends_today=sends_today)
    out = dispatch(connection, action, payload or {}, senders={kind: sender or record},
                   resolve=lambda *a, **k: credential)
    return out, calls


@pytest.mark.parametrize("address", [
    "victim@elsewhere.example.net@example.com",   # double @
    "victim%elsewhere.example.net@example.com",   # percent relay
    "elsewhere.example.net!victim@example.com",   # bang path
])
def test_a_relay_address_under_a_domain_glob_is_refused(address):
    """REGRESSION (wires): only one plain addr-spec per recipient; relay forms never reach the wire."""
    out, calls = _dispatch(address)
    assert out.ok is False and out.decision == "deny" and calls == []
    out, calls = _dispatch("ops@example.com", {"cc": [address]})
    assert out.ok is False and calls == []


@pytest.mark.parametrize("address", [
    '"a b"@example.com', "a(comment)@example.com", "@relay.example:a@example.com",
    "a@example.com,b@example.net", "a b@example.com",
])
def test_quoted_commented_routed_and_listed_addresses_are_refused(address):
    """PRESERVATION (wires): quotes, comments, routes, lists and spaces are refused."""
    out, calls = _dispatch(address)
    assert out.ok is False and calls == []


def test_a_forbidden_pattern_matches_regardless_of_case():
    """REGRESSION (wires): `*+noreply@*` is forbidden; an upper-case spelling must not dodge it."""
    out, calls = _dispatch("alerts+NOREPLY@example.com")
    assert (out.decision, calls) == ("deny", [])
    assert "forbidden-path" in out.reason


def test_an_allowed_address_matches_regardless_of_case():
    """REGRESSION (wires): address rows compare without regard to case, so a capitalised spelling
    of an allowed address is allowed."""
    out, calls = _dispatch("OPS@EXAMPLE.COM")
    assert out.ok is True and len(calls) == 1


def test_every_recipient_counts_against_the_daily_ceiling():
    """REGRESSION (wires): with 49 sent and a ceiling of 50, a message to two people is refused."""
    out, calls = _dispatch("ops@example.com", {"cc": ["owner@example.com"]}, sends_today=49)
    assert out.ok is False and calls == [] and "blast-radius-exceeded" in out.reason
    out, calls = _dispatch("ops@example.com", sends_today=49)
    assert out.ok is True and len(calls) == 1          # a single recipient fits


def test_a_ceiling_with_no_count_is_refused():
    """REGRESSION (wires): when max_sends_per_day is set, a send without sends_today is denied, at
    the gate and through dispatch."""
    connection = _mail()
    action = ConnectionAction(connection.connection_id, "send-email", path="ops@example.com",
                              sensitivity_tags=("public",))
    assert connection_scope_gate(action, connection).decision == "deny"
    out, calls = _dispatch("ops@example.com", sends_today=None)
    assert out.ok is False and calls == []


@pytest.mark.parametrize("error", [http.client.BadStatusLine("fake-credential"),
                                   http.client.IncompleteRead(b"fake-credential"),
                                   RuntimeError("fake-credential"), KeyError("fake-credential")])
def test_any_wire_exception_becomes_an_outcome_carrying_only_its_type(error):
    """REGRESSION (wires): an ``Exception`` a sender raises (here two ``http.client`` errors,
    RuntimeError and KeyError) comes back as an outcome that carries its type name and not its
    message."""
    def boom(*args):
        raise error

    out, _ = _dispatch("ops@example.com", sender=boom)
    assert out.ok is False and out.decision == "allow"
    assert out.reason == "wire-raised" and out.error_type == type(error).__name__
    assert "fake-credential" not in repr(out)


def test_a_sender_that_returns_no_dict_becomes_an_outcome():
    """REGRESSION (wires): a sender returning something other than a dict is a failure, not a crash."""
    out, _ = _dispatch("ops@example.com", sender=lambda *a: ["not", "a", "dict"])
    assert out.ok is False and out.decision == "allow"


def test_the_outcome_holds_no_form_of_a_credential_the_sender_echoes():
    """REGRESSION (wires): a sender result that echoes the credential raw, JSON-escaped and
    URL-encoded, in error, body, url, nested and key positions, leaves no form of it in the
    outcome, which reports only the failure code."""
    cred = "s3cr3t/value+with\"chars"

    def echo(payload, credential, connection, action):
        return {"ok": False, "error": f"bad {credential}",
                "body": json.dumps({"k": credential}),
                "url": "https://x.example/?t=" + quote(credential, safe=""),
                "nested": [{"v": credential}], credential: "as a key"}

    out, _ = _dispatch("ops@example.com", sender=echo, credential=cred)
    assert out.ok is False and out.reason == "wire-reported-failure"
    text = repr(out)
    for form in (cred, json.dumps(cred)[1:-1], quote(cred, safe="")):
        assert form not in text


# =========================================================================== gates
@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), 10 ** 400, True, "0.9"])
def test_post_gate_rejects_an_unusable_score_with_no_threshold(score):
    """REGRESSION (gates): a named score must be a finite number in range even with no threshold."""
    result = PostGate().check({"score": score}, score_field="score")
    assert result.passed is False and "schema_violation" in result.tags


@pytest.mark.parametrize("threshold", [float("nan"), float("-inf")])
def test_post_gate_refuses_a_non_finite_threshold(threshold):
    """REGRESSION (gates): a NaN or minus-infinity threshold must not disable the floor."""
    assert PostGate().check({"score": 0.0}, score_field="score", threshold=threshold).passed is False


def test_a_huge_integer_score_is_a_clean_rejection():
    """REGRESSION (gates): a score of 10**400 is rejected, not raised as OverflowError."""
    result = PostGate().check({"score": 10 ** 400}, score_field="score", threshold=0.5)
    assert result.passed is False


def test_parse_signals_does_not_raise_on_a_huge_integer():
    """REGRESSION (gates): a 400-digit confidence is not a usable number; it is not raised either."""
    signals = parse_signals('{"confidence": ' + "9" * 400 + "}")
    assert signals.confidence is None


def _pack_file(tmp_path, *, scoring=True, min_confidence=None, threshold=None):
    data = yaml.safe_load(_SCORING_PACK.read_text(encoding="utf-8"))
    if not scoring:
        data.pop("scoring", None)
        data["required_output_fields"] = ["text"]
    if threshold is not None:
        data["scoring"]["threshold"] = threshold
    if min_confidence is not None:
        data["thresholds"] = {"min_confidence": min_confidence}
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("where", ["scoring", "min_confidence"])
@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_the_pack_loader_rejects_a_non_finite_threshold(tmp_path, where, value):
    """REGRESSION (gates): YAML `.nan` / `.inf` thresholds are refused at load."""
    path = _pack_file(tmp_path, min_confidence=0.5)
    text = path.read_text(encoding="utf-8")
    if where == "scoring":
        text = text.replace("threshold: 0.6", f"threshold: {value}")
    else:
        text = text.replace("min_confidence: 0.5", f"min_confidence: {value}")
    assert value in text
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_pack(path)


def _run(tmp_path, pack_path, output):
    from omli.artifacts.store import ArtifactStore

    store = ArtifactStore.from_log(tmp_path / "a.jsonl")
    decision = _seed_decision(store)
    out = _agent(load_pack(pack_path), _CannedProvider(output), store).run(decision, "WorkResult")
    return out, store.by_kind("WorkResult")


@pytest.mark.parametrize("confidence", ["NaN", "Infinity", "-Infinity", "9" * 400, '"high"', "true"])
def test_an_unusable_confidence_is_rejected_when_a_floor_is_set(tmp_path, confidence):
    """REGRESSION (gates): with min_confidence set, a reported confidence that is not a finite
    number writes nothing; it is not read as "no confidence reported"."""
    pack = _pack_file(tmp_path, scoring=False, min_confidence=0.5)
    out, written = _run(tmp_path, pack, '{"text": "t", "confidence": ' + confidence + "}")
    assert out is None and written == ()


def test_an_absent_confidence_passes(tmp_path):
    """PRESERVATION (gates): a plain output that reports no confidence is written."""
    pack = _pack_file(tmp_path, scoring=False, min_confidence=0.5)
    out, written = _run(tmp_path, pack, '{"text": "t"}')
    assert out is not None and len(written) == 1


def test_both_floors_apply_when_a_pack_declares_scoring_and_min_confidence(tmp_path):
    """REGRESSION (gates): a high score does not excuse a confidence below min_confidence."""
    pack = _pack_file(tmp_path, min_confidence=0.9)
    out, written = _run(tmp_path, pack, '{"text": "t", "score": 0.95, "confidence": 0.1}')
    assert out is None and written == ()
    (tmp_path / "ok").mkdir()
    out, written = _run(tmp_path / "ok", pack, '{"text": "t", "score": 0.95, "confidence": 0.95}')
    assert out is not None and len(written) == 1           # clearing both floors writes


def test_a_huge_integer_through_the_agent_writes_nothing_and_does_not_raise(tmp_path):
    """REGRESSION (gates): `Agent.run` returns None for a 400-digit score; it does not raise."""
    out, written = _run(tmp_path, _SCORING_PACK, '{"text": "t", "score": ' + "9" * 400 + "}")
    assert out is None and written == ()


# =========================================================================== memory (Primitive 5)
def test_a_duplicate_lesson_id_is_refused_at_append():
    """REGRESSION (memory): a new lesson cannot reuse an approved lesson's id and inherit its approval."""
    store = MemoryStore()
    a = store.append(Lesson(text="approved text", source_artifact_ids=()))
    store.approve(a.id)
    with pytest.raises(ValueError):
        store.append(Lesson(text="UNAPPROVED", source_artifact_ids=(), id=a.id))
    made = store.append_consolidation(Consolidation(text="", supersedes=(a.id,)))
    assert "UNAPPROVED" not in made.text


def test_a_duplicate_lesson_id_is_refused_at_load(tmp_path):
    """REGRESSION (memory): a log holding two records with one id does not load."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a = store.append(Lesson(text="approved text", source_artifact_ids=()))
    store.approve(a.id)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(Lesson(text="UNAPPROVED", source_artifact_ids=(), id=a.id).to_dict()) + "\n")
    with pytest.raises(ValueError):
        MemoryStore(log)


def test_a_rejection_after_an_approval_withdraws_it(tmp_path):
    """REGRESSION (memory): the latest decision wins; a withdrawn lesson cannot be consolidated, and a
    consolidation already over it stops being in effect. Survives a reload."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a = store.append(Lesson(text="first", source_artifact_ids=()))
    b = store.append(Lesson(text="second", source_artifact_ids=()))
    store.approve(a.id)
    store.approve(b.id)
    live = store.append_consolidation(Consolidation(text="", supersedes=(a.id, b.id)))
    assert store.live_consolidations() == (live,)
    store.reject(a.id)
    for view in (store, MemoryStore(log)):
        assert a.id not in {l.id for l in view.approved()}
        assert view.live_consolidations() == ()
        with pytest.raises(ValueError):
            view.append_consolidation(Consolidation(text="", supersedes=(a.id,)))


def test_an_approval_after_a_rejection_restores_it():
    """PRESERVATION (memory): approving again after a rejection makes the lesson approved again."""
    store = MemoryStore()
    a = store.append(Lesson(text="t", source_artifact_ids=()))
    store.reject(a.id)
    store.approve(a.id)
    assert a.id in {l.id for l in store.approved()}


def test_editing_a_lesson_and_its_consolidation_together_puts_nothing_new_in_effect(tmp_path):
    """REGRESSION (memory): an approval binds to the approved text, so rewriting both log lines
    withdraws the approval instead of putting the edited text into effect."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a = store.append(Lesson(text="approved text", source_artifact_ids=()))
    store.approve(a.id)
    store.append_consolidation(Consolidation(text="", supersedes=(a.id,)))
    raw = log.read_text(encoding="utf-8").replace("approved text", "smuggled text")
    log.write_text(raw, encoding="utf-8")
    reloaded = MemoryStore(log)
    assert reloaded.live_consolidations() == ()
    assert all("smuggled" not in getattr(x, "text", "") for x in reloaded.active())


def test_promotion_reads_approval_from_the_store_not_from_the_lesson(tmp_path):
    """REGRESSION (memory): promotion reads approval from the store. A lesson the store holds but
    never approved, or whose approval a later rejection withdrew, writes no draft; the same
    lesson once approved does."""
    from omli.packs.promotion import draft_staging_pack

    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({"name": "n", "version": "1.0.0", "identity": "i",
                                    "prompt_template": "{input}"}), encoding="utf-8")
    memory = MemoryStore()
    lesson = memory.append(Lesson(text="a lesson", source_artifact_ids=()))
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="not approved in the memory store"):
        draft_staging_pack(base, memory, lesson.id, promoted_by="human", out_dir=out)
    memory.approve(lesson.id)
    memory.reject(lesson.id)
    with pytest.raises(ValueError, match="not approved in the memory store"):
        draft_staging_pack(base, memory, lesson.id, promoted_by="human", out_dir=out)
    assert not out.exists() or list(out.iterdir()) == []
    memory.approve(lesson.id)
    assert draft_staging_pack(base, memory, lesson.id, promoted_by="human", out_dir=out).exists()


# =========================================================================== freeze (frozen records)
def test_an_event_payload_is_deeply_frozen_and_copied():
    """REGRESSION (freeze): nested event payloads cannot be changed by a subscriber or the emitter."""
    nested = {"refs": ["a"], "inner": {"k": 1}}
    event = EventBus().emit("custom", {"data": nested})
    with pytest.raises((TypeError, AttributeError)):
        event.payload["data"]["inner"]["k"] = 2
    with pytest.raises((TypeError, AttributeError)):
        event.payload["data"]["refs"].append("b")
    nested["inner"]["k"] = 3
    nested["refs"].append("c")
    assert event.payload["data"]["inner"]["k"] == 1 and list(event.payload["data"]["refs"]) == ["a"]


def test_a_finding_evidence_is_deeply_frozen_and_copied():
    """REGRESSION (freeze): nested finding evidence cannot be changed after the finding is made."""
    evidence = {"scores": [0.1], "detail": {"n": 1}}
    finding = Finding(kind="model_evaluation", model_id="m", registry_version="1", summary="s",
                      evidence=evidence)
    with pytest.raises((TypeError, AttributeError)):
        finding.evidence["detail"]["n"] = 2
    evidence["detail"]["n"] = 3
    evidence["scores"].append(0.9)
    assert finding.evidence["detail"]["n"] == 1 and len(finding.evidence["scores"]) == 1


def test_a_finding_row_is_plain_json():
    """PRESERVATION (freeze): the stored row of a finding is the same plain JSON as its input."""
    evidence = {"scores": [0.1, 0.2], "detail": {"n": 1}}
    finding = Finding(kind="model_evaluation", model_id="m", registry_version="1", summary="s",
                      evidence=evidence)
    assert json.dumps(finding_to_row(finding)["evidence"]) == json.dumps(evidence)


# =========================================================================== secrets (credentials in errors)
VALUE = "pasted_credential_value_123"  # a valid path segment, so the reference loads


def test_a_value_pasted_after_the_prefix_is_not_in_the_missing_secret_error(monkeypatch):
    """REGRESSION (secrets): neither the reference nor the variable name derived from it is repeated."""
    monkeypatch.delenv("OMLI_SECRETS_DIR", raising=False)
    monkeypatch.delenv("OMLI_HOME", raising=False)
    with pytest.raises(secrets.SecretNotFound) as caught:
        secrets.resolve_secret(f"secret://{VALUE}", required=True)
    assert VALUE.lower() not in str(caught.value).lower()


def test_a_value_pasted_after_the_prefix_is_not_in_the_dispatch_outcome(tmp_path, monkeypatch):
    """REGRESSION (secrets): a registry whose credential_ref holds a pasted value loads (the shape is
    valid), and the resolver's refusal does not carry that value into `SenderOutcome.reason`."""
    monkeypatch.delenv("OMLI_SECRETS_DIR", raising=False)
    monkeypatch.delenv("OMLI_HOME", raising=False)
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    entry = next(c for c in data["connections"] if c["target"]["kind"] == "smtp")
    entry["target"]["credential_ref"] = f"secret://{VALUE}"
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    connection = load_registry(path).get(entry["connection_id"])
    calls = []
    action = ConnectionAction(connection.connection_id, "send-email", path="ops@example.com",
                              sensitivity_tags=("public",), sends_today=0)
    out = dispatch(connection, action, {}, senders={"smtp": lambda *a: calls.append(1) or {"ok": True}})
    assert out.ok is False and out.decision == "allow" and calls == []   # it got as far as the credential
    assert VALUE.lower() not in repr(out).lower()


def _link_outside(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return True
    except OSError:
        if os.name != "nt":
            return False
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    return done.returncode == 0


def test_a_reference_that_escapes_the_store_is_not_in_the_error(tmp_path, monkeypatch):
    """REGRESSION (secrets): the escaping-the-store refusal does not repeat the reference."""
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    store.mkdir()
    outside.mkdir()
    (outside / VALUE).write_text("x", encoding="utf-8")
    if not _link_outside(store / "door", outside):
        pytest.skip("cannot create a symlink or junction here")
    monkeypatch.setenv("OMLI_SECRETS_DIR", str(store))
    with pytest.raises(ValueError) as caught:
        secrets.resolve_secret(f"secret://door/{VALUE}")
    assert VALUE not in str(caught.value)


# =========================================================================== hosted adapter
KEY = "k3y/with+odd=chars"


def _keyed(monkeypatch):
    monkeypatch.setenv("TEST_HOSTED_MODEL_KEY", KEY)
    return dataclasses.replace(_hosted_entry(), api_key_env="TEST_HOSTED_MODEL_KEY")


def _request():
    return ModelRequest(messages=(Message("user", "hello"),))


@pytest.mark.parametrize("encode", [
    lambda k: json.dumps(k)[1:-1].replace("/", "\\/"),           # JSON with escaped slash
    lambda k: "".join(f"\\u{ord(c):04x}" for c in k),             # every char \\u-escaped
    lambda k: k[:3] + "".join(f"\\u{ord(c):04x}" for c in k[3:]),  # partly escaped
    lambda k: quote(k, safe=""),                                   # URL-encoded
    lambda k: quote(k, safe="").lower(),                           # URL-encoded, lower-case hex
], ids=["json-slash", "json-unicode", "json-partial", "url", "url-lower"])
def test_an_encoded_echo_of_the_key_is_not_in_the_response(monkeypatch, encode):
    """REGRESSION (hosted): the key is scrubbed in the encodings a response can carry it in."""
    def transport(url, body, timeout, headers=None):
        content = "your key is " + encode(KEY)
        return ('{"model": "m", "choices": [{"message": {"content": "' + content + '"}}]}').encode("utf-8")

    response = OpenAICompatibleProvider(transport=transport).invoke(_keyed(monkeypatch), _request())
    text = repr(response)
    assert KEY not in text and quote(KEY, safe="") not in text and quote(KEY, safe="").lower() not in text


def _chain(exc):
    seen, todo = [], [exc]
    while todo:
        e = todo.pop()
        if e is None or any(e is s for s in seen):
            continue
        seen.append(e)
        todo += [e.__cause__, e.__context__]
    return seen


@pytest.mark.parametrize("error", [OSError(f"refused {KEY}"), TimeoutError(f"slow {KEY}"),
                                   ConnectionResetError(f"reset {KEY}"), ValueError(f"bad {KEY}")])
def test_no_exception_reachable_from_the_raised_error_holds_the_key(monkeypatch, error):
    """REGRESSION (hosted): neither __context__ nor __cause__ (at any depth) holds the key."""
    def transport(url, body, timeout, headers=None):
        raise error

    with pytest.raises(ModelError) as caught:
        OpenAICompatibleProvider(transport=transport).invoke(_keyed(monkeypatch), _request())
    chain = _chain(caught.value)
    assert all(KEY not in repr(e) and KEY not in str(e) for e in chain)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def _redirect_handler():
    return next(h for h in ollama._OPENER.handlers
                if isinstance(h, urllib.request.HTTPRedirectHandler))


def test_a_redirect_to_another_host_does_not_carry_the_authorization_header():
    """REGRESSION (hosted): the opener the hosted adapter uses will not hand the key to another host."""
    # urllib follows a 302 on a POST (as a GET) and, by default, copies Authorization along.
    request = urllib.request.Request("https://api.example.com/v1/chat/completions", data=b"{}",
                                     headers={"Authorization": f"Bearer {KEY}"})
    for target in ("https://elsewhere.example.net/v1/x", "http://api.example.com/v1/x",
                   "https://api.example.com:8443/v1/x"):
        followed = _redirect_handler().redirect_request(request, None, 302, "Found", {}, target)
        assert followed is None or "Authorization" not in dict(followed.header_items())


def test_a_redirect_on_the_same_host_is_followed():
    """PRESERVATION (hosted): a same-origin redirect is followed to its target."""
    request = urllib.request.Request("https://api.example.com/v1/a", headers={"X-Test": "1"})
    followed = _redirect_handler().redirect_request(request, None, 302, "Found", {},
                                                    "https://api.example.com/v1/b")
    assert followed is not None and followed.full_url == "https://api.example.com/v1/b"
