"""Per-client context pack + retrieval result."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FAQItem(BaseModel):
    id: str
    question: str
    answer: str
    tags: list[str] = Field(default_factory=list)
    approved: bool = True  # only approved answers are eligible for grounded auto-reply


class ReferralProgram(BaseModel):
    description: str
    reward_text: str  # human-readable, e.g. "a thank-you gift" (NO invented dollar amounts)
    referral_link_template: str  # "{referral_base}/{contact_id}"
    terms_summary: str


class ClientContext(BaseModel):
    client_id: str
    name: str
    brand_voice: str
    persona_name: str = "Agent"
    services: list[str] = Field(default_factory=list)
    service_area: list[str] = Field(default_factory=list)
    hours: str = ""
    booking_policy: str = ""
    referral_program: ReferralProgram | None = None
    review_link: str = ""
    crew_names: list[str] = Field(default_factory=list)
    faq: list[FAQItem] = Field(default_factory=list)
    escalation_rules: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    approved_phrases: list[str] = Field(default_factory=list)
    # deterministic validator inputs
    allowed_url_domains: list[str] = Field(default_factory=list)
    max_sms_chars: int = 320
    # recipient-local timezone for quiet hours (IANA name, e.g. "America/Chicago")
    timezone: str | None = None
    # inbound numbers this client owns (E.164); routes provider webhooks to the client
    phone_numbers: list[str] = Field(default_factory=list)
    # sender number for outbound SMS (E.164); defaults to the first phone number
    sms_from_number: str | None = None
    # optional overrides for campaign copy: keys review_request | referral_ask | referral_link
    campaign_templates: dict[str, str] = Field(default_factory=dict)
    # provenance of the pack
    sources: list[str] = Field(default_factory=list)
    synthetic: bool = True


class RetrievedContext(BaseModel):
    """What context retrieval decided is relevant to this inbound message."""

    client_id: str
    faq_hits: list[FAQItem] = Field(default_factory=list)
    facts_used: list[str] = Field(default_factory=list)
    snippets: list[str] = Field(default_factory=list)
    retrieved_ids: list[str] = Field(default_factory=list)
    grounded: bool = False  # True only if an approved FAQ answer covers the question
