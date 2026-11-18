"""A planned (not yet sent) outbound response."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from sms_engine.domain.validation import ValidationResult


class PlannedResponse(BaseModel):
    id: str
    conversation_id: str
    contact_id: str
    # the message id this response is a reply to
    in_reply_to: str
    # event-log version the plan was computed against — the invalidation key
    built_at_version: int
    text: str
    variant_id: str | None = None
    validation: ValidationResult = Field(default_factory=ValidationResult.ok)
    send_after: datetime | None = None
    created_at: datetime
    # PLANNED | INVALIDATED | CANCELLED | SENT | FAILED | REJECTED | PENDING_REVIEW
    status: str = "PLANNED"
    # REPLY (answers an inbound message) | CAMPAIGN (review/referral step) | FOLLOWUP
    kind: str = "REPLY"
    model_used: str = "unknown"
    generation_latency_ms: float = 0.0
    # actions this response performs (so validators/policy can check them)
    actions: list[str] = Field(default_factory=list)
    # outbound transport bookkeeping
    send_attempts: int = 0
    last_error: str | None = None
