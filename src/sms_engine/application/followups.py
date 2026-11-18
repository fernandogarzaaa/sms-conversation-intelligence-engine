"""Follow-up processing for volunteered future intents.

When a customer volunteers a future need ("my daughter is moving next spring"),
the pipeline schedules a follow-up. When it comes due, this service either
cancels it (the contact opted out or is suppressed) or drafts a short check-in
into the review queue. Follow-ups are always human-gated: they are outreach the
customer did not just ask for, so a person approves each one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sms_engine.application.state_builder import build_state
from sms_engine.application.wiring import Engine
from sms_engine.domain.classification import Classification
from sms_engine.domain.context import ClientContext, RetrievedContext
from sms_engine.domain.enums import EventType, ReviewKind, RiskLevel
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.state import ConversationState
from sms_engine.policy import actions as A
from sms_engine.policy.engine import outbound_guard
from sms_engine.validation.engine import ValidationContext


@dataclass
class FollowupOutcome:
    followup_id: str
    conversation_id: str
    status: str
    reason: str
    review_id: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def draft_followup(client: ClientContext, state: ConversationState, payload: dict) -> str:
    name = state.identity.name
    who = payload.get("who")
    what = payload.get("what")
    when = payload.get("when_text")
    greeting = f"Hi {name}, it's {client.persona_name} from {client.name}." if name else (
        f"Hi, it's {client.persona_name} from {client.name}."
    )
    subject = f"your {who}" if who else "you"
    need = f" with {what}" if what and what not in ("move", "moving") else (" with the move" if what else "")
    timing = f" around {when}" if when else ""
    return " ".join(
        f"{greeting} You mentioned {subject} might need a hand{need}{timing}. "
        "If that's still the plan, just reply and the office will reach out.".split()
    )


class FollowupService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def process_due(self, now: datetime | None = None) -> list[FollowupOutcome]:
        e = self.e
        now = now or e.clock.now()
        out: list[FollowupOutcome] = []
        with e.db.transaction():
            for row in e.followups.due(now):
                out.append(self._process(row, now))
        return out

    def _process(self, row, now: datetime) -> FollowupOutcome:
        e = self.e
        conv = e.conversations.get(row["conversation_id"])
        state = build_state(
            contact_id=conv["contact_id"], conversation_id=conv["id"], client_id=conv["client_id"],
            events=e.events.list_for_conversation(conv["id"]),
            messages=e.messages.list_for_conversation(conv["id"]),
        )
        client = e.clients.get(conv["client_id"])
        fid = row["id"]

        def done(status: str, reason: str, review_id: str | None = None) -> FollowupOutcome:
            e.followups.set_status(fid, status, at=now, review_id=review_id)
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv["id"], contact_id=conv["contact_id"],
                type=EventType.FOLLOWUP_DUE, created_at=now,
                payload={"followup_id": fid, "status": status, "reason": reason, "review_id": review_id},
            )
            return FollowupOutcome(fid, conv["id"], status, reason, review_id)

        if state.opted_out:
            return done("CANCELLED", "contact opted out")
        if state.suppression.active:
            reason = state.suppression.reason.value if state.suppression.reason else "ACTIVE"
            return done("CANCELLED", f"contact is suppressed ({reason})")

        payload = json.loads(row["payload_json"] or "{}")
        text = draft_followup(client, state, payload)
        guard = outbound_guard(state, risk=RiskLevel.LOW)
        validation = e.validator.validate(
            text,
            ValidationContext(
                client=client, policy=guard, state=state,
                retrieved=RetrievedContext(client_id=client.client_id),
                actions=[A.FOLLOWUP_OUTREACH], intents=[],
            ),
        )
        anchor = payload.get("source_message_id") or f"followup:{fid}"
        plan = PlannedResponse(
            id=e.ids.new("plan"),
            conversation_id=conv["id"],
            contact_id=conv["contact_id"],
            in_reply_to=anchor,
            built_at_version=e.events.input_version(conv["id"]),
            text=text,
            variant_id="followup:0",
            validation=validation,
            send_after=None,
            created_at=now,
            status="PENDING_REVIEW",
            kind="FOLLOWUP",
            model_used="followup-template",
            actions=[A.FOLLOWUP_OUTREACH],
        )
        e.planned.save(plan)
        rid = e.ids.new("rev")
        e.reviews.create({
            "id": rid,
            "conversation_id": conv["id"],
            "message_id": anchor,
            "planned_response_id": plan.id,
            "created_at": now.isoformat(),
            "status": "OPEN",
            "kind": ReviewKind.FOLLOWUP.value,
            "recommended_action": "APPROVE" if validation.passed else "REJECT",
            "risk_level": RiskLevel.LOW.value,
            "model_classification_json": Classification(model_used="followup-template").model_dump_json(),
            "model_confidence": None,
            "generated_response": text,
            "notes": row["reason"],
        })
        e.events.append(
            id=e.ids.new("evt"), conversation_id=conv["id"], contact_id=conv["contact_id"],
            type=EventType.HUMAN_REVIEW_REQUIRED, created_at=now,
            payload={"review_id": rid, "kind": ReviewKind.FOLLOWUP.value, "followup_id": fid},
        )
        return done("REVIEW_OPENED", "drafted a check-in for human approval", rid)
