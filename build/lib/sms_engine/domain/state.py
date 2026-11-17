"""Persistent per-contact conversation state.

State is a *projection* of the event log + message history (see
`sms_engine.application.state_builder`). It is rebuilt deterministically on every
inbound message so a late message is never applied to a stale snapshot.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from sms_engine.domain.enums import (
    CampaignStatus,
    IntentType,
    ReferralStatus,
    SuppressionReason,
)


class Identity(BaseModel):
    name: str | None = None
    previous_name: str | None = None
    verified: bool = False
    # provenance
    source_message_id: str | None = None
    updated_at: datetime | None = None


class Fact(BaseModel):
    """An extracted fact. Provenance is mandatory."""

    key: str
    value: str
    source_message_id: str
    timestamp: datetime
    confidence: float = Field(ge=0.0, le=1.0)


class IntentRecord(BaseModel):
    type: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    source_message_id: str
    timestamp: datetime


class OpenIssue(BaseModel):
    id: str
    description: str
    source_message_id: str
    opened_at: datetime
    resolved: bool = False
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class Offer(BaseModel):
    """Something already presented to the customer (referral terms, review link, discount)."""

    kind: str  # "referral_terms" | "review_link" | "referral_link" | "discount" | ...
    detail: str
    presented_at: datetime
    source_message_id: str | None = None


class FutureIntent(BaseModel):
    id: str
    description: str
    who: str | None = None
    what: str | None = None
    when_text: str | None = None  # e.g. "spring", "next month" (not normalized to a date)
    source_message_id: str
    created_at: datetime
    followup_scheduled_for: datetime | None = None


class Suppression(BaseModel):
    active: bool = False
    reason: SuppressionReason | None = None
    since: datetime | None = None
    released_at: datetime | None = None
    note: str | None = None


class ConversationState(BaseModel):
    contact_id: str
    conversation_id: str
    client_id: str

    identity: Identity = Field(default_factory=Identity)

    # message ids only; full bodies live in the message store
    sent_messages: list[str] = Field(default_factory=list)
    received_messages: list[str] = Field(default_factory=list)

    facts: list[Fact] = Field(default_factory=list)
    intents: list[IntentRecord] = Field(default_factory=list)
    open_issues: list[OpenIssue] = Field(default_factory=list)
    offers_presented: list[Offer] = Field(default_factory=list)
    links_sent: list[str] = Field(default_factory=list)

    referral_status: ReferralStatus = ReferralStatus.NONE
    future_intents: list[FutureIntent] = Field(default_factory=list)

    campaign_status: CampaignStatus = CampaignStatus.PENDING
    suppression: Suppression = Field(default_factory=Suppression)

    last_interaction: datetime | None = None
    # Event-log version this projection was built from. Used for queue invalidation.
    version: int = 0

    # ---- derived helpers (no IO) -------------------------------------------------

    @property
    def has_unresolved_issue(self) -> bool:
        return any(not i.resolved for i in self.open_issues)

    @property
    def opted_out(self) -> bool:
        return (
            self.suppression.active
            and self.suppression.reason == SuppressionReason.OPT_OUT
        ) or self.campaign_status == CampaignStatus.OPTED_OUT

    @property
    def referral_already_committed(self) -> bool:
        return self.referral_status in (
            ReferralStatus.COMMITTED,
            ReferralStatus.COMPLETED,
        )

    def offer_kinds_presented(self) -> set[str]:
        return {o.kind for o in self.offers_presented}

    def latest_intent_types(self) -> set[IntentType]:
        if not self.intents:
            return set()
        last_msg = self.intents[-1].source_message_id
        return {i.type for i in self.intents if i.source_message_id == last_msg}
