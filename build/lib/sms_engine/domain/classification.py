"""Structured multi-intent classification output.

This is the *parsed* shape the system works with. The raw model output (whatever
the provider returns) is parsed into this and never used directly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sms_engine.domain.enums import IntentType


class IntentPrediction(BaseModel):
    type: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    # short human-readable reason the (mock) classifier fired this intent
    rationale: str = ""


class Classification(BaseModel):
    intents: list[IntentPrediction] = Field(default_factory=list)
    # free-form entity hints from the classifier; authoritative extraction is separate
    entities: dict[str, str] = Field(default_factory=dict)
    model_used: str = "unknown"
    latency_ms: float = 0.0
    raw: str = ""  # raw provider payload, for observability
    # set when the provider failed and this is a fail-safe empty classification
    error: str | None = None

    def types(self) -> set[IntentType]:
        return {i.type for i in self.intents}

    def confidence_of(self, t: IntentType) -> float:
        return max((i.confidence for i in self.intents if i.type == t), default=0.0)

    @property
    def top_confidence(self) -> float:
        return max((i.confidence for i in self.intents), default=0.0)

    @property
    def min_confidence(self) -> float:
        return min((i.confidence for i in self.intents), default=0.0)

    def has(self, t: IntentType) -> bool:
        return t in self.types()

    def has_question_like(self) -> bool:
        return self.has(IntentType.QUESTION)
