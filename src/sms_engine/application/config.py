"""Configurable thresholds. No arbitrary graduation is hardcoded in the engine —
automation eligibility per intent is data, and starts DISABLED.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class IntentAutomationRule(BaseModel):
    enabled: bool = False
    # minimum classifier confidence for the intent to be auto-eligible
    min_confidence: float = 0.9
    # minimum human-approval rate over a minimum sample before it SHOULD be enabled
    graduation_min_approval: float = 0.95
    graduation_min_sample: int = 50


class AutomationConfig(BaseModel):
    # global confidence floor: below this, always human review
    global_confidence_floor: float = 0.6
    # per-intent automation rules
    intents: dict[str, IntentAutomationRule] = Field(
        default_factory=lambda: {
            "PURE_POSITIVE": IntentAutomationRule(enabled=True, min_confidence=0.9),
            "QUESTION": IntentAutomationRule(enabled=True, min_confidence=0.8),
        }
    )

    def rule_for(self, intent: str) -> IntentAutomationRule:
        return self.intents.get(intent, IntentAutomationRule(enabled=False))

    @classmethod
    def default(cls) -> "AutomationConfig":
        return cls()

    @classmethod
    def conservative(cls) -> "AutomationConfig":
        """Everything routes to a human. Used to demonstrate false-negative bias."""
        return cls(intents={})
