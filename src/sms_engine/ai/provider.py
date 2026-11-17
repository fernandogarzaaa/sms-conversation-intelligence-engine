"""LLMProvider abstraction.

    LLMProvider (ABC)
    ├── MockProvider     deterministic, rule-based, no network  (this build)
    ├── OpenAIProvider   interface-only stub
    └── LocalProvider    interface-only stub  (future cloud-vs-local comparison)

Every method returns a `ProviderResponse` carrying the parsed payload, the raw
text, the model name, and measured latency — so the eval harness can compare
providers on latency / cost / accuracy without assuming which is better.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class ProviderResponse:
    payload: dict
    raw: str
    model: str
    latency_ms: float = 0.0
    # populated only by providers that meter cost; mock leaves it 0
    cost_usd: float = 0.0


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
        """Return {"intents": [{"type","confidence","rationale"}], "entities": {..}}."""

    @abc.abstractmethod
    def extract_entities(self, text: str, intents: list[str]) -> ProviderResponse:
        """Return a flat entity dict (see domain.entities.Entities)."""

    @abc.abstractmethod
    def generate_reply(self, spec: GenerateSpec) -> ProviderResponse:
        """Return {"text": str, "actions": [str, ...]}."""


class NotConfiguredProvider(LLMProvider):
    """Base for real providers that aren't wired in this build."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _fail(self):
        raise NotImplementedError(
            f"{self.name} is an interface-only stub in this build. "
            "Use MockProvider, or implement this method against the real API."
        )

    def classify_intents(self, text: str, state_summary: dict) -> ProviderResponse:  # noqa: D401
        self._fail()

    def extract_entities(self, text: str, intents: list[str]) -> ProviderResponse:
        self._fail()

    def generate_reply(self, spec: GenerateSpec) -> ProviderResponse:
        self._fail()


class OpenAIProvider(NotConfiguredProvider):
    def __init__(self) -> None:
        super().__init__("openai")


class LocalProvider(NotConfiguredProvider):
    def __init__(self) -> None:
        super().__init__("local")
