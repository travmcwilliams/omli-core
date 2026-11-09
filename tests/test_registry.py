"""ModelRegistry: strict validation, immutability, no moving tags."""

import copy
import dataclasses
from pathlib import Path

import pytest
import yaml

from omli.models.registry import ModelRegistry, load_registry, parse_registry

REG_PATH = Path(__file__).resolve().parent.parent / "omli" / "defaults" / "model_registry.example.yaml"


def _data():
    return yaml.safe_load(REG_PATH.read_text(encoding="utf-8"))


# The example registry holds text models only. The other kinds stay in the schema, so their rules
# are tested against the entries below.
def _audio_entry(model_id="local-sfx", **capabilities):
    return {
        "model_id": model_id, "display_name": model_id, "family": "audio", "provider_kind": "local-audio",
        "adapter": "audio-example", "endpoint": "http://localhost:8189",
        "pin": {"ref": "0123456789abcdef0123456789abcdef01234567", "quant": None}, "reproducible": True,
        "status": "staging",
        "capabilities": {"structured_output": "none", "sample_rate_hz": 44100, "channels": 2,
                         "max_duration_s": 47.0, "streaming": False, **capabilities},
    }


def _hosted_image_entry(model_id="hosted-image"):
    return {
        "model_id": model_id, "display_name": model_id, "family": "image", "provider_kind": "google-genai",
        "adapter": "image-example", "endpoint": "https://api.example.com/v1/images",
        "pin": {"ref": model_id, "quant": None}, "reproducible": False, "status": "staging",
        "capabilities": {"context_window": 32768, "max_output_tokens": 2000, "native_tool_calling": False,
                         "parallel_tool_calls": False, "structured_output": "none", "thinking_mode": False,
                         "vision": False},
        "pricing": {"usd_per_1m_output_tokens": 60.0, "tokens_by_resolution": {"1K": 1120}, "source": "example"},
    }


def _with(*entries):
    data = _data()
    data["models"].extend(entries)
    return parse_registry(data)


def test_example_registry_loads_and_resolves_ids():
    reg = load_registry(REG_PATH)
    assert isinstance(reg, ModelRegistry)
    assert set(reg.ids()) == {"example-local-4b", "hosted-burst"}
    local = reg.get("example-local-4b")
    assert local.provider_kind == "local-ollama"
    assert local.adapter == "ollama"
    assert local.reproducible is True
    assert local.serving_tag == "example-model:4b"  # the Ollama tag; pin.ref stays the digest


def test_local_requires_serving_tag():
    data = _data()
    del data["models"][0]["serving_tag"]
    with pytest.raises(ValueError, match="serving_tag"):
        parse_registry(data)


def test_missing_model_raises_keyerror():
    reg = load_registry(REG_PATH)
    with pytest.raises(KeyError):
        reg.get("does-not-exist")


def test_entry_is_immutable():
    reg = load_registry(REG_PATH)
    entry = reg.get("example-local-4b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.model_id = "x"  # type: ignore[misc]


def test_duplicate_model_id_rejected():
    data = _data()
    data["models"].append(copy.deepcopy(data["models"][0]))
    with pytest.raises(ValueError, match="duplicate model_id"):
        parse_registry(data)


def test_local_moving_tag_rejected():
    data = _data()
    data["models"][0]["pin"]["ref"] = "example-model:4b"  # a moving tag, not a digest
    with pytest.raises(ValueError, match="sha256 digest"):
        parse_registry(data)


def test_local_reproducible_must_be_true_for_digest():
    data = _data()
    data["models"][0]["reproducible"] = False
    with pytest.raises(ValueError, match="reproducible"):
        parse_registry(data)


def test_hosted_must_not_be_reproducible():
    """Find the hosted entry by what it is, not by its position: a test that picked the entry
    by index would silently start testing a local model (which may be reproducible) as soon
    as a model was added above it."""
    data = _data()
    hosted = next(m for m in data["models"]
                  if str(m.get("provider_kind", "")).startswith("openai-"))
    hosted["reproducible"] = True
    with pytest.raises(ValueError, match="reproducible must be false"):
        parse_registry(data)


def test_bad_status_rejected():
    data = _data()
    data["metadata"]["status"] = "live"
    with pytest.raises(ValueError, match="status must be one of"):
        parse_registry(data)


def test_bad_structured_output_rejected():
    data = _data()
    data["models"][0]["capabilities"]["structured_output"] = "fancy"
    with pytest.raises(ValueError, match="structured_output must be one of"):
        parse_registry(data)


def test_bad_provider_kind_rejected():
    data = _data()
    data["models"][0]["provider_kind"] = "local-vllm"
    with pytest.raises(ValueError, match="provider_kind must be one of"):
        parse_registry(data)


# ── pricing block ───────────────────────────────────
# Price is a property OF a model, so it lives in the one catalog that names models and
# inherits its immutability: a vendor price change is a new registry_version, never an
# edit. The block is OPTIONAL — an entry without one is UNPRICED, which callers must
# treat as unknown cost rather than free.

def _priced(data, **over):
    """Point the first model at a pricing block, for validator tests."""
    block = {"usd_per_1m_output_tokens": 60.0, "tokens_by_resolution": {"1k": 1120}}
    block.update(over)
    data["models"][0]["pricing"] = block
    return data


def test_pricing_is_optional_and_absent_means_unpriced():
    reg = load_registry(REG_PATH)
    assert reg.get("example-local-4b").pricing is None


def test_priced_entries_expose_their_rates():
    entry = _with(_hosted_image_entry()).get("hosted-image")
    assert entry.pricing.is_priced()
    assert entry.pricing.usd_per_1m_output_tokens == 60.0
    # the resolution label is stored with its casing unchanged
    assert entry.pricing.tokens_by_resolution["1K"] == 1120
    assert entry.pricing.source == "example"  # the free-text provenance is kept as given


def test_pricing_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown pricing key"):
        parse_registry(_priced(_data(), usd_per_gpu_hour=3.0))


def test_pricing_rejects_negative_rate():
    with pytest.raises(ValueError, match="must not be negative"):
        parse_registry(_priced(_data(), usd_per_1m_output_tokens=-1.0))


# ── local-audio kind + per-kind capabilities ────────
# Capabilities are checked per kind, so a model cannot describe itself with a surface it
# does not have. Both directions are tested: an audio entry may not claim text
# capabilities, and a text entry may not claim audio ones.

def _audio_index(data):
    """Add an audio entry to `data` and return its position."""
    data["models"].append(_audio_entry())
    return len(data["models"]) - 1


def test_audio_entry_declares_waveform_facts_and_no_text_ones():
    entry = _with(_audio_entry()).get("local-sfx")
    assert entry.provider_kind == "local-audio"
    assert entry.capabilities.sample_rate_hz == 44100
    assert entry.capabilities.channels == 2
    assert entry.capabilities.max_duration_s == 47.0
    # An audio entry declares no text surface.
    assert entry.capabilities.context_window is None
    assert entry.capabilities.max_output_tokens is None
    assert entry.pricing is None  # local inference is unpriced, never "free"


def test_sfx_model_states_no_voice_count_rather_than_zero():
    reg = _with(_audio_entry(), _audio_entry("local-speech", voice_count=54, voice_cloning=False))
    entry = reg.get("local-sfx")
    assert entry.capabilities.voice_count is None
    assert entry.capabilities.voice_cloning is None
    speech = reg.get("local-speech")
    assert speech.capabilities.voice_count == 54
    assert speech.capabilities.voice_cloning is False


def test_audio_entry_missing_sample_rate_rejected():
    data = _data()
    del data["models"][_audio_index(data)]["capabilities"]["sample_rate_hz"]
    with pytest.raises(ValueError, match="capabilities missing 'sample_rate_hz'"):
        parse_registry(data)


def test_audio_entry_may_not_claim_a_context_window():
    data = _data()
    data["models"][_audio_index(data)]["capabilities"]["context_window"] = 512
    with pytest.raises(ValueError, match="must not declare text capabilities"):
        parse_registry(data)


def test_text_entry_may_not_claim_audio_capabilities():
    data = _data()
    data["models"][0]["capabilities"]["sample_rate_hz"] = 24000
    with pytest.raises(ValueError, match="must not declare audio capabilities"):
        parse_registry(data)


def test_text_entry_requires_context_window():
    """A text entry without capabilities.context_window is rejected."""
    data = _data()
    del data["models"][0]["capabilities"]["context_window"]
    with pytest.raises(ValueError, match="capabilities missing 'context_window'"):
        parse_registry(data)


def test_unknown_capability_key_rejected():
    data = _data()
    data["models"][0]["capabilities"]["bitrate_kbps"] = 320
    with pytest.raises(ValueError, match="unknown capability key"):
        parse_registry(data)


def test_audio_channels_must_be_mono_or_stereo():
    data = _data()
    data["models"][_audio_index(data)]["capabilities"]["channels"] = 6
    with pytest.raises(ValueError, match="channels must be 1"):
        parse_registry(data)


def test_audio_pin_must_be_a_label_while_not_reproducible():
    """Both halves of the pair are set here rather than inherited.

    A test about the RULE should not depend on what a shipped entry happens to declare.
    """
    data = _data()
    i = _audio_index(data)
    data["models"][i]["reproducible"] = False
    data["models"][i]["pin"]["ref"] = "a" * 40  # a hash implies verification
    with pytest.raises(ValueError, match="must be a label"):
        parse_registry(data)


def test_reproducible_audio_pin_must_be_a_real_commit():
    data = _data()
    i = _audio_index(data)
    data["models"][i]["reproducible"] = True
    data["models"][i]["pin"]["ref"] = "some-moving-label"
    with pytest.raises(ValueError, match="Hugging Face commit hash"):
        parse_registry(data)


def test_num_ctx_refused_on_a_model_with_no_text_surface():
    data = _data()
    data["models"][_audio_index(data)]["params_default"] = {"num_ctx": 4096}
    with pytest.raises(ValueError, match="declares no context_window"):
        parse_registry(data)


def test_pricing_rejects_non_numeric_rate():
    with pytest.raises(ValueError, match="must be a number"):
        parse_registry(_priced(_data(), usd_per_1m_output_tokens="cheap"))


def test_pricing_block_that_prices_nothing_is_rejected():
    """A block that looks authoritative but computes no cost is worse than none."""
    data = _data()
    data["models"][0]["pricing"] = {"usd_per_1m_input_tokens": 0.5}
    with pytest.raises(ValueError, match="prices nothing"):
        parse_registry(data)


def test_tokens_by_resolution_requires_a_token_rate():
    data = _data()
    data["models"][0]["pricing"] = {
        "usd_per_image": 0.04, "tokens_by_resolution": {"1k": 1120},
    }
    with pytest.raises(ValueError, match="needs usd_per_1m_output_tokens"):
        parse_registry(data)


def test_tokens_by_resolution_rejects_non_positive_counts():
    with pytest.raises(ValueError, match="must be a positive integer"):
        parse_registry(_priced(_data(), tokens_by_resolution={"1k": 0}))


def test_zero_price_is_allowed_a_free_tier_is_a_real_price():
    reg = parse_registry(_priced(_data(), usd_per_1m_output_tokens=0.0,
                                 usd_per_image=0.0))
    assert reg.models[0].pricing.usd_per_image == 0.0


def test_google_genai_is_a_hosted_kind_so_it_cannot_claim_reproducibility():
    data = _data()
    image = _hosted_image_entry()
    image["reproducible"] = True
    data["models"].append(image)
    with pytest.raises(ValueError, match="reproducible must be false"):
        parse_registry(data)


def test_hosted_image_models_are_pinned_to_labels_not_digests():
    reg = _with(_hosted_image_entry("image-a"), _hosted_image_entry("image-b"))
    for model_id in ("image-a", "image-b"):
        entry = reg.get(model_id)
        assert not entry.pin.ref.startswith("sha256:")
        assert entry.pin.quant is None
        assert entry.reproducible is False


# ── local-diffusers ───────────────────────────────────────────
# A local image model whose weights are held locally: pinned to an immutable commit, so
# reproducible:true, but pinned by a 40-char commit hash rather than a sha256 blob digest.
# It may carry no pricing block.

def _diff(data, **over):
    """Point the first model at a valid local-diffusers shape, for validator tests."""
    m = data["models"][0]
    m["provider_kind"] = "local-diffusers"
    m["adapter"] = "diffusers-example"
    m["endpoint"] = "http://localhost:8188"
    m["reproducible"] = True
    m["pin"] = {"ref": "89abcdef0123456789abcdef0123456789abcdef", "quant": None}
    m.pop("serving_tag", None)
    m.pop("params_default", None)
    for k, v in over.items():
        m[k] = v
    return data


def test_local_diffusers_entry_loads():
    entry = parse_registry(_diff(_data())).models[0]
    assert entry.provider_kind == "local-diffusers"
    assert entry.adapter == "diffusers-example"
    assert entry.reproducible is True
    assert entry.pricing is None                      # local => unpriced, not zero-priced
    assert len(entry.pin.ref) == 40 and entry.pin.quant is None


def test_local_diffusers_accepts_a_full_commit_hash():
    reg = parse_registry(_diff(_data()))
    assert reg.models[0].pin.ref == "89abcdef0123456789abcdef0123456789abcdef"


def test_local_diffusers_rejects_a_branch_name_pin():
    with pytest.raises(ValueError, match="40-char Hugging Face commit"):
        parse_registry(_diff(_data(), pin={"ref": "main", "quant": None}))


def test_local_diffusers_rejects_a_short_hash():
    with pytest.raises(ValueError, match="40-char"):
        parse_registry(_diff(_data(), pin={"ref": "b3c9ce6", "quant": None}))


def test_local_diffusers_accepts_a_file_sha256_when_reproducible():
    # A single-file checkpoint pins by the FILE's sha256, not a Hugging Face commit. A
    # verifiable sha256 with reproducible:true is valid, so such a checkpoint needs no
    # 40-char commit.
    reg = parse_registry(_diff(_data(), pin={"ref": "sha256:" + "a" * 64, "quant": None}))
    assert reg.get("example-local-4b").reproducible is True


def test_local_diffusers_reproducible_needs_a_verifiable_pin():
    # A plain label with reproducible:true is refused — reproducibility must be earned.
    with pytest.raises(ValueError, match="commit hash OR a 64-char sha256"):
        parse_registry(_diff(_data(), pin={"ref": "just-a-name", "quant": None}))


def test_local_diffusers_may_be_unreproducible_with_a_label():
    # reproducible:false + a label pin is accepted, as it is for a hosted model.
    reg = parse_registry(_diff(_data(), reproducible=False,
                               pin={"ref": "example-label", "quant": None}))
    assert reg.get("example-local-4b").reproducible is False


def test_local_diffusers_unreproducible_pin_must_not_be_a_hash():
    # reproducible:false + a real digest is dishonest — it implies verification we lack.
    with pytest.raises(ValueError, match="label"):
        parse_registry(_diff(_data(), reproducible=False,
                             pin={"ref": "sha256:" + "a" * 64, "quant": None}))


def test_local_diffusers_rejects_a_quant():
    with pytest.raises(ValueError, match="quant must be null"):
        parse_registry(_diff(_data(), pin={"ref": "b" * 40, "quant": "Q4"}))


def test_local_diffusers_may_be_unpriced():
    # a local model carries no pricing block and that is valid
    reg = parse_registry(_diff(_data()))
    assert reg.models[0].pricing is None
