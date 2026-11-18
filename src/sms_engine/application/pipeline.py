"""The inbound message pipeline.

    SMS event
      -> [idempotency] same provider message id seen before? replay the stored decision
      -> [tx 1] persist message + SMS_RECEIVED event (bumps conversation version)
             invalidate any stale queued response for this conversation
             supersede any open review whose draft was written for an older message
      -> [tx 2] rebuild state_before
             multi-intent classify -> extract entities
             deterministic policy decision (routing / risk / suppression / allowed actions)
             persist classification + state delta + suppression events
             rebuild state_after -> retrieve client context
             if AUTO_REPLY: generate -> validate -> (regen once) -> schedule + enqueue
             else:          generate a draft for the reviewer, open a review row
             future intent -> schedule follow-up
             assemble + persist the decision trace
      -> if tx 2 fails for any reason: roll it back and open a HIGH-risk review so a
         person sees the message (fail safe: an error never sends, never drops)

The whole call holds the database lock, so two messages for the same contact
can never be analysed against each other's half-written state.
"""

from __future__ import annotations

import logging
import re
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
    ReviewKind,
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
from sms_engine.policy.optout import detect_opt_out
from sms_engine.validation.engine import ValidationContext

log = logging.getLogger("sms_engine.pipeline")

_RESOLUTION_MARKERS = ("resolved", "fixed now", "taken care of", "sorted out", "all good now", "came back and")
_URL_RE = re.compile(r"https?://[^\s)]+")

# Hard cap on stored inbound text. SMS concatenation tops out well below this;
# anything longer is not a real SMS and is truncated before analysis.
MAX_INBOUND_CHARS = 2000


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
    # True when this call replayed a previously processed provider message
    duplicate: bool = False
    # set when analysis failed and the message was routed to a human instead
    error: str | None = None
    invalidated_ids: list[str] = field(default_factory=list)
    superseded_review_ids: list[str] = field(default_factory=list)

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
        provider_message_id: str | None = None,
        contact_name: str | None = None,
    ) -> PipelineResult:
        e = self.e
        t0 = time.perf_counter()
        now = received_at or e.clock.now()
        text = (text or "")[:MAX_INBOUND_CHARS]

        with e.db.conn.lock:
            client = e.clients.get(client_id)
            if client is None:
                raise ValueError(f"unknown client_id {client_id!r}; ingest a context pack first")

            # 0. idempotency: providers retry webhooks; never process one SMS twice
            if provider_message_id:
                existing = e.messages.get_by_provider_id(provider_message_id)
                if existing is not None:
                    replay = self._replay(existing)
                    if replay is not None:
                        return replay
                    # stored but never analysed (process died mid-flight): analyse it now
                    conv = e.conversations.get(existing.conversation_id)
                    return self._analyse_safely(
                        client, existing.conversation_id, conv["contact_id"], existing,
                        e.events.input_version(existing.conversation_id), [], [],
                        neighborhood, now, t0,
                    )

            # 1. persist the inbound message first, and make everything older stale
            with e.db.transaction():
                conv_id = self._ensure_conversation(
                    client_id, contact_id, conversation_id, now, phone, neighborhood, contact_name
                )
                msg = Message(
                    id=e.ids.new("msg"),
                    conversation_id=conv_id,
                    direction=Direction.INBOUND,
                    raw_text=text,
                    created_at=now,
                    provider_message_id=provider_message_id,
                )
                e.messages.add(msg)
                e.events.append(
                    id=e.ids.new("evt"),
                    conversation_id=conv_id,
                    contact_id=contact_id,
                    type=EventType.SMS_RECEIVED,
                    created_at=now,
                    payload={"message_id": msg.id, "chars": len(text),
                             "provider_message_id": provider_message_id},
                    caused_by=msg.id,
                )
                version = e.events.input_version(conv_id)
                invalidated = e.queue.invalidate_stale(conv_id, contact_id, version, now)
                superseded = self._supersede_open_reviews(conv_id, contact_id, msg, now)

            return self._analyse_safely(
                client, conv_id, contact_id, msg, version, invalidated, superseded,
                neighborhood, now, t0,
            )

    def _analyse_safely(self, client, conv_id, contact_id, msg, version, invalidated, superseded,
                        neighborhood, now, t0) -> PipelineResult:
        e = self.e
        try:
            with e.db.transaction():
                return self._analyse(
                    client, conv_id, contact_id, msg, version, invalidated, superseded,
                    neighborhood, now, t0,
                )
        except Exception as exc:  # fail safe: a person sees the message, nothing is sent
            log.exception("inbound analysis failed; routing to human review",
                          extra={"conversation_id": conv_id, "message_id": msg.id})
            with e.db.transaction():
                return self._fail_safe(client, conv_id, contact_id, msg, version, invalidated,
                                       superseded, now, t0, exc)

    # -- analysis ---------------------------------------------------------------

    def _analyse(self, client: ClientContext, conv_id: str, contact_id: str, msg: Message,
                 version: int, invalidated: list[str], superseded: list[str],
                 neighborhood: str | None, now: datetime, t0: float) -> PipelineResult:
        e = self.e
        text = msg.raw_text
        client_id = client.client_id

        # state_before (projection through the inbound event, before this msg's analysis)
        state_before = self._build_state(conv_id, contact_id, client_id)

        trace = DecisionTrace(
            conversation_id=conv_id, message_id=msg.id, created_at=now, message_text=text
        )
        trace.add("INBOUND", f"received {len(text)} chars", message_id=msg.id, version=version)
        if invalidated:
            trace.add("QUEUE_INVALIDATION", f"invalidated {len(invalidated)} stale queued response(s)", ids=invalidated)
        if superseded:
            trace.add("REVIEW_SUPERSEDED", f"closed {len(superseded)} open review(s) drafted for an older message",
                      ids=superseded)

        # classify
        classification = e.classifier.classify(text, self._state_summary(state_before))
        injection = "possible_prompt_injection=True" in (classification.entities.get("_flags", ""))
        provider_error = getattr(classification, "error", None)
        trace.add(
            "CLASSIFIED",
            ", ".join(f"{i.type.value} {i.confidence:.2f}" for i in classification.intents)
            or ("none (provider error)" if provider_error else "none"),
            intents=[i.model_dump() for i in classification.intents],
            possible_prompt_injection=injection,
            **({"provider_error": provider_error} if provider_error else {}),
        )

        # entities
        entities = e.extractor.extract(text, classification)
        # deterministic opt-out backstop: compliance never depends on the model
        if not entities.opt_out_phrase:
            phrase = detect_opt_out(text)
            if phrase:
                entities.opt_out_phrase = phrase
                trace.add("OPT_OUT_BACKSTOP", f"deterministic opt-out phrase {phrase!r} (model did not flag it)")
        trace.add("ENTITY", ", ".join(f"{k}={v}" for k, v in entities.as_facts_dict().items()) or "none",
                  entities=entities.model_dump(exclude={"raw"}))

        # policy
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
        if provider_error and policy.routing == RoutingDecision.AUTO_REPLY:
            # degraded mode (primary model failed, rules fallback answered): never auto-send
            policy.routing = RoutingDecision.HUMAN_REVIEW
            policy.risk = max(policy.risk, RiskLevel.MEDIUM, key=lambda r: r.rank)
            policy.summary += "; [R_PROVIDER_DEGRADED] model provider failed -> human review"
            policy.rule_hits.append(
                PolicyRuleHit(
                    rule_id="R_PROVIDER_DEGRADED",
                    description="The model provider failed for this message; no automated send.",
                    routing=RoutingDecision.HUMAN_REVIEW,
                    risk=RiskLevel.MEDIUM,
                )
            )
        trace.add("POLICY", policy.summary, routing=policy.routing.value, risk=policy.risk.value,
                  forbidden=policy.forbidden_actions, allowed=policy.allowed_actions)

        # persist classification + state delta + suppression events
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

        state_after = self._build_state(conv_id, contact_id, client_id)

        retrieved = e.retriever.retrieve(
            message_text=text,
            classification=classification,
            entities=entities,
            state=state_after,
            client=client,
        )
        trace.add("CONTEXT", f"grounded={retrieved.grounded}, faq={retrieved.retrieved_ids}",
                  retrieved=retrieved.model_dump())

        # routing branch
        draft_text: str | None = None
        validation: ValidationResult | None = None
        planned: PlannedResponse | None = None
        review_id: str | None = None

        routing = policy.routing
        if routing == RoutingDecision.AUTO_REPLY:
            draft_text, validation, planned, routing, review_id = self._auto_branch(
                client, conv_id, contact_id, msg, classification, entities, state_after,
                policy, retrieved, neighborhood, version, now, trace
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

        followups = self._schedule_followups(conv_id, contact_id, state_after, now, trace)

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

        log.info(
            "inbound processed",
            extra={"conversation_id": conv_id, "message_id": msg.id, "routing": routing.value,
                   "risk": policy.risk.value, "primary_intent": policy.primary_intent,
                   "latency_ms": round(latency_ms, 1)},
        )

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
            invalidated_ids=invalidated,
            superseded_review_ids=superseded,
        )

    def _fail_safe(self, client, conv_id, contact_id, msg, version, invalidated, superseded,
                   now, t0, exc: BaseException) -> PipelineResult:
        e = self.e
        err = f"{type(exc).__name__}: {exc}"[:300]
        state = self._build_state(conv_id, contact_id, client.client_id)
        classification = Classification(model_used=e.provider_name)
        policy = PolicyOutcome(
            routing=RoutingDecision.HUMAN_REVIEW,
            risk=RiskLevel.HIGH,
            summary=f"[R_PROCESSING_FAILED] analysis failed ({err}); routed to a human, nothing sent",
            rule_hits=[PolicyRuleHit(
                rule_id="R_PROCESSING_FAILED",
                description="The pipeline raised an error; fail safe to human review.",
                routing=RoutingDecision.HUMAN_REVIEW, risk=RiskLevel.HIGH,
            )],
        )
        e.events.append(
            id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
            type=EventType.PROCESSING_FAILED, created_at=now,
            payload={"message_id": msg.id, "error": err}, caused_by=msg.id,
        )
        review_id = self._open_review(conv_id, contact_id, msg, classification, policy, None, now)
        trace = DecisionTrace(conversation_id=conv_id, message_id=msg.id, created_at=now, message_text=msg.raw_text)
        trace.add("INBOUND", f"received {len(msg.raw_text)} chars", message_id=msg.id, version=version)
        trace.add("ERROR", err)
        trace.add("ROUTE", RoutingDecision.HUMAN_REVIEW.value, review_id=review_id)
        trace.state_before = trace.state_after = state.model_dump(mode="json")
        trace.policy = policy.model_dump()
        trace.routing = RoutingDecision.HUMAN_REVIEW.value
        trace.latency_ms = (time.perf_counter() - t0) * 1000.0
        trace.model_used = e.provider_name
        e.traces.save(trace)
        return PipelineResult(
            conversation_id=conv_id, contact_id=contact_id, message=msg,
            classification=classification, entities=Entities(), state_before=state,
            state_after=state, policy=policy,
            retrieved=RetrievedContext(client_id=client.client_id),
            routing=RoutingDecision.HUMAN_REVIEW, draft_text=None, validation=None,
            planned_response=None, review_id=review_id, trace=trace,
            latency_ms=trace.latency_ms, error=err,
            invalidated_ids=invalidated, superseded_review_ids=superseded,
        )

    def _replay(self, msg: Message) -> PipelineResult | None:
        """Rebuild the result of an already-processed message from its stored trace."""
        e = self.e
        trace = e.traces.for_message(msg.conversation_id, msg.id)
        if trace is None:
            return None
        conv = e.conversations.get(msg.conversation_id)
        plans = e.planned.for_reply_to(msg.id)
        reviews = e.reviews.for_message(msg.id)
        state_before = ConversationState.model_validate(trace.state_before) if trace.state_before else None
        state_after = ConversationState.model_validate(trace.state_after) if trace.state_after else None
        fallback_state = state_after or state_before or ConversationState(
            contact_id=conv["contact_id"], conversation_id=msg.conversation_id, client_id=conv["client_id"]
        )
        return PipelineResult(
            conversation_id=msg.conversation_id,
            contact_id=conv["contact_id"],
            message=msg,
            classification=Classification.model_validate(trace.classification or {}),
            entities=Entities.model_validate(trace.entities or {}),
            state_before=state_before or fallback_state,
            state_after=state_after or fallback_state,
            policy=PolicyOutcome.model_validate(trace.policy) if trace.policy else PolicyOutcome(
                routing=RoutingDecision.HUMAN_REVIEW, risk=RiskLevel.MEDIUM),
            retrieved=RetrievedContext.model_validate(trace.retrieved_context)
            if trace.retrieved_context else RetrievedContext(client_id=conv["client_id"]),
            routing=RoutingDecision(trace.routing or "HUMAN_REVIEW"),
            draft_text=trace.response_text,
            validation=ValidationResult.model_validate(trace.validation) if trace.validation else None,
            planned_response=plans[0] if plans else None,
            review_id=reviews[0]["id"] if reviews else None,
            trace=trace,
            latency_ms=0.0,
            duplicate=True,
        )

    # -- send worker (kept here for backwards compatibility) ------------------

    def process_due(self, now: datetime | None = None) -> list[Message]:
        """Send every planned response whose time has come and whose conversation
        has not moved on. Returns the outbound Messages created."""
        from sms_engine.application.send_worker import SendWorker

        return SendWorker(self.e).process_due(now)

    # -- internals -----------------------------------------------------------------

    def _ensure_conversation(self, client_id, contact_id, conversation_id, now, phone,
                             neighborhood=None, contact_name=None) -> str:
        e = self.e
        if conversation_id:
            if e.conversations.get(conversation_id) is None:
                e.conversations.create(conversation_id, contact_id, client_id, now, phone, neighborhood)
                self._job_on_file(conversation_id, contact_id, now, contact_name)
            else:
                e.conversations.update_contact_details(conversation_id, phone=phone, neighborhood=neighborhood)
            return conversation_id
        row = e.conversations.find_by_contact(contact_id, client_id)
        if row:
            e.conversations.update_contact_details(row["id"], phone=phone, neighborhood=neighborhood)
            return row["id"]
        cid = e.ids.new("conv")
        e.conversations.create(cid, contact_id, client_id, now, phone, neighborhood)
        self._job_on_file(cid, contact_id, now, contact_name)
        return cid

    def _job_on_file(self, conv_id, contact_id, now, contact_name) -> None:
        payload = {"note": "job on file"}
        if contact_name:
            payload["customer_name"] = contact_name
        self.e.events.append(
            id=self.e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
            type=EventType.JOB_COMPLETED, created_at=now, payload=payload,
        )

    def _supersede_open_reviews(self, conv_id, contact_id, msg: Message, now) -> list[str]:
        e = self.e
        ids: list[str] = []
        for r in e.reviews.open_for_conversation(conv_id):
            e.reviews.supersede(r["id"], at=now, by_message_id=msg.id)
            if r["planned_response_id"]:
                plan = e.planned.get(r["planned_response_id"])
                if plan and plan.status == "PENDING_REVIEW":
                    e.planned.set_status(plan.id, "INVALIDATED")
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                type=EventType.REVIEW_SUPERSEDED, created_at=now,
                payload={"review_id": r["id"], "superseded_by_message_id": msg.id},
                caused_by=msg.id,
            )
            ids.append(r["id"])
        return ids

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
                "model": classification.model_used,
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
                delta["resolution_note"] = "customer reported the issue resolved"

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
            from sms_engine.domain.enums import SuppressionReason

            # an opt-out always wins over a softer active suppression (e.g. OPEN_ISSUE)
            if (policy.suppression_activate == SuppressionReason.OPT_OUT
                    and state_before.suppression.reason != SuppressionReason.OPT_OUT):
                e.events.append(
                    id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                    type=EventType.SUPPRESSION_ACTIVATED, created_at=now,
                    payload={"reason": SuppressionReason.OPT_OUT.value, "since": now.isoformat(),
                             "note": "opt-out supersedes earlier suppression"},
                    caused_by=msg.id,
                )
                trace.add("SUPPRESSION", "UPGRADED to OPT_OUT")
            else:
                reason = state_before.suppression.reason.value if state_before.suppression.reason else "?"
                trace.add("SUPPRESSION", f"ALREADY ACTIVE ({reason})")

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
                     state, policy, retrieved, neighborhood, version, now, trace):
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

        # variant + timing (usage history comes from the DB so it survives restarts)
        kind = "answer_question" if A.ANSWER_QUESTION in draft.actions else (
            "confirm_identity" if A.CONFIRM_IDENTITY in draft.actions else "acknowledgement"
        )
        conv_row = e.conversations.get(conv_id)
        nb = neighborhood or (conv_row["neighborhood"] if conv_row else None)
        sel = e.variants.select(
            kind, contact_id=contact_id, neighborhood=nb,
            history=e.planned.variant_history(contact_id),
            neighborhood_used=e.planned.neighborhood_variants(nb, kind) if nb else None,
            ack_text=draft.text,
        )
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
            is_question=is_q, salt=msg.id, tz=e.client_timezone(client),
        )
        planned = PlannedResponse(
            id=e.ids.new("plan"),
            conversation_id=conv_id,
            contact_id=contact_id,
            in_reply_to=msg.id,
            built_at_version=version,
            text=final_text,
            variant_id=sel.variant_id,
            validation=validation,
            send_after=timing.send_after,
            created_at=now,
            status="PLANNED",
            kind="REPLY",
            model_used=draft.model_used,
            generation_latency_ms=draft.latency_ms,
            actions=draft.actions,
        )
        e.queue.enqueue(planned)
        trace.add("ROUTE", "AUTO_REPLY", planned_response_id=planned.id,
                  send_after=timing.send_after.isoformat(), timing=timing.reason, variant=sel.variant_id)
        return final_text, validation, planned, RoutingDecision.AUTO_REPLY, None

    def _open_review(self, conv_id, contact_id, msg, classification: Classification,
                     policy: PolicyOutcome, draft_text, now, recommended: str | None = None,
                     kind: ReviewKind = ReviewKind.REPLY, planned_response_id: str | None = None) -> str:
        e = self.e
        rid = e.ids.new("rev")
        e.reviews.create({
            "id": rid,
            "conversation_id": conv_id,
            "message_id": msg.id,
            "planned_response_id": planned_response_id,
            "created_at": now.isoformat(),
            "status": "OPEN",
            "kind": kind.value,
            "recommended_action": recommended or policy.routing.value,
            "risk_level": policy.risk.value,
            "model_classification_json": classification.model_dump_json(exclude={"raw"}),
            "model_confidence": classification.top_confidence,
            "generated_response": draft_text,
        })
        e.events.append(
            id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
            type=EventType.HUMAN_REVIEW_REQUIRED, created_at=now,
            payload={"review_id": rid, "kind": kind.value, "risk": policy.risk.value,
                     "recommended_action": recommended or policy.routing.value},
            caused_by=msg.id,
        )
        return rid

    def _schedule_followups(self, conv_id, contact_id, state: ConversationState, now, trace) -> list[str]:
        e = self.e
        ids: list[str] = []
        existing = {r["id"] for r in e.followups.for_conversation(conv_id)}
        delay = timedelta(days=e.config.followups.delay_days)
        for fi in state.future_intents:
            key = f"followup_{fi.id}"
            if key in existing:
                continue
            due = (now + delay).replace(hour=15, minute=7, second=0, microsecond=0)
            e.followups.add(
                id=key, conversation_id=conv_id, contact_id=contact_id, due_at=due,
                reason=f"volunteered future intent: {fi.description}",
                payload={"future_intent_id": fi.id, "who": fi.who, "what": fi.what,
                         "when_text": fi.when_text, "source_message_id": fi.source_message_id},
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
    return _URL_RE.findall(text or "")
