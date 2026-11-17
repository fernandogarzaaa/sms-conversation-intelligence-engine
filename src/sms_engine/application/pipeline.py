"""The inbound message pipeline.

    SMS event
      -> persist message + SMS_RECEIVED event (bumps conversation version)
      -> invalidate any stale queued response for this conversation
      -> rebuild state_before
      -> multi-intent classify
      -> extract entities
      -> deterministic policy decision (routing / risk / suppression / allowed actions)
      -> persist classification + state delta + suppression events
      -> rebuild state_after
      -> retrieve client context
      -> if AUTO_REPLY: generate -> validate -> (regen once) -> schedule + enqueue
         else:          generate a draft for the reviewer, open a review row
      -> future-intent -> schedule follow-up
      -> assemble + persist the decision trace
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sms_engine.ai.provider import GenerateSpec
from sms_engine.application.state_builder import build_state
from sms_engine.application.variants import VariantPool
from sms_engine.application.wiring import Engine
from sms_engine.domain.classification import Classification
from sms_engine.domain.context import ClientContext, RetrievedContext
from sms_engine.domain.entities import Entities
from sms_engine.domain.enums import (
    EventType,
    IntentType,
    ReferralStatus,
    RiskLevel,
    RoutingDecision,
)
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.policy import PolicyOutcome, PolicyRuleHit
from sms_engine.domain.state import ConversationState
from sms_engine.domain.trace import DecisionTrace
from sms_engine.domain.validation import ValidationResult
from sms_engine.policy import actions as A
from sms_engine.policy.engine import PolicyInput
from sms_engine.validation.engine import ValidationContext

_RESOLUTION_MARKERS = ("resolved", "fixed now", "taken care of", "sorted out", "all good now", "came back and")


@dataclass
class PipelineResult:
    conversation_id: str
    contact_id: str
    message: Message
    classification: Classification
    entities: Entities
    state_before: ConversationState
    state_after: ConversationState
    policy: PolicyOutcome
    retrieved: RetrievedContext
    routing: RoutingDecision
    draft_text: str | None
    validation: ValidationResult | None
    planned_response: PlannedResponse | None
    review_id: str | None
    followup_ids: list[str] = field(default_factory=list)
    trace: DecisionTrace | None = None
    latency_ms: float = 0.0

    @property
    def auto_sent_scheduled(self) -> bool:
        return self.planned_response is not None and self.planned_response.status == "PLANNED"


class InboundPipeline:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    # -- entrypoint ---------------------------------------------------------------

    def handle_inbound(
        self,
        *,
        client_id: str,
        contact_id: str,
        text: str,
        conversation_id: str | None = None,
        phone: str | None = None,
        neighborhood: str | None = None,
        received_at: datetime | None = None,
    ) -> PipelineResult:
        e = self.e
        t0 = time.perf_counter()
        now = received_at or e.clock.now()

        client = e.clients.get(client_id)
        if client is None:
            raise ValueError(f"unknown client_id {client_id!r}; ingest a context pack first")

        conv_id = self._ensure_conversation(client_id, contact_id, conversation_id, now, phone)

        # 1. persist inbound message + event (bumps version)
        msg = Message(
            id=e.ids.new("msg"),
            conversation_id=conv_id,
            direction=Direction.INBOUND,
            raw_text=text,
            created_at=now,
        )
        e.messages.add(msg)
        e.events.append(
            id=e.ids.new("evt"),
            conversation_id=conv_id,
            contact_id=contact_id,
            type=EventType.SMS_RECEIVED,
            created_at=now,
            payload={"message_id": msg.id, "chars": len(text)},
            caused_by=msg.id,
        )
        version_after_inbound = e.events.input_version(conv_id)

        # 2. queue invalidation — any response planned against an older version is stale
        invalidated = e.queue.invalidate_stale(conv_id, contact_id, version_after_inbound, now)

        # 3. state_before (projection through the inbound event, before this msg's analysis)
        state_before = self._build_state(conv_id, contact_id, client_id)

        trace = DecisionTrace(
            conversation_id=conv_id, message_id=msg.id, created_at=now, message_text=text
        )
        trace.add("INBOUND", f"received {len(text)} chars", message_id=msg.id, version=version_after_inbound)
        if invalidated:
            trace.add("QUEUE_INVALIDATION", f"invalidated {len(invalidated)} stale queued response(s)", ids=invalidated)

        # 4. classify
        classification = e.classifier.classify(text, self._state_summary(state_before))
        injection = "possible_prompt_injection=True" in (classification.entities.get("_flags", ""))
        trace.add(
            "CLASSIFIED",
            ", ".join(f"{i.type.value} {i.confidence:.2f}" for i in classification.intents) or "none",
            intents=[i.model_dump() for i in classification.intents],
            possible_prompt_injection=injection,
        )

        # 5. entities
        entities = e.extractor.extract(text, classification)
        trace.add("ENTITY", ", ".join(f"{k}={v}" for k, v in entities.as_facts_dict().items()) or "none",
                  entities=entities.model_dump(exclude={"raw"}))

        # 6. policy
        policy = e.policy.decide(
            PolicyInput(
                classification=classification,
                entities=entities,
                state_before=state_before,
                client=client,
            )
        )
        if injection and policy.routing == RoutingDecision.AUTO_REPLY:
            policy.routing = RoutingDecision.HUMAN_REVIEW
            policy.risk = max(policy.risk, RiskLevel.MEDIUM, key=lambda r: r.rank)
            policy.summary += "; [R_PROMPT_INJECTION] injection markers in inbound text -> forced human review"
            policy.rule_hits.append(
                PolicyRuleHit(
                    rule_id="R_PROMPT_INJECTION",
                    description="Inbound text contains prompt-injection markers; never auto-send.",
                    routing=RoutingDecision.HUMAN_REVIEW,
                    risk=RiskLevel.MEDIUM,
                )
            )
        trace.add("POLICY", policy.summary, routing=policy.routing.value, risk=policy.risk.value,
                  forbidden=policy.forbidden_actions, allowed=policy.allowed_actions)

        # 7. persist classification + state delta + suppression events
        self._persist_classification(conv_id, contact_id, msg, classification, now)
        delta = self._state_delta(classification, entities, state_before, msg, now)
        if delta:
            e.events.append(
                id=e.ids.new("evt"),
                conversation_id=conv_id,
                contact_id=contact_id,
                type=EventType.STATE_UPDATED,
                created_at=now,
                payload=delta,
                caused_by=msg.id,
            )
        self._apply_suppression_events(conv_id, contact_id, policy, state_before, msg, now, trace)

        # 8. state_after
        state_after = self._build_state(conv_id, contact_id, client_id)

        # 9. retrieval
        retrieved = e.retriever.retrieve(
            message_text=text,
            classification=classification,
            entities=entities,
            state=state_after,
            client=client,
        )
        trace.add("CONTEXT", f"grounded={retrieved.grounded}, faq={retrieved.retrieved_ids}",
                  retrieved=retrieved.model_dump())

        # 10. routing branch
        draft_text: str | None = None
        validation: ValidationResult | None = None
        planned: PlannedResponse | None = None
        review_id: str | None = None

        routing = policy.routing
        if routing == RoutingDecision.AUTO_REPLY:
            draft_text, validation, planned, routing, review_id = self._auto_branch(
                client, conv_id, contact_id, msg, classification, entities, state_after,
                policy, retrieved, neighborhood, now, trace
            )
        elif routing == RoutingDecision.SUPPRESS:
            # opt-out / already-opted-out: deterministic, no human needed
            trace.add("ROUTE", routing.value)
            trace.add("NO_AUTOMATED_MESSAGE", "routing=SUPPRESS")
        else:
            # HUMAN_REVIEW and ESCALATE: draft for the reviewer + open a review row
            draft_text, validation = self._draft_only(
                client, msg, classification, entities, state_after, policy, retrieved
            )
            review_id = self._open_review(
                conv_id, contact_id, msg, classification, policy, draft_text, now
            )
            trace.add("ROUTE", routing.value, review_id=review_id)
            if routing == RoutingDecision.ESCALATE:
                trace.add("NO_AUTOMATED_MESSAGE", "routing=ESCALATE")

        # 11. future intent -> follow-up task
        followups = self._schedule_followups(conv_id, contact_id, state_after, now, trace)

        # 12. trace finalize
        latency_ms = (time.perf_counter() - t0) * 1000.0
        trace.classification = classification.model_dump(exclude={"raw"})
        trace.entities = entities.model_dump(exclude={"raw"})
        trace.state_before = state_before.model_dump(mode="json")
        trace.state_after = state_after.model_dump(mode="json")
        trace.policy = policy.model_dump()
        trace.retrieved_context = retrieved.model_dump()
        trace.response_text = (planned.text if planned else draft_text)
        trace.validation = validation.model_dump() if validation else {}
        trace.routing = routing.value
        trace.latency_ms = latency_ms
        trace.model_used = classification.model_used
        e.traces.save(trace)

        return PipelineResult(
            conversation_id=conv_id,
            contact_id=contact_id,
            message=msg,
            classification=classification,
            entities=entities,
            state_before=state_before,
            state_after=state_after,
            policy=policy,
            retrieved=retrieved,
            routing=routing,
            draft_text=draft_text,
            validation=validation,
            planned_response=planned,
            review_id=review_id,
            followup_ids=followups,
            trace=trace,
            latency_ms=latency_ms,
        )

    # -- send worker ------------------------------------------------------------

    def process_due(self, now: datetime | None = None) -> list[Message]:
        """Send every planned response whose time has come and whose conversation
        has not moved on. Returns the outbound Messages created."""
        e = self.e
        now = now or e.clock.now()
        sent: list[Message] = []
        for p in e.queue.due(now):
            out = Message(
                id=e.ids.new("msg"),
                conversation_id=p.conversation_id,
                direction=Direction.OUTBOUND,
                raw_text=p.text,
                created_at=now,
                variant_id=p.variant_id,
            )
            e.messages.add(out)
            links = [u for u in _find_urls(p.text)]
            offers = [{"kind": a.split(":", 1)[1], "detail": p.text[:60]} for a in p.actions if a.startswith("repeat_offer:")]
            e.queue.mark_sent(p, out.id, now, offers=offers, links=links)
            sent.append(out)
        return sent

    # -- internals -----------------------------------------------------------------

    def _ensure_conversation(self, client_id, contact_id, conversation_id, now, phone) -> str:
        e = self.e
        if conversation_id:
            if e.conversations.get(conversation_id) is None:
                e.conversations.create(conversation_id, contact_id, client_id, now, phone)
            return conversation_id
        row = e.conversations.find_by_contact(contact_id)
        if row:
            return row["id"]
        cid = e.ids.new("conv")
        e.conversations.create(cid, contact_id, client_id, now, phone)
        e.events.append(
            id=e.ids.new("evt"), conversation_id=cid, contact_id=contact_id,
            type=EventType.JOB_COMPLETED, created_at=now, payload={"note": "job on file"},
        )
        return cid

    def _build_state(self, conv_id, contact_id, client_id) -> ConversationState:
        e = self.e
        return build_state(
            contact_id=contact_id,
            conversation_id=conv_id,
            client_id=client_id,
            events=e.events.list_for_conversation(conv_id),
            messages=e.messages.list_for_conversation(conv_id),
        )

    @staticmethod
    def _state_summary(st: ConversationState) -> dict:
        return {
            "name": st.identity.name,
            "has_unresolved_issue": st.has_unresolved_issue,
            "referral_status": st.referral_status.value,
            "opted_out": st.opted_out,
            "offers_presented": sorted(st.offer_kinds_presented()),
        }

    def _persist_classification(self, conv_id, contact_id, msg, classification: Classification, now):
        self.e.events.append(
            id=self.e.ids.new("evt"),
            conversation_id=conv_id,
            contact_id=contact_id,
            type=EventType.CLASSIFICATION_COMPLETED,
            created_at=now,
            payload={
                "source_message_id": msg.id,
                "timestamp": now.isoformat(),
                "intents": [{"type": i.type.value, "confidence": i.confidence} for i in classification.intents],
            },
            caused_by=msg.id,
        )

    def _state_delta(self, classification: Classification, entities: Entities,
                     state_before: ConversationState, msg: Message, now) -> dict:
        e = self.e
        types = classification.types()
        delta: dict = {}

        if IntentType.IDENTITY_CORRECTION in types and entities.corrected_name:
            delta["identity"] = {
                "name": entities.corrected_name,
                "verified": True,
                "source_message_id": msg.id,
            }

        facts = []
        conf = classification.top_confidence or 0.8
        for k, v in entities.as_facts_dict().items():
            facts.append({
                "key": k, "value": v, "source_message_id": msg.id,
                "timestamp": now.isoformat(), "confidence": conf,
            })
        if facts:
            delta["facts"] = facts

        if (IntentType.OPEN_ISSUE in types or IntentType.MIXED_SENTIMENT in types) and (
            entities.issue or IntentType.OPEN_ISSUE in types
        ):
            delta["open_issues"] = [{
                "id": e.ids.new("issue"),
                "description": entities.issue or "unresolved issue reported by customer",
                "source_message_id": msg.id,
                "opened_at": now.isoformat(),
            }]

        if IntentType.VOLUNTEERED_FUTURE_INTENT in types:
            desc = " ".join(
                x for x in [entities.future_person, entities.future_service, entities.future_timeframe] if x
            ) or "future service need"
            delta["future_intents"] = [{
                "id": e.ids.new("fi"),
                "description": desc,
                "who": entities.future_person,
                "what": entities.future_service,
                "when_text": entities.future_timeframe,
                "source_message_id": msg.id,
                "created_at": now.isoformat(),
            }]

        if types & {IntentType.REFERRAL_INTENT, IntentType.INLINE_REFERRAL}:
            delta["referral_status"] = ReferralStatus.COMMITTED.value

        # naive resolution detection
        if state_before.has_unresolved_issue and IntentType.PURE_POSITIVE in types:
            low = msg.raw_text.lower()
            if any(mk in low for mk in _RESOLUTION_MARKERS):
                delta["resolve_issues"] = [i.id for i in state_before.open_issues if not i.resolved]

        return delta

    def _apply_suppression_events(self, conv_id, contact_id, policy: PolicyOutcome,
                                  state_before: ConversationState, msg, now, trace):
        e = self.e
        if policy.suppression_activate and not state_before.suppression.active:
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                type=EventType.SUPPRESSION_ACTIVATED, created_at=now,
                payload={"reason": policy.suppression_activate.value, "since": now.isoformat(),
                         "note": policy.summary[:200]},
                caused_by=msg.id,
            )
            trace.add("SUPPRESSION", f"ACTIVATED ({policy.suppression_activate.value})")
        elif policy.suppression_activate and state_before.suppression.active:
            trace.add("SUPPRESSION", f"ALREADY ACTIVE ({state_before.suppression.reason})")

        if policy.routing == RoutingDecision.ESCALATE:
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                type=EventType.ESCALATED, created_at=now,
                payload={"note": policy.summary[:200]}, caused_by=msg.id,
            )

        if policy.suppression_release and state_before.suppression.active:
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                type=EventType.SUPPRESSION_RELEASED, created_at=now,
                payload={"released_at": now.isoformat()}, caused_by=msg.id,
            )
            trace.add("SUPPRESSION", "RELEASED")

    def _gen_spec(self, client: ClientContext, msg, classification, entities,
                  state: ConversationState, policy: PolicyOutcome, retrieved) -> GenerateSpec:
        e = self.e
        history = [
            {"direction": m.direction.value, "text": m.raw_text}
            for m in e.messages.list_for_conversation(msg.conversation_id)[-6:]
        ]
        return GenerateSpec(
            customer_message=msg.raw_text,
            history=history,
            state_summary=self._state_summary(state),
            client_context=client.model_dump(),
            allowed_actions=policy.allowed_actions,
            forbidden_actions=policy.forbidden_actions,
            retrieved_context={
                "grounded": retrieved.grounded,
                "faq_hits": [f.model_dump() for f in retrieved.faq_hits],
                "snippets": retrieved.snippets,
            },
            intents=[i.type.value for i in classification.intents],
            entities=entities.as_facts_dict() | (
                {"corrected_name": entities.corrected_name} if entities.corrected_name else {}
            ),
            persona_name=client.persona_name,
            contact_name=state.identity.name,
            max_chars=client.max_sms_chars,
        )

    def _validate(self, text, client, policy, state, retrieved, actions, classification) -> ValidationResult:
        return self.e.validator.validate(
            text,
            ValidationContext(
                client=client, policy=policy, state=state, retrieved=retrieved,
                actions=actions, intents=[i.type.value for i in classification.intents],
            ),
        )

    def _draft_only(self, client, msg, classification, entities, state, policy, retrieved):
        spec = self._gen_spec(client, msg, classification, entities, state, policy, retrieved)
        draft = self.e.generator.generate(spec)
        validation = self._validate(draft.text, client, policy, state, retrieved, draft.actions, classification)
        return draft.text, validation

    def _auto_branch(self, client, conv_id, contact_id, msg, classification, entities,
                     state, policy, retrieved, neighborhood, now, trace):
        e = self.e

        if policy.requires_grounding and not retrieved.grounded:
            trace.add("ROUTE", "AUTO_REPLY downgraded to HUMAN_REVIEW: answer not grounded in an approved FAQ")
            draft_text, validation = self._draft_only(client, msg, classification, entities, state, policy, retrieved)
            review_id = self._open_review(conv_id, contact_id, msg, classification, policy, draft_text, now,
                                          recommended=RoutingDecision.HUMAN_REVIEW.value)
            return draft_text, validation, None, RoutingDecision.HUMAN_REVIEW, review_id

        spec = self._gen_spec(client, msg, classification, entities, state, policy, retrieved)
        draft = e.generator.generate(spec)
        validation = self._validate(draft.text, client, policy, state, retrieved, draft.actions, classification)

        if not validation.passed:
            trace.add("VALIDATION", "reject: " + "; ".join(f.message for f in validation.rejects))
            draft = e.generator.generate(spec)  # single regenerate attempt
            validation = self._validate(draft.text, client, policy, state, retrieved, draft.actions, classification)
            if not validation.passed:
                trace.add("VALIDATION", "reject again -> HUMAN_REVIEW")
                review_id = self._open_review(conv_id, contact_id, msg, classification, policy, draft.text, now,
                                              recommended=RoutingDecision.HUMAN_REVIEW.value)
                return draft.text, validation, None, RoutingDecision.HUMAN_REVIEW, review_id

        trace.add("VALIDATION", "passed" + (f" ({len(validation.warnings)} warning(s))" if validation.warnings else ""))

        # variant + timing
        kind = "answer_question" if A.ANSWER_QUESTION in draft.actions else (
            "confirm_identity" if A.CONFIRM_IDENTITY in draft.actions else "acknowledgement"
        )
        sel = e.variants.select(kind, contact_id=contact_id, neighborhood=neighborhood)
        final_text = VariantPool.render(sel.template, draft.text)
        # re-validate the rendered text (framing wrapper could, in principle, break a rule)
        validation = self._validate(final_text, client, policy, state, retrieved, draft.actions, classification)
        if not validation.passed:
            review_id = self._open_review(conv_id, contact_id, msg, classification, policy, final_text, now,
                                          recommended=RoutingDecision.HUMAN_REVIEW.value)
            return final_text, validation, None, RoutingDecision.HUMAN_REVIEW, review_id

        is_q = classification.has(IntentType.QUESTION)
        timing = e.timing.schedule(
            now, char_len=len(msg.raw_text), intent_count=len(classification.intents),
            is_question=is_q, salt=msg.id,
        )
        planned = PlannedResponse(
            id=e.ids.new("plan"),
            conversation_id=conv_id,
            contact_id=contact_id,
            in_reply_to=msg.id,
            built_at_version=e.events.input_version(conv_id),
            text=final_text,
            variant_id=sel.variant_id,
            validation=validation,
            send_after=timing.send_after,
            created_at=now,
            status="PLANNED",
            model_used=draft.model_used,
            generation_latency_ms=draft.latency_ms,
            actions=draft.actions,
        )
        e.queue.enqueue(planned)
        trace.add("ROUTE", "AUTO_REPLY", planned_response_id=planned.id,
                  send_after=timing.send_after.isoformat(), timing=timing.reason, variant=sel.variant_id)
        return final_text, validation, planned, RoutingDecision.AUTO_REPLY, None

    def _open_review(self, conv_id, contact_id, msg, classification: Classification,
                     policy: PolicyOutcome, draft_text, now, recommended: str | None = None) -> str:
        e = self.e
        rid = e.ids.new("rev")
        e.reviews.create({
            "id": rid,
            "conversation_id": conv_id,
            "message_id": msg.id,
            "planned_response_id": None,
            "created_at": now.isoformat(),
            "status": "OPEN",
            "recommended_action": recommended or policy.routing.value,
            "risk_level": policy.risk.value,
            "model_classification_json": classification.model_dump_json(exclude={"raw"}),
            "model_confidence": classification.top_confidence,
            "generated_response": draft_text,
        })
        return rid

    def _schedule_followups(self, conv_id, contact_id, state: ConversationState, now, trace) -> list[str]:
        e = self.e
        ids: list[str] = []
        existing = {r["id"] for r in e.followups.for_conversation(conv_id)}
        for fi in state.future_intents:
            key = f"followup_{fi.id}"
            if key in existing:
                continue
            due = (now + timedelta(days=30)).replace(hour=15, minute=7, second=0, microsecond=0)
            e.followups.add(
                id=key, conversation_id=conv_id, contact_id=contact_id, due_at=due,
                reason=f"volunteered future intent: {fi.description}",
                payload={"future_intent_id": fi.id, "who": fi.who, "when_text": fi.when_text},
                created_at=now,
            )
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                type=EventType.SCHEDULED_FOLLOWUP, created_at=now,
                payload={"future_intent_id": fi.id, "due_at": due.isoformat()},
            )
            ids.append(key)
            trace.add("FOLLOWUP", f"scheduled for {fi.description}", due_at=due.isoformat())
        return ids


def _find_urls(text: str) -> list[str]:
    import re
    return re.findall(r"https?://[^\s)]+", text or "")
