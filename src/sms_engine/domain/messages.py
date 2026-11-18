"""Inbound / outbound SMS messages."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Direction(str, Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class Message(BaseModel):
    """A single SMS in the thread.

    `raw_text` is stored verbatim and is treated as untrusted data everywhere
    downstream (see SECURITY.md).
    """

    id: str
    conversation_id: str
    direction: Direction
    raw_text: str
    created_at: datetime
    # For outbound: which variant template produced it, and whether a human touched it.
    variant_id: str | None = None
    human_edited: bool = False
    # SMS-provider id (e.g. Twilio MessageSid). Unique when present: the idempotency key.
    provider_message_id: str | None = None
    meta: dict = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.raw_text
