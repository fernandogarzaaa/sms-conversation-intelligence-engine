"""Policy engine output types."""

from __future__ import annotations

from pydantic import BaseModel, Field

from sms_engine.domain.enums import RiskLevel, RoutingDecision, SuppressionReason


class PolicyRuleHit(BaseModel):
    """One deterministic rule that fired, with why."""

    rule_id: str
    description: str
    routing: RoutingDecision | None = None
    risk: RiskLevel | None = None
    activate_suppression: SuppressionReason | None = None
    release_suppression: bool = False
    # rules can forbid specific downstream actions regardless of routing
    forbid_actions: list[str] = Field(default_factory=list)


class PolicyOutcome(BaseModel):
    routing: RoutingDecision
    risk: RiskLevel
    # union of every forbidden action across fired rules
    forbidden_actions: list[str] = Field(default_factory=list)
    suppression_activate: SuppressionReason | None = None
    suppression_release: bool = False
    rule_hits: list[PolicyRuleHit] = Field(default_factory=list)
    # the reason string surfaced to reviewers
    summary: str = ""
    # if routed AUTO_REPLY but this is set, the pipeline must confirm the response
    # is grounded in approved context or downgrade to HUMAN_REVIEW.
    requires_grounding: bool = False
    # the dominant intent that drove the routing decision (for eval bucketing)
    primary_intent: str | None = None
    # actions the response IS allowed to take (empty => acknowledgement only)
    allowed_actions: list[str] = Field(default_factory=list)

    @property
    def allows_automation(self) -> bool:
        return self.routing == RoutingDecision.AUTO_REPLY

    def forbids(self, action: str) -> bool:
        return action in self.forbidden_actions
