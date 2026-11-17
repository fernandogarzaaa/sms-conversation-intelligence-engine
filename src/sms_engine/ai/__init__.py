"""AI / model layer.

Contains the `LLMProvider` abstraction and the components that *use* a provider:
the multi-intent classifier, the entity extractor, and the constrained response
generator. None of these make decisions — they propose structured data and draft
language, which the deterministic layers then bound and validate.
"""

from sms_engine.ai.classifier import MultiIntentClassifier
from sms_engine.ai.extractor import EntityExtractor
from sms_engine.ai.factory import provider_from_env
from sms_engine.ai.generator import ResponseGenerator
from sms_engine.ai.llm import (
    AnthropicProvider,
    HTTPLLMProvider,
    LLMError,
    LocalProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    OpenRouterProvider,
)
from sms_engine.ai.mock_provider import MockProvider
from sms_engine.ai.provider import (
    GenerateSpec,
    LLMProvider,
    ProviderResponse,
)

__all__ = [
    "LLMProvider",
    "ProviderResponse",
    "GenerateSpec",
    "MockProvider",
    "MultiIntentClassifier",
    "EntityExtractor",
    "ResponseGenerator",
    "HTTPLLMProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "LocalProvider",
    "AnthropicProvider",
    "LLMError",
    "provider_from_env",
]
