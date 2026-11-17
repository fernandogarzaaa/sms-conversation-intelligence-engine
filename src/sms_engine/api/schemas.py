"""Request/response models for the API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InboundMessageIn(BaseModel):
    client_id: str
    contact_id: str
    text: str
    conversation_id: str | None = None
    phone: str | None = None
    neighborhood: str | None = None


class EventIn(BaseModel):
    conversation_id: str
    contact_id: str
    type: str
    payload: dict = Field(default_factory=dict)


class ReviewDecisionIn(BaseModel):
    action: str  # APPROVE | EDIT_AND_SEND | REJECT | SUPPRESS | ESCALATE
    edited_text: str | None = None
    reviewer: str | None = None
    notes: str | None = None


class SuppressIn(BaseModel):
    reason: str = "MANUAL"
    note: str | None = None


class ReleaseIn(BaseModel):
    note: str | None = None


class ClientIn(BaseModel):
    client_id: str
    name: str
    website_text: str = ""
    faq: list[dict] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    sample_replies: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
