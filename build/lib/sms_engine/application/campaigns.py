"""Campaign steps: review request, referral ask, referral link.

This replaces "a timer fires a template". A campaign trigger (from the existing
campaign/timer system, an operator, or the API) is only a *request*. The engine
decides, deterministically, against the conversation state:

  - suppression is global: an opt-out, complaint, wrong number, escalation, or
    open issue blocks every campaign step (Maya)
  - nothing already presented is presented again
  - a customer who already said they will refer gets their own referral link,
    built from the client's link template and *this* contact's id — never the
    pitch again, never someone else's link (John)

Campaign copy comes from client-approved templates, not from the model. The text
still runs through the full validator battery before it can be queued.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sms_engine.application.state_builder import build_state
from sms_engine.application.wiring import Engine
from sms_engine.domain.classification import Classification
from sms_engine.domain.context import ClientContext, RetrievedContext
from sms_engine.domain.enums import EventType, ReviewKind, RiskLevel, RoutingDecision
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.state import ConversationState
from sms_engine.domain.validation import ValidationResult
from sms_engine.policy.engine import CAMPAIGN_KINDS, campaign_check, outbound_guard
from sms_engine.validation.engine import ValidationContext

log = logging.getLogger("sms_engine.campaigns")

DEFAULT_TEMPLATES: dict[str, str] = {
    "review_request": (
        "{greeting}thanks again for choosing {company}. If you have a minute, a quick "
        "review helps a small team like ours a lot: {review_link}"
    ),
    "referral_ask": (
        "{greeting}if you know anyone who could use a hand, we'd be glad to help them too. "
        "{program} You'll get {reward} once their job is done. Your link: {referral_link}"
    ),
    "referral_link": (
        "{greeting}thanks for spreading the word. Here's your personal link to pass along: "
        "{referral_link}"
    ),
}


@dataclass
class CampaignDecision:
    conversation_id: str
    requested_kind: str
    kind: str
    allowed: bool
    reason: str
    text: str | None = None
    validation: ValidationResult | None = None
    planned_response: PlannedResponse | None = None
    review_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "requested_kind": self.requested_kind,
            "kind": self.kind,
            "allowed": self.allowed,
            "reason": self.reason,
            "text": self.text,
            "validation": self.validation.model_dump() if self.validation else None,
            "planned_response_id": self.planned_response.id if self.planned_response else None,
            "status": (self.planned_response.status if self.planned_response else None),
            "send_after": (
                self.planned_response.send_after.isoformat()
                if self.planned_response and self.planned_response.send_after else None
            ),
            "review_id": self.review_id,
        }


def referral_link_for(client: ClientContext, contact_id: str) -> str | None:
    rp = client.referral_program
    if not rp or not rp.referral_link_template:
        return None
    try:
        return rp.referral_link_template.format(contact_id=contact_id, referral_base="")
    except (KeyError, IndexError, ValueError):
        return None


def render_campaign(kind: str, client: ClientContext, state: ConversationState) -> str:
    tpl = client.campaign_templates.get(kind) or DEFAULT_TEMPLATES[kind]
    name = state.identity.name
    rp = client.referral_program
    values = {
        "greeting": f"Hi {name}, " if name else "Hi, ",
        "name": name or "",
        "company": client.name,
        "persona": client.persona_name,
        "review_link": client.review_link,
        "referral_link": referral_link_for(client, state.contact_id) or "",
        "reward": rp.reward_text if rp else "",
        "program": rp.description if rp else "",
        "terms": rp.terms_summary if rp else "",
    }
    text = tpl.format(**values)
    return " ".join(text.split())


class CampaignService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def trigger(
        self,
        conversation_id: str,
        kind: str,
        *,
        now: datetime | None = None,
        source: str = "api",
        dry_run: bool = False,
    ) -> CampaignDecision:
        e = self.e
        now = now or e.clock.now()
        kind = (kind or "").strip().lower()
        with e.db.transaction():
            conv = e.conversations.get(conversation_id)
            if conv is None:
                raise ValueError("unknown conversation")
            if kind not in CAMPAIGN_KINDS:
                raise ValueError(f"unknown campaign kind {kind!r}; expected one of {list(CAMPAIGN_KINDS)}")
            client = e.clients.get(conv["client_id"])
            state = self._state(conv)
            check = campaign_check(
                kind, state, client, e.config,
                has_open_review=bool(e.reviews.open_for_conversation(conversation_id)),
            )
            decision = CampaignDecision(
                conversation_id=conversation_id, requested_kind=kind, kind=check.kind,
                allowed=check.allowed, reason=check.reason,
            )
            if check.allowed:
                text = render_campaign(check.kind, client, state)
                decision.text = text
                decision.validation = self._validate(text, client, state, check.actions)
                if not decision.validation.passed:
                    decision.allowed = False
                    decision.reason = "validator rejected the campaign text: " + "; ".join(
                        f.message for f in decision.validation.rejects
                    )

            if dry_run:  # nothing has been written yet; report the decision only
                return decision

            if decision.allowed:
                self._plan(decision, conv, client, check.actions, now)

            e.events.append(
                id=e.ids.new("evt"), conversation_id=conversation_id, contact_id=conv["contact_id"],
                type=EventType.CAMPAIGN_TRIGGERED, created_at=now,
                payload={
                    "kind": decision.kind, "requested_kind": kind, "source": source,
                    "allowed": decision.allowed, "reason": decision.reason,
                    "planned_response_id": decision.planned_response.id if decision.planned_response else None,
                    "review_id": decision.review_id,
                },
            )
        log.info("campaign trigger",
                 extra={"conversation_id": conversation_id, "kind": decision.kind,
                        "allowed": decision.allowed, "reason": decision.reason})
        return decision

    # -- internals -----------------------------------------------------------

    def _state(self, conv) -> ConversationState:
        e = self.e
        return build_state(
            contact_id=conv["contact_id"], conversation_id=conv["id"], client_id=conv["client_id"],
            events=e.events.list_for_conversation(conv["id"]),
            messages=e.messages.list_for_conversation(conv["id"]),
        )

    def _validate(self, text: str, client: ClientContext, state: ConversationState, actions: list[str]):
        guard = outbound_guard(state, risk=RiskLevel.LOW)
        guard.routing = RoutingDecision.AUTO_REPLY
        return self.e.validator.validate(
            text,
            ValidationContext(
                client=client, policy=guard, state=state,
                retrieved=RetrievedContext(client_id=client.client_id), actions=actions, intents=[],
            ),
        )

    def _plan(self, decision: CampaignDecision, conv, client: ClientContext, actions: list[str], now) -> None:
        e = self.e
        require_review = e.config.campaigns.require_review
        timing = e.timing.schedule(
            now, char_len=len(decision.text or ""), intent_count=1, is_question=False,
            salt=f"campaign:{conv['id']}:{decision.kind}:{now.isoformat()}",
            tz=e.client_timezone(client),
        )
        msgs = e.messages.list_for_conversation(conv["id"])
        anchor = next((m.id for m in reversed(msgs) if m.direction.value == "INBOUND"), None)
        plan = PlannedResponse(
            id=e.ids.new("plan"),
            conversation_id=conv["id"],
            contact_id=conv["contact_id"],
            in_reply_to=anchor or f"campaign:{decision.kind}",
            built_at_version=e.events.input_version(conv["id"]),
            text=decision.text or "",
            variant_id=f"campaign:{decision.kind}",
            validation=decision.validation or ValidationResult.ok(),
            send_after=timing.send_after,
            created_at=now,
            status="PENDING_REVIEW" if require_review else "PLANNED",
            kind="CAMPAIGN",
            model_used="campaign-template",
            actions=actions,
        )
        if require_review:
            e.planned.save(plan)
            decision.review_id = self._open_review(conv, plan, decision, now)
        else:
            e.queue.enqueue(plan)
        decision.planned_response = plan

    def _open_review(self, conv, plan: PlannedResponse, decision: CampaignDecision, now) -> str:
        e = self.e
        rid = e.ids.new("rev")
        e.reviews.create({
            "id": rid,
            "conversation_id": conv["id"],
            "message_id": plan.in_reply_to,
            "planned_response_id": plan.id,
            "created_at": now.isoformat(),
            "status": "OPEN",
            "kind": ReviewKind.CAMPAIGN.value,
            "recommended_action": "APPROVE",
            "risk_level": RiskLevel.LOW.value,
            "model_classification_json": Classification(model_used="campaign-template").model_dump_json(),
            "model_confidence": None,
            "generated_response": plan.text,
            "notes": f"campaign step {decision.kind}: {decision.reason}",
        })
        e.events.append(
            id=e.ids.new("evt"), conversation_id=conv["id"], contact_id=conv["contact_id"],
            type=EventType.HUMAN_REVIEW_REQUIRED, created_at=now,
            payload={"review_id": rid, "kind": ReviewKind.CAMPAIGN.value, "campaign": decision.kind},
        )
        return rid
