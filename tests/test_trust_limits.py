"""The written trust boundary: what each boundary catches, and what it reports.

Every test docstring starts with its kind:

* REGRESSION   — an input the code must refuse or report in a fixed way.
* PRESERVATION — behaviour that must stay as it is.

No test here opens a socket, and no test touches a key outside its temporary directory.
"""

from __future__ import annotations

import dataclasses
import json
import math

import pytest
import yaml

from omli.agents.reflector import Lesson, MemoryStore
from omli.connections.scope_gate import ConnectionAction
from omli.connections.sender import dispatch
from omli.gates.traffic import HardwareState, TrafficGuard, parse_traffic_policy
from omli.integrity.__main__ import main as integrity_cli
from omli.integrity.keyring import DEFAULT_KEYRING
from omli.integrity.signer import DEFAULT_PUBKEY, verify_signature
from omli.models.errors import ModelError
from omli.models.provider import resolve_api_key
from omli.models.registry import parse_registry as parse_models
from test_fail_closed import MODELS, TRAFFIC, _models_with
from test_trust_boundaries import _keyed, _mail, signed  # noqa: F401 (fixture)

GARBAGE_PEM = "-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n"


# =========================================================================== integrity
def _keyring_lines(signed):
    path = signed["home"] / DEFAULT_KEYRING
    return path, [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_an_unparseable_keyring_pem_reads_corrupt(signed, capsys):
    """REGRESSION (integrity): a keyring record whose public-key PEM cannot be parsed gives
    `corrupt` and exit 1, not `invalid`."""
    path, records = _keyring_lines(signed)
    records[0]["pubkey_pem"] = GARBAGE_PEM
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    seal = json.loads(signed["seal"].read_text(encoding="utf-8"))
    report = verify_signature(seal["head"], seal["count"], signed["sig"],
                              signed["home"] / DEFAULT_PUBKEY, keyring_path=path)
    assert report.status == "corrupt" and not report.trustworthy
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "signature: CORRUPT" in capsys.readouterr().out


def test_an_unparseable_public_key_file_reads_corrupt(signed, capsys):
    """REGRESSION (integrity): with no keyring, a public-key file that cannot be parsed gives
    `corrupt` and exit 1, not `invalid`."""
    (signed["home"] / DEFAULT_KEYRING).unlink()
    (signed["home"] / DEFAULT_PUBKEY).write_text(GARBAGE_PEM, encoding="utf-8")
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "signature: CORRUPT" in capsys.readouterr().out


def test_a_signature_that_does_not_verify_still_reads_invalid(signed, capsys):
    """PRESERVATION (integrity): a parseable key with a signature that does not match the head
    is still `invalid`, exit 1."""
    sig = json.loads(signed["sig"].read_text(encoding="utf-8"))
    sig["sig"] = "A" * len(sig["sig"])
    signed["sig"].write_text(json.dumps(sig), encoding="utf-8")
    capsys.readouterr()
    assert integrity_cli(["verify"]) == 1
    assert "signature: INVALID" in capsys.readouterr().out


# =========================================================================== wires
def test_a_base_exception_is_outside_the_boundary():
    """PRESERVATION (wires): the boundary catches Exception, not BaseException. A
    KeyboardInterrupt raised by the wire propagates out of `dispatch`."""
    connection = _mail("smtp")
    action = ConnectionAction(connection.connection_id, "send-email", path="ops@example.com",
                              sensitivity_tags=("public",), sends_today=0)

    def interrupt(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        dispatch(connection, action, {}, senders={"smtp": interrupt}, resolve=lambda *a, **k: "x")


# =========================================================================== hosted
@pytest.mark.parametrize("name", ["OMLI_HOME", "OMLI_SEAL_PASSPHRASE", "OMLI_SEAL_KEY",
                                  "OMLI_SECRETS_DIR", "OMLI_SECRET_ANY_REF", "OMLI_ANYTHING"])
def test_api_key_env_refuses_omlis_own_variables(name):
    """REGRESSION (hosted): `api_key_env` may not name a variable with OMLi's own OMLI_ prefix,
    and the error does not repeat the name."""
    with pytest.raises(ValueError) as caught:
        parse_models(_models_with(name))
    assert name not in str(caught.value) and "api_key_env" in str(caught.value)


def test_resolve_refuses_omlis_own_variables_on_a_hand_built_entry(monkeypatch):
    """REGRESSION (hosted): an entry built outside the registry cannot read the seal passphrase
    as an API key; the error does not repeat the name, and nothing is read from it."""
    monkeypatch.setenv("OMLI_SEAL_PASSPHRASE", "passphrase-value")
    entry = dataclasses.replace(_keyed(monkeypatch), api_key_env="OMLI_SEAL_PASSPHRASE")
    with pytest.raises(ModelError) as caught:
        resolve_api_key(entry)
    assert "OMLI_SEAL_PASSPHRASE" not in str(caught.value)
    assert "passphrase-value" not in str(caught.value)


def test_a_name_outside_the_omli_prefix_is_accepted(monkeypatch):
    """PRESERVATION (hosted): an ordinary variable name outside the OMLI_ prefix is accepted
    and read."""
    entry = parse_models(_models_with("HOSTED_MODEL_API_KEY")).get("hosted-burst")
    monkeypatch.setenv("HOSTED_MODEL_API_KEY", "value-from-env")
    assert resolve_api_key(entry) == "value-from-env"


# =========================================================================== traffic
def _guard(state):
    policy = parse_traffic_policy(yaml.safe_load(TRAFFIC.read_text(encoding="utf-8")))
    return TrafficGuard(policy, probe=lambda: state)


@pytest.mark.parametrize("state", [
    HardwareState(cpu_percent=math.nan, ram_available_mb=8192.0, inflight=0),
    HardwareState(cpu_percent=10.0, ram_available_mb=math.inf, inflight=0),
    HardwareState(cpu_percent=10.0, ram_available_mb=8192.0, vram_available_mb=-math.inf, inflight=0),
], ids=["nan-cpu", "inf-ram", "-inf-vram"])
@pytest.mark.parametrize("priority", ["bulk", "normal", "interactive"])
def test_a_non_finite_probe_reading_fails_closed(state, priority):
    """REGRESSION (traffic): a NaN or infinite reading from the probe refuses admission
    (`defer`) for every priority below the protected class."""
    decision = _guard(state).check(priority)
    assert decision.decision == "defer" and not decision.admitted
    assert "probe_invalid" in decision.tags


def test_the_protected_class_is_still_admitted_on_a_non_finite_reading():
    """PRESERVATION (traffic): the protected class is admitted before any reading is looked at,
    so a NaN reading does not defer it."""
    decision = _guard(HardwareState(cpu_percent=math.nan, inflight=0)).check("critical")
    assert decision.decision == "admit" and "protected" in decision.tags


def test_finite_readings_with_no_matching_rule_are_admitted():
    """PRESERVATION (traffic): ordinary finite readings that match no rule are admitted."""
    decision = _guard(HardwareState(cpu_percent=10.0, ram_available_mb=8192.0,
                                    vram_available_mb=8192.0, inflight=0)).check("bulk")
    assert decision.decision == "admit"


@pytest.mark.parametrize("where", ["class", "rule", "when"])
def test_a_policy_row_that_is_not_a_mapping_is_a_value_error(where):
    """REGRESSION (traffic): a priority class, rule or `when` block that is not a mapping is
    refused with ValueError, not AttributeError."""
    data = yaml.safe_load(TRAFFIC.read_text(encoding="utf-8"))
    if where == "class":
        data["priority_classes"].append(["critical", 99])
    elif where == "rule":
        data["rules"].append("defer everything")
    else:
        data["rules"][0]["when"] = ["cpu_percent_at_least", 80]
    with pytest.raises(ValueError):
        parse_traffic_policy(data)


# =========================================================================== memory
DEEP_LINE = "[" * 200_000 + "]" * 200_000


@pytest.mark.parametrize("bad", ["{not json", DEEP_LINE, "[1, 2, 3]", '"a string"'],
                         ids=["malformed", "deeply-nested", "array", "string"])
def test_a_bad_lessons_log_line_is_a_value_error_naming_its_line(tmp_path, bad):
    """REGRESSION (memory): loading a lessons log with a line that is malformed, nested past
    the parser's limit, or not a JSON object raises ValueError naming that line's number."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    store.append(Lesson(text="a lesson", source_artifact_ids=()))
    with log.open("a", encoding="utf-8") as fh:
        fh.write(bad + "\n")
    with pytest.raises(ValueError, match=r"line 2\b"):
        MemoryStore(log)


def test_a_well_formed_lessons_log_still_loads(tmp_path):
    """PRESERVATION (memory): a log with blank lines between records loads every record."""
    log = tmp_path / "lessons.jsonl"
    store = MemoryStore(log)
    a = store.append(Lesson(text="one", source_artifact_ids=()))
    with log.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    store.append(Lesson(text="two", source_artifact_ids=()))
    store.approve(a.id)
    reloaded = MemoryStore(log)
    assert [l.text for l in reloaded.all()] == ["one", "two"] and reloaded.get(a.id).approved


def test_models_example_names_no_omli_variable():
    """REGRESSION (hosted): the example model registry's hosted entry names a variable outside
    the OMLI_ prefix, so the shipped example passes its own rule."""
    data = yaml.safe_load(MODELS.read_text(encoding="utf-8"))
    names = [m.get("api_key_env") for m in data["models"] if m.get("api_key_env")]
    assert names and not any(n.startswith("OMLI_") for n in names)
    parse_models(data)
