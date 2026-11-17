"""Configurable thresholds. No arbitrary graduation is hardcoded in the engine —
automation eligibility per intent is data, and starts DISABLED unless the
operator's config enables it.

`AutomationConfig` can be loaded from TOML (see `config/automation.example.toml`)
so that turning on auto-send for an intent is a reviewed config change, not a
code change (DECISIONS D7).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class IntentAutomationRule(BaseModel):
    enabled: bool = False
    # minimum classifier confidence for the intent to be auto-eligible
    min_confidence: float = Field(0.9, ge=0.0, le=1.0)
    # minimum human-approval rate over a minimum sample before it SHOULD be enabled
    graduation_min_approval: float = Field(0.95, ge=0.0, le=1.0)
    graduation_min_sample: int = Field(50, ge=1)


class CampaignConfig(BaseModel):
    """Campaign steps (review request, referral ask/link) the engine may send."""

    enabled: bool = True
    # when true, every campaign message is drafted into the review queue instead
    # of being queued for automatic send
    require_review: bool = False


class SendingConfig(BaseModel):
    max_attempts: int = Field(3, ge=1)
    retry_backoff_seconds: int = Field(300, ge=1)


class FollowupConfig(BaseModel):
    # days after a volunteered future intent before a follow-up is drafted
    delay_days: int = Field(30, ge=1)


class AutomationConfig(BaseModel):
    # global confidence floor: below this, always human review
    global_confidence_floor: float = Field(0.6, ge=0.0, le=1.0)
    # escalate a contact once this many issues are open at the same time
    # (mirrors the sample client's escalation rule "two or more unresolved issues")
    escalate_on_open_issues: int = Field(2, ge=1)
    # per-intent automation rules
    intents: dict[str, IntentAutomationRule] = Field(
        default_factory=lambda: {
            "PURE_POSITIVE": IntentAutomationRule(enabled=True, min_confidence=0.9),
            "QUESTION": IntentAutomationRule(enabled=True, min_confidence=0.8),
        }
    )
    campaigns: CampaignConfig = Field(default_factory=CampaignConfig)
    sending: SendingConfig = Field(default_factory=SendingConfig)
    followups: FollowupConfig = Field(default_factory=FollowupConfig)

    def rule_for(self, intent: str) -> IntentAutomationRule:
        return self.intents.get(intent, IntentAutomationRule(enabled=False))

    @classmethod
    def default(cls) -> AutomationConfig:
        return cls()

    @classmethod
    def conservative(cls) -> AutomationConfig:
        """Everything routes to a human. Used to demonstrate false-negative bias."""
        return cls(intents={}, campaigns=CampaignConfig(require_review=True))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationConfig:
        return cls.model_validate(data)

    @classmethod
    def from_toml(cls, path: str | Path) -> AutomationConfig:
        with open(path, "rb") as fh:
            return cls.from_dict(tomllib.load(fh))
