"""The declared serving window (num_ctx) is sent to the server.

If num_ctx were not sent, the server would apply its own small default and truncate
silently, and the gates would be validating a prompt the model never fully saw.

Two halves, tested separately on purpose:
  * the value is transmitted (and keep_alive as a TOP-LEVEL field, not an option);
  * an unknown params_default key fails loud instead of being dropped, so a knob added
    to the YAML can never parse clean and do nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omli.models.provider import (
    Message,
    ModelRequest,
    OllamaProvider,
    SamplingParams,
    _ollama_options,
)
from omli.models.registry import PARAMS_DEFAULT_KEYS, load_registry, parse_registry

REPO = Path(__file__).resolve().parents[1]
REGISTRIES = [REPO / "omli" / "defaults" / "model_registry.example.yaml",
              REPO / "tests" / "data" / "registry.yaml"]


def _entry_data(**over):
    data = {
        "model_id": "m",
        "display_name": "M",
        "family": "example-family",
        "provider_kind": "local-ollama",
        "adapter": "ollama",
        "endpoint": "http://localhost:11434",
        "serving_tag": "m:tag",
        "pin": {"ref": "sha256:" + "ab" * 32, "quant": "Q4_K_M"},
        "reproducible": True,
        "status": "staging",
        "capabilities": {
            "context_window": 8192,
            "max_output_tokens": 2048,
            "native_tool_calling": False,
            "parallel_tool_calls": False,
            "structured_output": "none",
            "thinking_mode": False,
            "vision": False,
        },
        "params_default": {"temperature": 0.2},
    }
    data.update(over)
    return data


def _registry(**over):
    return parse_registry({
        "metadata": {
            "registry_id": "r", "registry_version": "1.0.0",
            "authored_at": "2026-07-21T00:00:00Z", "authored_by": "t", "status": "staging",
        },
        "models": [_entry_data(**over)],
    })


# --------------------------------------------------------------- it is transmitted
def test_num_ctx_is_emitted_as_a_sampling_option():
    opts = _ollama_options(SamplingParams(num_ctx=8192))
    assert opts["num_ctx"] == 8192


def test_unset_num_ctx_is_omitted_rather_than_guessed():
    # Omitting differs from sending a default: it leaves the server's choice intact
    # for models we have not sized. Sending a made-up number would be worse.
    assert "num_ctx" not in _ollama_options(SamplingParams(temperature=0.2))


def test_keep_alive_is_a_top_level_field_not_a_sampling_option():
    # keep_alive is a top-level request field, so it is sent there and not inside `options`.
    assert "keep_alive" not in _ollama_options(SamplingParams(keep_alive="30m"))

    sent = {}

    def transport(url, body, timeout):
        sent.update(json.loads(body.decode("utf-8")))
        return json.dumps({"message": {"content": "ok"}, "done_reason": "stop"}).encode()

    entry = _registry(params_default={"temperature": 0.2, "num_ctx": 4096,
                                      "keep_alive": "30m"}).get("m")
    OllamaProvider(transport=transport).invoke(
        entry, ModelRequest(messages=(Message(role="user", content="hi"),),
                            params=SamplingParams(num_ctx=4096, keep_alive="30m")))
    assert sent["keep_alive"] == "30m"
    assert sent["options"]["num_ctx"] == 4096


def test_registry_defaults_reach_the_wire_through_the_agent_request():
    from omli.agents.base import build_model_request
    from omli.artifacts import Provenance, new_artifact
    from omli.packs.schema import ModelSelection, Pack

    entry = _registry(params_default={"temperature": 0.2, "num_ctx": 8192,
                                      "keep_alive": "30m"}).get("m")
    pack = Pack(name="p", version="1", identity="i", prompt_template="{input}",
                model_selection=ModelSelection(model_id="m"))
    art = new_artifact("CanonicalInput", {"text": "x"},
                       Provenance(agent="t", pack_name="p", pack_version="1"))
    req = build_model_request(pack, art, entry)
    assert req.params.num_ctx == 8192 and req.params.keep_alive == "30m"


# --------------------------------------------------------------- loud validation
def test_unknown_params_default_key_fails_loud():
    # A misspelled knob must not parse clean and do nothing, which would make the config
    # look applied while the server never saw it.
    with pytest.raises(ValueError, match="unknown params_default key"):
        _registry(params_default={"temperature": 0.2, "num_ctxx": 8192})


def test_the_error_names_the_offending_key_and_the_valid_set():
    with pytest.raises(ValueError) as e:
        _registry(params_default={"keepalive": "30m"})
    msg = str(e.value)
    assert "keepalive" in msg and "keep_alive" in msg


@pytest.mark.parametrize("bad", [0, -1, "8192", 8192.5, True])
def test_num_ctx_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="num_ctx must be a positive integer"):
        _registry(params_default={"num_ctx": bad})


def test_num_ctx_may_not_exceed_the_declared_capability():
    # A serving window bigger than the model's declared context is a config error.
    with pytest.raises(ValueError, match="exceeds the declared context_window"):
        _registry(params_default={"num_ctx": 131072})


def test_num_ctx_equal_to_the_capability_is_allowed():
    assert _registry(params_default={"num_ctx": 8192}).get("m").params_default.num_ctx == 8192


@pytest.mark.parametrize("bad", ["", "   ", 30, None_ := 0])
def test_keep_alive_must_be_a_non_empty_string(bad):
    with pytest.raises(ValueError, match="keep_alive must be a non-empty string"):
        _registry(params_default={"keep_alive": bad})


def test_params_default_without_num_ctx_or_keep_alive_parses_with_both_unset():
    reg = _registry(params_default={"temperature": 0.2})
    d = reg.get("m").params_default
    assert d.num_ctx is None and d.keep_alive is None


def test_the_valid_key_set_is_exactly_what_sampling_defaults_supports():
    assert PARAMS_DEFAULT_KEYS == {
        "temperature", "top_p", "max_tokens", "stop", "num_ctx", "keep_alive"}


# --------------------------------------------------------------- real registries
def test_the_repo_registries_parse_under_loud_validation():
    for path in REGISTRIES:
        load_registry(path)


def test_example_3b_declares_a_serving_window_and_keep_alive():
    # The one registry that ships example-3b states the window the server should allocate
    # and the residency, rather than leaving either to a server default.
    seen = 0
    for path in REGISTRIES:
        reg = load_registry(path)
        if "example-3b" not in reg.ids():
            continue
        seen += 1
        d = reg.get("example-3b").params_default
        assert d.num_ctx is not None, f"{path} leaves num_ctx to the server default"
        assert d.keep_alive is not None, f"{path} does not pin residency"
    assert seen == 1


def test_serving_windows_stay_at_or_under_8192_tokens():
    # A fixed cap in the test data. It compares no real hardware. The declared capability is
    # larger, and a local server may not fit a window sized to that.
    for path in REGISTRIES:
        reg = load_registry(path)
        for mid in reg.ids():
            entry = reg.get(mid)
            if entry.adapter != "ollama" or entry.params_default.num_ctx is None:
                continue
            assert entry.params_default.num_ctx <= 8192, (
                f"{mid} in {path.parent.name} asks for a window above the cap")


def test_hosted_models_do_not_carry_ollama_serving_knobs():
    # num_ctx / keep_alive are serving-side concepts for the local runtime; setting
    # them on a hosted entry would imply control we do not have.
    for path in REGISTRIES:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        for m in raw.get("models", []):
            if m.get("provider_kind") == "openai-compatible":
                pd = m.get("params_default") or {}
                assert "num_ctx" not in pd and "keep_alive" not in pd, m.get("model_id")
