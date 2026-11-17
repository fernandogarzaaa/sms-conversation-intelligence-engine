"""Append-only conversation events."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from sms_engine.domain.enums import EventType


class Event(BaseModel):
    """One immutable fact about what happened, in order.

    `seq` is the per-conversation monotonic sequence number assigned by the event
    store. `payload` is intentionally loose JSON — reducers know how to read it.
    """

    id: str
    conversation_id: str
    contact_id: str
    type: EventType
    seq: int
    created_at: datetime
    payload: dict = Field(default_factory=dict)
    # what caused this event: message id, review id, scheduler, etc.
    caused_by: str | None = None
