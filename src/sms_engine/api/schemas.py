"""Request models for the API. Bounded lengths keep a hostile client from
pushing megabytes of "SMS" through the classifier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReviewActionName = Literal["APPROVE", "EDIT_AND_SEND", "REJECT", "SUPPRESS", "ESCALATE"]
SuppressionReasonName = Literal[
    "OPEN_ISSUE", "COMPLAINT", "OPT_OUT", "WRONG_NUMBER", "ESCALATED", "MANUAL", "LOW_CONFIDENCE"
]
CampaignKindName = Literal["review_request", "referral_ask", "referral_link"]

_ID = dict(min_length=1, max_length=128)


class InboundMessageIn(BaseModel):
    client_id: str = Field(**_ID)
    contact_id: str = Field(**_ID)
    text: str = Field(max_length=2000)
    conversation_id: str | None = Field(None, max_length=128)
    phone: str | None = Field(None, max_length=32)
    neighborhood: str | None = Field(None, max_length=128)
    # SMS-provider message id; makes the call idempotent (retries are replayed)
    provider_message_id: str | None = Field(None, max_length=128)
    # the CRM's name on file, used only when the conversation is first created
    contact_name: str | None = Field(None, max_length=128)


class EventIn(BaseModel):
    conversation_id: str = Field(**_ID)
    contact_id: str = Field(**_ID)
    type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


class JobCompletedIn(BaseModel):
    client_id: str = Field(**_ID)
    contact_id: str = Field(**_ID)
    phone: str | None = Field(None, max_length=32)
    customer_name: str | None = Field(None, max_length=128)
    neighborhood: str | None = Field(None, max_length=128)
    job_id: str | None = Field(None, max_length=128)
    payload: dict = Field(default_factory=dict)


class CampaignTriggerIn(BaseModel):
    kind: CampaignKindName
    # evaluate and render, but write nothing
    dry_run: bool = False


class ReviewDecisionIn(BaseModel):
    action: ReviewActionName
    edited_text: str | None = Field(None, max_length=1600)
    reviewer: str | None = Field(None, max_length=128)
    notes: str | None = Field(None, max_length=1000)


class ValidateTextIn(BaseModel):
    text: str = Field(max_length=1600)


class SuppressIn(BaseModel):
    reason: SuppressionReasonName = "MANUAL"
    note: str | None = Field(None, max_length=500)


class ReleaseIn(BaseModel):
    note: str | None = Field(None, max_length=500)


class ResolveIssueIn(BaseModel):
    note: str | None = Field(None, max_length=500)
    release_suppression: bool = False
    resolved_by: str | None = Field(None, max_length=128)


class ClientIn(BaseModel):
    client_id: str = Field(**_ID)
    name: str = Field(min_length=1, max_length=200)
    website_text: str = Field("", max_length=50_000)
    faq: list[dict] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    sample_replies: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
