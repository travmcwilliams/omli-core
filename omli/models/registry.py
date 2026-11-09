"""ModelRegistry — the single, universal catalog of every model.

This is the ONLY place a concrete model is named. Packs select a model by `model_id`;
core and agents never name one. A registry is immutable per `registry_version`: adopting
or changing a model is a NEW version + human sign-off, never an edit.

Validation is hand-rolled (the Python package does not use zod; the TypeScript contract in
`docs/connection-registry.ts` does) and is deliberately strict and
loud. A local-ollama pin must be a sha256 digest, never a moving tag like "example-model:4b". A
reproducible local-diffusers or local-audio pin must be a 40-character commit hash or a sha256 of
the checkpoint file; one that is not yet reproducible carries a label instead. Every boolean field
must be a YAML boolean: a quoted "true" or "false" is refused.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import yaml

MODEL_STATUSES = ("draft", "staging", "production", "deprecated")
PROVIDER_KINDS = (
    "local-ollama",
    "local-diffusers",
    "local-audio",
    "openai-compatible",
    "cli-subscription",
    "google-genai",
)

# Kinds whose weights we do NOT control: pinned by a dated vendor id rather than a
# digest, never reproducible. "cli-subscription" is a vendor CLI already logged in on
# this machine (no API key in the environment), so there
# is no endpoint and no digest to verify; the vendor may move the model under the alias
# at any time, which is exactly why reproducible must be false. "google-genai" is a hosted
# vendor API with the same rule: a dated vendor id, weights not ours. Only "openai-compatible" has an adapter in this package; the
# other hosted kinds, and the local kinds other than "local-ollama", are vocabulary for
# adapters a deployment supplies. A key is named by `api_key_env`, read from the environment.
_HOSTED_KINDS = ("openai-compatible", "cli-subscription", "google-genai")
# Kinds whose weights ARE controlled locally: downloaded to disk and pinned, so they are
# reproducible. "local-ollama" pins a sha256 digest of the served blob; "local-diffusers"
# is a diffusers pipeline pinned to an immutable commit hash; "local-audio" is the same
# arrangement for a waveform model. Each is served from a loopback endpoint.
_LOCAL_KINDS = ("local-ollama", "local-diffusers", "local-audio")
STRUCTURED_OUTPUT = ("none", "json_mode", "grammar")

_ENV_VAR_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")

#: The prefix of every environment variable OMLi reads by name. The package reads
#: ``OMLI_HOME``, ``OMLI_CONNECTION_REGISTRY``, ``OMLI_SECRETS_DIR``, one ``OMLI_SECRET_<PATH>``
#: per secret reference, ``OMLI_SEAL_KEY``, ``OMLI_SEAL_PASSPHRASE`` and ``OMLI_GPU_TELEMETRY``;
#: the test suite also reads ``OMLI_LIVE_OLLAMA``. `api_key_env` may not start with the prefix,
#: so a registry entry cannot send OMLi's own settings or secrets to a model endpoint.
RESERVED_ENV_PREFIX = "OMLI_"

#: The home-directory variables, which `api_key_env` may not name: ``os.path.expanduser`` reads
#: ``HOME`` on POSIX and ``USERPROFILE``, ``HOMEDRIVE`` and ``HOMEPATH`` on Windows to expand a
#: leading ``~`` in the integrity key path and in ``OMLI_HOME``.
RESERVED_ENV_NAMES = frozenset({"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"})

#: The proxy rule: `api_key_env` may not name any variable whose name, compared without regard
#: to case, ends in ``_PROXY`` (``NO_PROXY`` among them). When the shared opener in `omli.net`
#: is built, urllib reads every variable whose name ends in ``_proxy`` in any case, so this
#: suffix covers each one it can read.
RESERVED_ENV_SUFFIX = "_PROXY"


def is_env_var_name(value: Any) -> bool:
    """True when ``value`` is an environment-variable name: capital letters, digits and
    underscores, not starting with a digit."""
    return isinstance(value, str) and _ENV_VAR_NAME.fullmatch(value) is not None


def is_api_key_env_name(value: Any) -> bool:
    """True when ``value`` may be used as `api_key_env`: an environment-variable name that does
    not start with :data:`RESERVED_ENV_PREFIX`, is not in :data:`RESERVED_ENV_NAMES`, and does
    not end in :data:`RESERVED_ENV_SUFFIX` in any case."""
    return (is_env_var_name(value) and not value.startswith(RESERVED_ENV_PREFIX)
            and value not in RESERVED_ENV_NAMES
            and not value.upper().endswith(RESERVED_ENV_SUFFIX))


_SHA256_PREFIX = "sha256:"
_HEXDIGITS = set("0123456789abcdef")

# -- capability vocabulary, split by what a kind can honestly answer ----------
#
# Requirement is per-kind, because a waveform model has no defensible integer to put in
# `context_window`. A text model still cannot be listed without a context_window.
_CAP_COMMON = ("structured_output",)
_CAP_TEXT = (
    "context_window",
    "max_output_tokens",
    "native_tool_calling",
    "parallel_tool_calls",
    "thinking_mode",
    "vision",
)
#: What a waveform model can actually state about itself. Deliberately does NOT include
#: voice_count / voice_cloning: those describe a speech model, and an effects model must
#: not be forced to invent them. They are optional extras below.
_CAP_AUDIO = ("sample_rate_hz", "channels", "max_duration_s", "streaming")
#: Optional on any audio entry — a TTS model sets them, an SFX model leaves them None.
_CAP_AUDIO_OPTIONAL = ("voice_count", "voice_cloning")

_CAP_REQUIRED_BY_KIND = {
    "local-audio": _CAP_COMMON + _CAP_AUDIO,
    # Every other kind requires _CAP_DEFAULT_REQUIRED (the common and text capabilities).
}
_CAP_DEFAULT_REQUIRED = _CAP_COMMON + _CAP_TEXT

#: Every capability key the schema knows. An unknown key is a typo, and a silently
#: dropped capability is a false description of a model — fail loudly, as with pricing.
_CAP_KNOWN = frozenset(_CAP_COMMON + _CAP_TEXT + _CAP_AUDIO + _CAP_AUDIO_OPTIONAL)
_ENTRY_REQUIRED = (
    "model_id",
    "display_name",
    "family",
    "provider_kind",
    "adapter",
    "endpoint",
    "pin",
    "reproducible",
    "status",
    "capabilities",
)
_META_REQUIRED = ("registry_id", "registry_version", "authored_at", "authored_by", "status")


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model can do — with `None` meaning "this does not apply to this kind".

    `structured_output` is the one field every kind answers, so it is the only one
    without a default. The rest are Optional, but that is NOT a licence to omit them:
    `_CAP_REQUIRED_BY_KIND` decides which are mandatory for a given provider_kind, and
    a text model that leaves out `context_window` is still rejected. Optional here means
    "a kind exists that cannot answer this", not "authors may skip it".
    """

    structured_output: str
    # Text surface — required of every kind except local-audio.
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    native_tool_calling: Optional[bool] = None
    parallel_tool_calls: Optional[bool] = None
    thinking_mode: Optional[bool] = None
    vision: Optional[bool] = None
    # Waveform surface — required of local-audio, None everywhere else.
    sample_rate_hz: Optional[int] = None
    channels: Optional[int] = None
    #: Longest clip the model is designed to emit, in seconds. Recorded for callers; no gate
    #: in this package checks a request against it.
    max_duration_s: Optional[float] = None
    streaming: Optional[bool] = None
    # Speech-only extras. An SFX model leaves both None rather than claiming zero voices.
    voice_count: Optional[int] = None
    voice_cloning: Optional[bool] = None


@dataclass(frozen=True)
class ModelPin:
    ref: str
    quant: Optional[str] = None


@dataclass(frozen=True)
class SamplingDefaults:
    temperature: float = 0.0
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Tuple[str, ...] = field(default_factory=tuple)
    # Serving-side knobs. `context_window` in capabilities DECLARES what the model can
    # take; num_ctx is what the server is actually told to allocate. They are different
    # facts: when num_ctx is unset, it is not sent and the server's own default applies.
    num_ctx: Optional[int] = None
    # How long the server keeps the weights resident (e.g. "30m", "-1" for indefinite).
    # Passed through verbatim; the serving layer owns the format.
    keep_alive: Optional[str] = None


#: Keys accepted inside a registry entry's `params_default` block. Anything else is a
#: typo or an unsupported knob, and must fail loudly at parse rather than be dropped: a
#: dropped `num_ctx` would leave the server's own default in force.
PARAMS_DEFAULT_KEYS = frozenset(
    {"temperature", "top_p", "max_tokens", "stop", "num_ctx", "keep_alive"}
)


@dataclass(frozen=True)
class ModelPricing:
    """What a call to this model costs, as data. OPTIONAL on an entry.

    Price is a property OF a model, so it belongs in the one catalog that names models
    — and it inherits that catalog's immutability: a vendor price change is a NEW
    registry_version + sign-off, never an edit. A missing block means "cost unknown",
    which callers must treat as un-estimatable rather than free.

    Two shapes, because vendors use both:
      * per-token — `usd_per_1m_output_tokens` (+ input), with `tokens_by_resolution`
        giving the vendor's NOMINAL token count for an image of each size; and
      * flat per-image — `usd_per_image`, where size does not move the price.

    `tokens_by_resolution` is nominal ONLY. Billed tokens can differ from the published
    count for the same resolution, so it may seed an ESTIMATE but must never stand in for
    measured usage: reconcile against what the response reports.
    """

    usd_per_1m_input_tokens: Optional[float] = None
    usd_per_1m_output_tokens: Optional[float] = None
    usd_per_image: Optional[float] = None
    tokens_by_resolution: Mapping[str, int] = field(default_factory=dict)
    #: Free-text provenance for the numbers — where they were read, and when.
    source: Optional[str] = None

    def is_priced(self) -> bool:
        return (
            self.usd_per_image is not None
            or self.usd_per_1m_output_tokens is not None
        )


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    display_name: str
    family: str
    provider_kind: str
    adapter: str
    endpoint: str
    pin: ModelPin
    capabilities: ModelCapabilities
    params_default: SamplingDefaults
    reproducible: bool
    status: str
    test_suite_ref: Optional[str] = None
    # How the endpoint is asked to load this model (local-ollama: the Ollama tag, e.g.
    # "example-model:4b"). pin.ref stays the immutable digest used to verify it. Required for
    # local-ollama; for hosted the provider falls back to pin.ref (the dated id).
    serving_tag: Optional[str] = None
    # For hosted (openai-compatible) models: the environment variable holding the API key
    # (e.g. "EXAMPLE_API_KEY"). The secret itself never lives in the registry — only the
    # name of where to read it. None for local models, which need no key.
    api_key_env: Optional[str] = None
    # What a call costs. None = unpriced (local models, or a hosted model whose price we
    # have not recorded) — callers must treat that as unknown, never as free.
    pricing: Optional[ModelPricing] = None


@dataclass(frozen=True)
class ModelRegistryMetadata:
    registry_id: str
    registry_version: str
    authored_at: str
    authored_by: str
    status: str
    description: Optional[str] = None


@dataclass(frozen=True)
class ModelRegistry:
    metadata: ModelRegistryMetadata
    models: Tuple[ModelEntry, ...]

    def get(self, model_id: str) -> ModelEntry:
        for entry in self.models:
            if entry.model_id == model_id:
                return entry
        raise KeyError(model_id)

    def ids(self) -> Tuple[str, ...]:
        return tuple(e.model_id for e in self.models)

    def __iter__(self):
        return iter(self.models)


# -- validation helpers ------------------------------------------------------

def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def is_loopback_endpoint(endpoint: Any) -> bool:
    """True only for an http(s) URL whose host is localhost, 127.0.0.0/8 or ::1.

    Fails closed: userinfo, backslashes, whitespace, a missing host, a repo id or a file path all
    return False, so what the check parses cannot differ from what a transport would dial.
    """
    text = str(endpoint)
    if any(c.isspace() or c == "\\" for c in text):
        return False
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_sha256_digest(ref: str) -> bool:
    if not ref.startswith(_SHA256_PREFIX):
        return False
    digest = ref[len(_SHA256_PREFIX):]
    return len(digest) == 64 and all(c in _HEXDIGITS for c in digest.lower())


def _is_hf_commit(ref: str) -> bool:
    """A Hugging Face snapshot revision — a full 40-char git commit SHA-1.

    This is the immutable pin for a diffusers pipeline: unlike a branch name or a tag
    ('main', 'v1.0'), a full commit hash cannot be moved to point at different weights.
    Deliberately rejects the short 7-char form, which is only probabilistically unique.
    """
    return len(ref) == 40 and all(c in _HEXDIGITS for c in ref.lower())


def _parse_capabilities(
    data: Mapping[str, Any], model_id: str, provider_kind: str
) -> ModelCapabilities:
    _require(
        isinstance(data, Mapping), f"model {model_id!r}: capabilities must be a mapping"
    )
    unknown = sorted(set(data) - _CAP_KNOWN)
    _require(
        not unknown,
        f"model {model_id!r}: unknown capability key(s) {', '.join(unknown)}; "
        f"expected one of {', '.join(sorted(_CAP_KNOWN))}",
    )

    required = _CAP_REQUIRED_BY_KIND.get(provider_kind, _CAP_DEFAULT_REQUIRED)
    for f in required:
        _require(f in data, f"model {model_id!r}: capabilities missing {f!r}")

    # The other way round: a kind may not claim a capability it has no surface for. An
    # audio model asserting a context_window is a false statement about that model.
    forbidden = sorted(set(data) - set(required) - set(_CAP_AUDIO_OPTIONAL))
    if provider_kind == "local-audio":
        _require(
            not forbidden,
            f"model {model_id!r}: local-audio must not declare text capabilities "
            f"({', '.join(forbidden)}) — a waveform model has no such surface",
        )
    else:
        audio_claimed = sorted(set(data) & set(_CAP_AUDIO + _CAP_AUDIO_OPTIONAL))
        _require(
            not audio_claimed,
            f"model {model_id!r}: {provider_kind} must not declare audio capabilities "
            f"({', '.join(audio_claimed)}); use provider_kind 'local-audio'",
        )

    _require(
        data["structured_output"] in STRUCTURED_OUTPUT,
        f"model {model_id!r}: structured_output must be one of {STRUCTURED_OUTPUT}",
    )

    def _opt_int(key: str, *, positive: bool = True) -> Optional[int]:
        if key not in data:
            return None
        value = data[key]
        _require(
            isinstance(value, int) and not isinstance(value, bool),
            f"model {model_id!r}: capabilities.{key} must be an integer",
        )
        if positive:
            _require(
                value > 0, f"model {model_id!r}: capabilities.{key} must be positive"
            )
        return int(value)

    def _opt_bool(key: str) -> Optional[bool]:
        if key not in data:
            return None
        value = data[key]
        _require(
            isinstance(value, bool),
            f"model {model_id!r}: capabilities.{key} must be true or false",
        )
        return value

    channels = _opt_int("channels")
    if channels is not None:
        _require(
            channels in (1, 2),
            f"model {model_id!r}: capabilities.channels must be 1 (mono) or 2 (stereo)",
        )

    max_duration_s: Optional[float] = None
    if "max_duration_s" in data:
        raw = data["max_duration_s"]
        _require(
            isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0,
            f"model {model_id!r}: capabilities.max_duration_s must be a positive number of seconds",
        )
        max_duration_s = float(raw)

    return ModelCapabilities(
        structured_output=str(data["structured_output"]),
        context_window=_opt_int("context_window", positive=False),
        max_output_tokens=_opt_int("max_output_tokens", positive=False),
        native_tool_calling=_opt_bool("native_tool_calling"),
        parallel_tool_calls=_opt_bool("parallel_tool_calls"),
        thinking_mode=_opt_bool("thinking_mode"),
        vision=_opt_bool("vision"),
        sample_rate_hz=_opt_int("sample_rate_hz"),
        channels=channels,
        max_duration_s=max_duration_s,
        streaming=_opt_bool("streaming"),
        voice_count=_opt_int("voice_count"),
        voice_cloning=_opt_bool("voice_cloning"),
    )


#: Keys accepted inside a registry entry's `pricing` block. As with params_default, an
#: unknown key is a typo or an unsupported knob and must fail loudly — a silently dropped
#: price field would make every downstream cost estimate quietly wrong.
PRICING_KEYS = frozenset(
    {
        "usd_per_1m_input_tokens",
        "usd_per_1m_output_tokens",
        "usd_per_image",
        "tokens_by_resolution",
        "source",
    }
)


def _parse_pricing(data: Any, model_id: str) -> ModelPricing:
    _require(isinstance(data, Mapping), f"model {model_id!r}: pricing must be a mapping")
    unknown = sorted(set(data) - PRICING_KEYS)
    _require(
        not unknown,
        f"model {model_id!r}: unknown pricing key(s) {', '.join(unknown)}; "
        f"expected one of {', '.join(sorted(PRICING_KEYS))}",
    )

    def _rate(key: str) -> Optional[float]:
        if data.get(key) is None:
            return None
        value = data[key]
        _require(
            isinstance(value, (int, float)) and not isinstance(value, bool),
            f"model {model_id!r}: pricing.{key} must be a number",
        )
        # Zero is a real, meaningful price (a free tier); negative never is.
        _require(float(value) >= 0.0, f"model {model_id!r}: pricing.{key} must not be negative")
        return float(value)

    by_res_raw = data.get("tokens_by_resolution") or {}
    _require(
        isinstance(by_res_raw, Mapping),
        f"model {model_id!r}: pricing.tokens_by_resolution must be a mapping of label -> token count",
    )
    by_res = {}
    for label, tokens in by_res_raw.items():
        _require(
            isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0,
            f"model {model_id!r}: pricing.tokens_by_resolution[{label!r}] must be a positive integer",
        )
        by_res[str(label)] = int(tokens)

    pricing = ModelPricing(
        usd_per_1m_input_tokens=_rate("usd_per_1m_input_tokens"),
        usd_per_1m_output_tokens=_rate("usd_per_1m_output_tokens"),
        usd_per_image=_rate("usd_per_image"),
        tokens_by_resolution=by_res,
        source=(str(data["source"]) if data.get("source") else None),
    )
    # A pricing block that prices nothing is a config error: it reads as "we know the
    # cost" while supplying no way to compute one.
    _require(
        pricing.is_priced(),
        f"model {model_id!r}: pricing must set usd_per_image or usd_per_1m_output_tokens "
        f"(a block that prices nothing is worse than none at all — it looks authoritative)",
    )
    # Per-resolution token counts only mean something under per-token pricing.
    _require(
        not by_res or pricing.usd_per_1m_output_tokens is not None,
        f"model {model_id!r}: pricing.tokens_by_resolution needs usd_per_1m_output_tokens "
        f"to convert those tokens into a cost",
    )
    return pricing


def _parse_entry(data: Mapping[str, Any]) -> ModelEntry:
    missing = [k for k in _ENTRY_REQUIRED if k not in data]
    _require(not missing, f"model entry missing keys: {', '.join(missing)}")
    model_id = str(data["model_id"])

    provider_kind = str(data["provider_kind"])
    _require(provider_kind in PROVIDER_KINDS, f"model {model_id!r}: provider_kind must be one of {PROVIDER_KINDS}")
    _require(str(data["status"]) in MODEL_STATUSES, f"model {model_id!r}: status must be one of {MODEL_STATUSES}")
    # A local kind is a claim about where the payload goes, and route controls rely on it.
    if provider_kind.startswith("local-"):
        _require(
            is_loopback_endpoint(data["endpoint"]),
            f"model {model_id!r}: a {provider_kind} endpoint must be loopback "
            f"(localhost, 127.0.0.0/8 or ::1), got {data['endpoint']!r}",
        )

    pin_data = data["pin"]
    _require(isinstance(pin_data, Mapping) and pin_data.get("ref"), f"model {model_id!r}: pin.ref is required")
    pin = ModelPin(ref=str(pin_data["ref"]), quant=pin_data.get("quant"))
    reproducible = data["reproducible"]
    _require(isinstance(reproducible, bool),
             f"model {model_id!r}: reproducible must be true or false (a YAML boolean)")

    serving_tag = data.get("serving_tag")
    if provider_kind == "local-ollama":
        _require(
            serving_tag,
            f"model {model_id!r}: local-ollama requires a serving_tag (the Ollama tag to "
            f"load, e.g. 'example-model:4b'); pin.ref stays the digest used to verify it",
        )
    serving_tag = str(serving_tag) if serving_tag else None

    # Pin / reproducibility rules — the heart of "no moving tags".
    if provider_kind == "local-ollama":
        _require(
            _is_sha256_digest(pin.ref),
            f"model {model_id!r}: local-ollama pin must be a sha256 digest, not a moving "
            f"tag like 'example-model:4b' (got {pin.ref!r})",
        )
        _require(reproducible, f"model {model_id!r}: a sha256-pinned local model must be reproducible: true")
    elif provider_kind in ("local-diffusers", "local-audio"):
        # A local weights-on-disk model is either a snapshot repo (pinned by 40-char commit)
        # or a single-file checkpoint (pinned by the file's sha256). Either way, REPRODUCIBLE demands
        # a verifiable, immutable pin. local-audio is held to exactly the same bar: a
        # waveform model is no less a weights artifact than a diffusion one.
        if reproducible:
            _require(
                _is_hf_commit(pin.ref) or _is_sha256_digest(pin.ref),
                f"model {model_id!r}: a reproducible {provider_kind} pin must be a 40-char "
                f"Hugging Face commit hash OR a 64-char sha256 of the checkpoint file "
                f"(got {pin.ref!r})",
            )
        else:
            # Not yet reproducible — e.g. a community checkpoint the operator has not
            # downloaded. The pin is a LABEL until the file lands, exactly as a hosted
            # model's pin is a dated id; it must not masquerade as a verified digest.
            _require(
                not _is_hf_commit(pin.ref) and not _is_sha256_digest(pin.ref),
                f"model {model_id!r}: a reproducible:false {provider_kind} pin must be a "
                f"label, not a hash that implies verification (got {pin.ref!r}). Set the "
                f"sha256 and flip reproducible:true once the checkpoint is on disk.",
            )
        _require(pin.quant is None, f"model {model_id!r}: {provider_kind} pin.quant must be null (dtype is a serving choice)")
    else:  # openai-compatible / cli-subscription / google-genai — weights not controlled by us
        _require(not _is_sha256_digest(pin.ref), f"model {model_id!r}: hosted pin should be a dated id, not a sha256 digest")
        _require(not reproducible, f"model {model_id!r}: hosted model weights are not controlled — reproducible must be false")
        _require(pin.quant is None, f"model {model_id!r}: hosted pin.quant must be null")

    caps = _parse_capabilities(data["capabilities"], model_id, provider_kind)

    pd = data.get("params_default") or {}
    unknown = sorted(set(pd) - PARAMS_DEFAULT_KEYS)
    _require(
        not unknown,
        f"model {model_id!r}: unknown params_default key(s) {', '.join(unknown)}; "
        f"expected one of {', '.join(sorted(PARAMS_DEFAULT_KEYS))}",
    )
    num_ctx = pd.get("num_ctx")
    if num_ctx is not None:
        _require(
            isinstance(num_ctx, int) and not isinstance(num_ctx, bool) and num_ctx > 0,
            f"model {model_id!r}: params_default.num_ctx must be a positive integer",
        )
        # A serving window larger than the model's declared capability is a config
        # error, not a stretch goal — catch it here rather than at inference time.
        # A kind with no text surface declares no context_window, and asking for a
        # serving window on one is itself the error.
        _require(
            caps.context_window is not None,
            f"model {model_id!r}: params_default.num_ctx is meaningless for "
            f"provider_kind {provider_kind!r}, which declares no context_window",
        )
        _require(
            num_ctx <= caps.context_window,
            f"model {model_id!r}: params_default.num_ctx ({num_ctx}) exceeds the "
            f"declared context_window ({caps.context_window})",
        )
    keep_alive = pd.get("keep_alive")
    if keep_alive is not None:
        _require(
            isinstance(keep_alive, str) and keep_alive.strip() != "",
            f"model {model_id!r}: params_default.keep_alive must be a non-empty string",
        )
    params = SamplingDefaults(
        temperature=float(pd.get("temperature", 0.0)),
        top_p=pd.get("top_p"),
        max_tokens=pd.get("max_tokens"),
        stop=tuple(pd.get("stop", ()) or ()),
        num_ctx=num_ctx,
        keep_alive=keep_alive,
    )

    api_key_env = data.get("api_key_env")
    # Only a variable NAME belongs here. The value is never repeated in the error: a key pasted
    # into this field by mistake must not be echoed back.
    _require(
        api_key_env is None or is_api_key_env_name(api_key_env),
        f"model {model_id!r}: api_key_env must be an environment-variable name "
        "(capital letters, digits and underscores, not starting with a digit) outside "
        f"OMLi's own {RESERVED_ENV_PREFIX} prefix and not a home-directory or proxy variable",
    )

    return ModelEntry(
        model_id=model_id,
        display_name=str(data["display_name"]),
        family=str(data["family"]),
        provider_kind=provider_kind,
        adapter=str(data["adapter"]),
        endpoint=str(data["endpoint"]),
        pin=pin,
        capabilities=caps,
        params_default=params,
        reproducible=reproducible,
        status=str(data["status"]),
        test_suite_ref=(str(data["test_suite_ref"]) if data.get("test_suite_ref") else None),
        serving_tag=serving_tag,
        api_key_env=api_key_env,
        pricing=(_parse_pricing(data["pricing"], model_id) if data.get("pricing") else None),
    )


def parse_registry(data: Mapping[str, Any]) -> ModelRegistry:
    """Validate a registry mapping and build an immutable ModelRegistry."""
    _require(isinstance(data, Mapping), "registry must be a mapping at top level")
    meta = data.get("metadata")
    _require(isinstance(meta, Mapping), "registry missing 'metadata' mapping")
    missing = [k for k in _META_REQUIRED if k not in meta]
    _require(not missing, f"registry metadata missing keys: {', '.join(missing)}")
    _require(str(meta["status"]) in MODEL_STATUSES, f"registry status must be one of {MODEL_STATUSES}")

    models_data = data.get("models")
    _require(isinstance(models_data, list) and models_data, "registry must list at least one model")

    entries = tuple(_parse_entry(m) for m in models_data)
    seen: set[str] = set()
    for e in entries:
        _require(e.model_id not in seen, f"duplicate model_id {e.model_id!r} in registry")
        seen.add(e.model_id)

    metadata = ModelRegistryMetadata(
        registry_id=str(meta["registry_id"]),
        registry_version=str(meta["registry_version"]),
        authored_at=str(meta["authored_at"]),
        authored_by=str(meta["authored_by"]),
        status=str(meta["status"]),
        description=(str(meta["description"]) if meta.get("description") else None),
    )
    return ModelRegistry(metadata=metadata, models=entries)


def load_registry(path: str | Path) -> ModelRegistry:
    """Load and validate a model registry from YAML."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return parse_registry(data)
