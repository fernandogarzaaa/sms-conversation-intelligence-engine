"""AI / model layer.

Contains the `LLMProvider` abstraction and the components that *use* a provider:
the multi-intent classifier, the entity extractor, and the constrained response
generator. None of these make decisions — they propose structured data and draft
language, which the deterministic layers then bound and validate.
"""

from sms_engine.ai.provider import (
    LLMProvider,
    ProviderResponse,
    GenerateSpec,
)
from sms_engine.ai.mock_provider import MockProvider
from sms_engine.ai.classifier import MultiIntentClassifier
from sms_engine.ai.extractor import EntityExtractor
from sms_engine.ai.generator import ResponseGenerator

__all__ = [
    "LLMProvider",
    "ProviderResponse",
    "GenerateSpec",
    "MockProvider",
    "MultiIntentClassifier",
    "EntityExtractor",
    "ResponseGenerator",
]
