"""Human review: apply a reviewer decision, and capture it as evaluation data.

Every decision persists: the model classification + confidence, the generated
response, the human action, the human edits, and the final response. That row is
the training/eval signal (brief §12–13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sms_engine.application.state_builder import build_state
from sms_engine.application.timing import TimingEngine
from sms_engine.application.wiring import Engine
from sms_engine.domain.enums import EventType, ReviewAction, RoutingDecision, SuppressionReason
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.policy import PolicyOutcome
from sms_engine.domain.validation import ValidationResult
from sms_engine.policy import actions as A
from sms_engine.validation.engine import ValidationContext


@dataclass
class ReviewDecisionResult:
    review_id: str
    action: ReviewAction
    planned_response: PlannedResponse | None
    final_response: str | None
    validation: ValidationResult | None
    edited: bool


class ReviewService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def open_queue(self) -> list[dict]:
        return [dict(r) for r in self.e.reviews.open_queue()]

    def get(self, review_id: str) -> dict | None:
        r = self.e.reviews.get(review_id)
        return dict(r) if r else None

    def decide(
        self,
        review_id: str,
        *,
        action: ReviewAction | str,
        edited_text: str | None = None,
        reviewer: str | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> ReviewDecisionResult:
        e = self.e
        action = ReviewAction(action) if isinstance(action, str) else action
        now = now or e.clock.now()
        row = e.reviews.get(review_id)
        if row is None:
            raise ValueError(f"unknown review {review_id!r}")
        if row["status"] == "DECIDED":
            raise ValueError(f"review {review_id!r} already decided")

        conv_id = row["conversation_id"]
        contact_id = e.conversations.get(conv_id)["contact_id"]
        client_id = e.conversations.get(conv_id)["client_id"]
        client = e.clients.get(client_id)
        msg_id = row["message_id"]

        planned: PlannedResponse | None = None
        final_response: str | None = None
        validation: ValidationResult | None = None
        edited = False

        if action in (ReviewAction.APPROVE, ReviewAction.EDIT_AND_SEND):
            text = (edited_text or row["generated_response"] or "").strip()
            edited = action == ReviewAction.EDIT_AND_SEND
            state = build_state(
                contact_id=contact_id, conversation_id=conv_id, client_id=client_id,
                events=e.events.list_for_conversation(conv_id),
                messages=e.messages.list_for_conversation(conv_id),
            )
            # human-approved text is STILL validated (brief: never bypass validators)
            neutral_policy = PolicyOutcome(
                routing=RoutingDecision.HUMAN_REVIEW, risk=e_risk(row), allowed_actions=[A.ACKNOWLEDGEMENT],
            )
            validation = e.validator.validate(
                text,
                ValidationContext(
                    client=client, policy=neutral_policy, state=state,
                    retrieved=_empty_retrieved(client_id), actions=[A.ACKNOWLEDGEMENT], intents=[],
                ),
            )
            if not validation.passed:
                # do not send invalid text even on human approval; record and stop
                e.reviews.decide(
                    review_id, action=action.value, decided_at=now,
                    human_edits=edited_text, final_response=None, reviewer=reviewer,
                    notes=(notes or "") + " | BLOCKED: validation failed on approved text",
                )
                self._event(conv_id, contact_id, EventType.RESPONSE_REJECTED, now,
                            {"review_id": review_id, "reason": "validation failed on human text"})
                return ReviewDecisionResult(review_id, action, None, None, validation, edited)

            timing = e.timing.schedule(
                now, char_len=len(text), intent_count=1, is_question=False, salt=review_id
            )
            planned = PlannedResponse(
                id=e.ids.new("plan"),
                conversation_id=conv_id,
                contact_id=contact_id,
                in_reply_to=msg_id,
                built_at_version=e.events.input_version(conv_id),
                text=text,
                validation=validation,
                send_after=timing.send_after,
                created_at=now,
                status="PLANNED",
                model_used="human",
                actions=[A.ACKNOWLEDGEMENT],
            )
            e.queue.enqueue(planned)
            final_response = text
            self._event(conv_id, contact_id, EventType.RESPONSE_APPROVED, now,
                        {"review_id": review_id, "planned_response_id": planned.id, "human_edited": edited})

        elif action == ReviewAction.REJECT:
            self._event(conv_id, contact_id, EventType.RESPONSE_REJECTED, now, {"review_id": review_id})

        elif action == ReviewAction.SUPPRESS:
            self._event(conv_id, contact_id, EventType.SUPPRESSION_ACTIVATED, now,
                        {"reason": SuppressionReason.MANUAL.value, "since": now.isoformat(),
                         "note": notes or "suppressed by reviewer"})

        elif action == ReviewAction.ESCALATE:
            self._event(conv_id, contact_id, EventType.ESCALATED, now,
                        {"review_id": review_id, "note": notes or "escalated by reviewer"})
            self._event(conv_id, contact_id, EventType.SUPPRESSION_ACTIVATED, now,
                        {"reason": SuppressionReason.ESCALATED.value, "since": now.isoformat()})

        e.reviews.decide(
            review_id,
            action=action.value,
            decided_at=now,
            human_edits=edited_text,
            final_response=final_response,
            reviewer=reviewer,
            notes=notes,
        )
        return ReviewDecisionResult(review_id, action, planned, final_response, validation, edited)

    def _event(self, conv_id, contact_id, etype, now, payload):
        self.e.events.append(
            id=self.e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
            type=etype, created_at=now, payload=payload,
        )


def e_risk(row) -> "RiskLevel":
    from sms_engine.domain.enums import RiskLevel
    try:
        return RiskLevel(row["risk_level"])
    except Exception:
        return RiskLevel.MEDIUM


def _empty_retrieved(client_id: str):
    from sms_engine.domain.context import RetrievedContext
    return RetrievedContext(client_id=client_id)
