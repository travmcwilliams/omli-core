"""Model adapters — the "Open Model Local integrator" seam.

Generic transport only. Prompts, thresholds and output shape are pack-defined; these
classes know nothing about any domain.
"""

from omli.models.base import ModelClient
from omli.models.local import LocalEchoModel
from omli.models.ollama import OllamaModel
from omli.models.factory import make_model
from omli.models.spec import ModelSpec
from omli.models.errors import ModelError, ModelTimeout, ModelUnavailable
from omli.models.registry import (
    ModelCapabilities,
    ModelEntry,
    ModelPin,
    ModelRegistry,
    ModelRegistryMetadata,
    SamplingDefaults,
    load_registry,
    parse_registry,
)
from omli.models.provider import (
    EchoProvider,
    Message,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    NormalizedToolCall,
    OllamaProvider,
    OpenAICompatibleProvider,
    SamplingParams,
    ToolSchema,
    default_provider_table,
)

__all__ = [
    "ModelClient",
    "LocalEchoModel",
    "OllamaModel",
    "make_model",
    "ModelSpec",
    "ModelError",
    "ModelTimeout",
    "ModelUnavailable",
    "ModelRegistry",
    "ModelRegistryMetadata",
    "ModelEntry",
    "ModelCapabilities",
    "ModelPin",
    "SamplingDefaults",
    "load_registry",
    "parse_registry",
    "ModelProvider",
    "EchoProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "default_provider_table",
    "ModelRequest",
    "ModelResponse",
    "Message",
    "ToolSchema",
    "NormalizedToolCall",
    "SamplingParams",
]
