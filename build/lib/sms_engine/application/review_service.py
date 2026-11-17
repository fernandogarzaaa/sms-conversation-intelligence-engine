"""Human review: apply a reviewer decision, and capture it as evaluation data.

Every decision persists: the model classification + confidence, the generated
response, the human action, the human edits, and the final response. That row is
the training/eval signal (brief §12–13).

Approval never bypasses the guardrails. Human text is validated against
`outbound_guard(state)` — the actions the *current* conversation state forbids —
so a reviewer cannot, for example, add a referral ask to a thread with an open
issue, or message a contact who has since opted out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sms_engine.application.state_builder import build_state
from sms_engine.application.wiring import Engine
from sms_engine.domain.context import RetrievedContext
from sms_engine.domain.enums import (
    EventType,
    ReviewAction,
    ReviewKind,
    RiskLevel,
    SuppressionReason,
)
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.state import ConversationState
from sms_engine.domain.validation import ValidationResult
from sms_engine.policy import actions as A
from sms_engine.policy.engine import outbound_guard
from sms_engine.validation.engine import ValidationContext


@dataclass
class ReviewDecisionResult:
    review_id: str
    action: ReviewAction
    planned_response: PlannedResponse | None
    final_response: str | None
    validation: ValidationResult | None
    edited: bool
    blocked: bool = False


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


class ReviewService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def open_queue(self) -> list[dict]:
        return [dict(r) for r in self.e.reviews.open_queue()]

    def get(self, review_id: str) -> dict | None:
        r = self.e.reviews.get(review_id)
        return dict(r) if r else None

    # -- validation preview --------------------------------------------------

    def validate_text(self, review_id: str, text: str) -> ValidationResult:
        """Run the exact checks `decide` would run on this text, without deciding."""
        e = self.e
        row = e.reviews.get(review_id)
        if row is None:
            raise ValueError(f"unknown review {review_id!r}")
        state, client = self._state_and_client(row["conversation_id"])
        plan = e.planned.get(row["planned_response_id"]) if row["planned_response_id"] else None
        actions = plan.actions if plan else [A.ACKNOWLEDGEMENT]
        return self._validate(text, state, client, actions, row)

    # -- decision ------------------------------------------------------------

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
        with e.db.transaction():
            return self._decide(review_id, action, edited_text, reviewer, notes, now)

    def _decide(self, review_id, action: ReviewAction, edited_text, reviewer, notes, now) -> ReviewDecisionResult:
        e = self.e
        row = e.reviews.get(review_id)
        if row is None:
            raise ValueError(f"unknown review {review_id!r}")
        if row["status"] == "DECIDED":
            raise ValueError(f"review {review_id!r} already decided")
        if row["status"] == "SUPERSEDED":
            raise ValueError(
                f"review {review_id!r} was superseded by a newer customer message; decide the newer review"
            )

        conv_id = row["conversation_id"]
        conv = e.conversations.get(conv_id)
        contact_id = conv["contact_id"]
        kind = ReviewKind(row["kind"] or "REPLY")
        plan = e.planned.get(row["planned_response_id"]) if row["planned_response_id"] else None

        # An "Approve" that carries changed text is an edit. Recording it as a
        # clean approval would inflate the learning loop's no-edit approval rate.
        base_text = plan.text if plan else (row["generated_response"] or "")
        if action == ReviewAction.APPROVE and edited_text is not None and _norm(edited_text) != _norm(base_text):
            action = ReviewAction.EDIT_AND_SEND
        human_edits = edited_text if action == ReviewAction.EDIT_AND_SEND else None

        planned: PlannedResponse | None = None
        final_response: str | None = None
        validation: ValidationResult | None = None
        edited = action == ReviewAction.EDIT_AND_SEND

        if action in (ReviewAction.APPROVE, ReviewAction.EDIT_AND_SEND):
            text = (edited_text if edited else base_text) or ""
            text = text.strip()
            state, client = self._state_and_client(conv_id)
            actions = plan.actions if plan else [A.ACKNOWLEDGEMENT]
            validation = self._validate(text, state, client, actions, row)
            if not validation.passed:
                # do not send invalid text even on human approval; record and stop
                e.reviews.decide(
                    review_id, action=action.value, decided_at=now,
                    human_edits=human_edits, final_response=None, reviewer=reviewer,
                    notes=((notes + " | ") if notes else "") + "BLOCKED: validation failed on approved text",
                )
                if plan and plan.status == "PENDING_REVIEW":
                    e.planned.set_status(plan.id, "REJECTED")
                self._event(conv_id, contact_id, EventType.RESPONSE_REJECTED, now,
                            {"review_id": review_id, "reason": "validation failed on human text",
                             "codes": [f.code.value for f in validation.rejects]})
                return ReviewDecisionResult(review_id, action, None, None, validation, edited, blocked=True)

            tz = e.client_timezone(client)
            timing = e.timing.schedule(
                now, char_len=len(text), intent_count=1, is_question=False, salt=review_id, tz=tz
            )
            version = e.events.input_version(conv_id)
            if plan is not None:
                plan.text = text
                plan.validation = validation
                plan.send_after = timing.send_after
                plan.status = "PLANNED"
                plan.built_at_version = version
                plan.model_used = "human" if edited else plan.model_used
                e.planned.save(plan)
                planned = plan
            else:
                planned = PlannedResponse(
                    id=e.ids.new("plan"),
                    conversation_id=conv_id,
                    contact_id=contact_id,
                    in_reply_to=row["message_id"],
                    built_at_version=version,
                    text=text,
                    validation=validation,
                    send_after=timing.send_after,
                    created_at=now,
                    status="PLANNED",
                    kind=kind.value,
                    model_used="human",
                    actions=actions,
                )
                e.queue.enqueue(planned)
            final_response = text
            self._event(conv_id, contact_id, EventType.RESPONSE_APPROVED, now,
                        {"review_id": review_id, "planned_response_id": planned.id, "human_edited": edited})

        elif action == ReviewAction.REJECT:
            if plan and plan.status == "PENDING_REVIEW":
                e.planned.set_status(plan.id, "REJECTED")
            self._event(conv_id, contact_id, EventType.RESPONSE_REJECTED, now, {"review_id": review_id})

        elif action == ReviewAction.SUPPRESS:
            if plan and plan.status == "PENDING_REVIEW":
                e.planned.set_status(plan.id, "REJECTED")
            self._event(conv_id, contact_id, EventType.SUPPRESSION_ACTIVATED, now,
                        {"reason": SuppressionReason.MANUAL.value, "since": now.isoformat(),
                         "note": notes or "suppressed by reviewer"})
            self._invalidate_queue(conv_id, contact_id, now)

        elif action == ReviewAction.ESCALATE:
            if plan and plan.status == "PENDING_REVIEW":
                e.planned.set_status(plan.id, "REJECTED")
            self._event(conv_id, contact_id, EventType.ESCALATED, now,
                        {"review_id": review_id, "note": notes or "escalated by reviewer"})
            self._event(conv_id, contact_id, EventType.SUPPRESSION_ACTIVATED, now,
                        {"reason": SuppressionReason.ESCALATED.value, "since": now.isoformat()})
            self._invalidate_queue(conv_id, contact_id, now)

        e.reviews.decide(
            review_id,
            action=action.value,
            decided_at=now,
            human_edits=human_edits,
            final_response=final_response,
            reviewer=reviewer,
            notes=notes,
        )
        if kind == ReviewKind.FOLLOWUP:
            for f in e.followups.for_conversation(conv_id):
                if f["review_id"] == review_id:
                    status = "SENT_FOR_DELIVERY" if final_response else "DISMISSED"
                    e.followups.set_status(f["id"], status, at=now)
        return ReviewDecisionResult(review_id, action, planned, final_response, validation, edited)

    # -- internals -----------------------------------------------------------

    def _state_and_client(self, conv_id: str):
        e = self.e
        conv = e.conversations.get(conv_id)
        state = build_state(
            contact_id=conv["contact_id"], conversation_id=conv_id, client_id=conv["client_id"],
            events=e.events.list_for_conversation(conv_id),
            messages=e.messages.list_for_conversation(conv_id),
        )
        return state, e.clients.get(conv["client_id"])

    def _validate(self, text: str, state: ConversationState, client, actions: list[str], row) -> ValidationResult:
        guard = outbound_guard(state, risk=_risk(row))
        return self.e.validator.validate(
            text,
            ValidationContext(
                client=client, policy=guard, state=state,
                retrieved=RetrievedContext(client_id=state.client_id), actions=actions, intents=[],
            ),
        )

    def _invalidate_queue(self, conv_id: str, contact_id: str, now: datetime) -> None:
        v = self.e.events.input_version(conv_id)
        self.e.queue.invalidate_stale(conv_id, contact_id, v + 1, now)

    def _event(self, conv_id, contact_id, etype, now, payload):
        self.e.events.append(
            id=self.e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
            type=etype, created_at=now, payload=payload,
        )


def _risk(row) -> RiskLevel:
    try:
        return RiskLevel(row["risk_level"])
    except Exception:
        return RiskLevel.MEDIUM


# Backwards-compatible alias (older code imported this helper).
e_risk = _risk
