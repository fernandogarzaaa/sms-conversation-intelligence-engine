"""LLMProvider abstraction.

    LLMProvider (ABC)
    ├── MockProvider              deterministic, rule-based, no network (tests, demo, default)
    └── HTTPLLMProvider           real models over HTTP  (sms_engine.ai.llm)
        ├── OpenAICompatibleProvider   /chat/completions
        │     ├── OpenAIProvider       api.openai.com
        │     ├── OpenRouterProvider   openrouter.ai
        │     └── LocalProvider        Ollama / vLLM / LM Studio (cloud-vs-local comparison)
        └── AnthropicProvider          /v1/messages

Every method returns a `ProviderResponse` carrying the parsed payload, the raw
text, the model name, measured latency, and token usage / cost — so the eval
harness can compare providers on latency / cost / accuracy without assuming
which is better. Use `sms_engine.ai.factory.provider_from_env()` to pick one.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class ProviderResponse:
    payload: dict
    raw: str
    model: str
    latency_ms: float = 0.0
    # populated only by providers that meter cost; mock leaves it 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class GenerateSpec:
    """Everything the generator is allowed to see (brief §10)."""

    customer_message: str
    history: list[dict]                 # [{direction, text}]
    state_summary: dict
    client_context: dict
    allowed_actions: list[str]
    forbidden_actions: list[str]
    retrieved_context: dict
    intents: list[str]
    entities: dict
    persona_name: str = "Agent"
    contact_name: str | None = None
    max_chars: int = 320


class LLMProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def classify_intents(self, text: str, state_summary: dict) -> ProviderResponse:
        """Return {"intents": [{"type","confidence","rationale"}], "entities": {..}, "flags": {..}}."""

    @abc.abstractmethod
    def extract_entities(self, text: str, intents: list[str]) -> ProviderResponse:
        """Return a flat entity dict (see domain.entities.Entities)."""

    @abc.abstractmethod
    def generate_reply(self, spec: GenerateSpec) -> ProviderResponse:
        """Return {"text": str, "actions": [str, ...]}."""


_LAZY = {
    "HTTPLLMProvider", "OpenAICompatibleProvider", "OpenAIProvider", "OpenRouterProvider",
    "LocalProvider", "AnthropicProvider", "LLMError",
}


def __getattr__(name: str):
    # Real providers live in sms_engine.ai.llm (which imports this module);
    # re-export them lazily so `from sms_engine.ai.provider import OpenAIProvider`
    # keeps working without a circular import.
    if name in _LAZY:
        from sms_engine.ai import llm

        return getattr(llm, name)
    raise AttributeError(name)
