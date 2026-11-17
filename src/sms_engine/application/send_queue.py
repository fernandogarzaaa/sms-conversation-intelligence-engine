"""Send queue + the queue-invalidation race handler (brief §8).

A planned response records `built_at_version` = the conversation's event-log
version it was computed against. When a new inbound message arrives, the
conversation version increases and `invalidate_stale` marks every still-planned
response built against an older version as INVALIDATED (emitting a
RESPONSE_INVALIDATED event). The send worker re-checks the version at send time
as a second guard, so a stale response can never reach the customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sms_engine.domain.enums import EventType
from sms_engine.domain.planning import PlannedResponse
from sms_engine.infrastructure.ids import IdFactory
from sms_engine.infrastructure.repositories import EventRepo, PlannedResponseRepo


@dataclass
class SendQueue:
    planned: PlannedResponseRepo
    events: EventRepo
    ids: IdFactory

    def enqueue(self, p: PlannedResponse) -> None:
        self.planned.save(p)
        self.events.append(
            id=self.ids.new("evt"),
            conversation_id=p.conversation_id,
            contact_id=p.contact_id,
            type=EventType.RESPONSE_PLANNED,
            created_at=p.created_at,
            payload={
                "planned_response_id": p.id,
                "built_at_version": p.built_at_version,
                "send_after": p.send_after.isoformat() if p.send_after else None,
                "in_reply_to": p.in_reply_to,
            },
            caused_by=p.in_reply_to,
        )

    def invalidate_stale(self, conversation_id: str, contact_id: str, current_version: int, now: datetime) -> list[str]:
        """Mark every PLANNED/PENDING_REVIEW response older than current_version as INVALIDATED."""
        invalidated: list[str] = []
        for p in self.planned.open_for_conversation(conversation_id):
            if p.built_at_version < current_version:
                self.planned.set_status(p.id, "INVALIDATED")
                self.events.append(
                    id=self.ids.new("evt"),
                    conversation_id=conversation_id,
                    contact_id=contact_id,
                    type=EventType.RESPONSE_INVALIDATED,
                    created_at=now,
                    payload={
                        "planned_response_id": p.id,
                        "built_at_version": p.built_at_version,
                        "current_version": current_version,
                        "reason": "newer inbound message changed the conversation",
                    },
                    caused_by=p.in_reply_to,
                )
                invalidated.append(p.id)
        return invalidated

    def due(self, now: datetime) -> list[PlannedResponse]:
        out = []
        for p in self.planned.due(now):
            current = self.events.input_version(p.conversation_id)
            if p.built_at_version < current:
                # second guard: invalidate at send time
                self.planned.set_status(p.id, "INVALIDATED")
                self.events.append(
                    id=self.ids.new("evt"),
                    conversation_id=p.conversation_id,
                    contact_id=p.contact_id,
                    type=EventType.RESPONSE_INVALIDATED,
                    created_at=now,
                    payload={"planned_response_id": p.id, "reason": "stale at send time"},
                )
                continue
            out.append(p)
        return out

    def mark_sent(self, p: PlannedResponse, sent_message_id: str, now: datetime, *, offers=None, links=None) -> None:
        self.planned.set_status(p.id, "SENT")
        self.events.append(
            id=self.ids.new("evt"),
            conversation_id=p.conversation_id,
            contact_id=p.contact_id,
            type=EventType.RESPONSE_SENT,
            created_at=now,
            payload={
                "planned_response_id": p.id,
                "message_id": sent_message_id,
                "offers_presented": offers or [],
                "links_sent": links or [],
            },
            caused_by=p.in_reply_to,
        )
