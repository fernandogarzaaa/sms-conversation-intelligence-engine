"""Facade over the engine: the single surface the API, UI, CLI, and worker call.

Keeps FastAPI/Jinja handlers thin and keeps orchestration in one place. Every
mutating method runs as one database transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sms_engine.application.campaigns import CampaignService
from sms_engine.application.followups import FollowupService
from sms_engine.application.pipeline import InboundPipeline, PipelineResult
from sms_engine.application.review_service import ReviewService
from sms_engine.application.send_worker import SendWorker
from sms_engine.application.state_builder import build_state
from sms_engine.application.wiring import Engine
from sms_engine.context.ingestion import ContextIngestor, RawClientSources
from sms_engine.domain.enums import EventType, ReviewAction, SuppressionReason
from sms_engine.domain.messages import Message
from sms_engine.domain.state import ConversationState
from sms_engine.eval.evaluator import evaluate_classifier
from sms_engine.eval.metrics import MetricsService
from sms_engine.eval.thresholds import compute_eligibility

# Event types an external system may post to /events. Everything else is written
# by the engine itself (and has a dedicated endpoint where a human may trigger it).
EXTERNAL_EVENT_TYPES = {
    EventType.JOB_COMPLETED,
    EventType.CAMPAIGN_TRIGGERED,
    EventType.SUPPRESSION_ACTIVATED,
    EventType.ESCALATED,
}


class NotFound(LookupError):
    """Raised for unknown ids; the API maps it to 404."""


@dataclass
class ConversationView:
    conversation: dict
    state: ConversationState
    messages: list[Message]
    events: list[dict]
    reviews: list[dict]
    traces: list[dict]
    followups: list[dict]
    planned: list[dict]


class SmsEngineService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.pipeline = InboundPipeline(engine)
        self.reviews = ReviewService(engine)
        self.campaigns = CampaignService(engine)
        self.followup_service = FollowupService(engine)
        self.send_worker = SendWorker(engine)
        self.metrics = MetricsService(engine.db)
        self._classifier_eval_cache: dict | None = None

    # -- clients ---------------------------------------------------------------

    def ingest_client(self, raw: dict) -> dict:
        ctx = ContextIngestor().ingest(
            RawClientSources(
                client_id=raw["client_id"],
                name=raw["name"],
                website_text=raw.get("website_text", ""),
                faq=raw.get("faq", []),
                config=raw.get("config", {}),
                sample_replies=raw.get("sample_replies", []),
                sources=raw.get("sources", []),
            )
        )
        self.e.clients.upsert(ctx)
        return ctx.model_dump()

    def list_clients(self) -> list[dict]:
        return [c.model_dump() for c in self.e.clients.all()]

    def get_client(self, client_id: str) -> dict:
        c = self.e.clients.get(client_id)
        if c is None:
            raise NotFound("client not found")
        return c.model_dump()

    def client_for_number(self, phone_number: str) -> str | None:
        """Route a provider webhook: which client owns this inbound number?"""
        clients = self.e.clients.all()
        for c in clients:
            if phone_number in c.phone_numbers or phone_number == c.sms_from_number:
                return c.client_id
        return clients[0].client_id if len(clients) == 1 else None

    # -- inbound ------------------------------------------------------------------

    def inbound(
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
        return self.pipeline.handle_inbound(
            client_id=client_id,
            contact_id=contact_id,
            text=text,
            conversation_id=conversation_id,
            phone=phone,
            neighborhood=neighborhood,
            received_at=received_at,
            provider_message_id=provider_message_id,
            contact_name=contact_name,
        )

    def inbound_from_phone(
        self, *, client_id: str, phone: str, text: str, provider_message_id: str | None = None
    ) -> PipelineResult:
        """Provider-webhook entry: identify the contact by phone number."""
        row = self.e.conversations.find_by_phone(phone, client_id)
        contact_id = row["contact_id"] if row else f"tel:{phone}"
        return self.inbound(
            client_id=client_id, contact_id=contact_id, text=text,
            conversation_id=row["id"] if row else None, phone=phone,
            provider_message_id=provider_message_id,
        )

    def job_completed(
        self,
        *,
        client_id: str,
        contact_id: str,
        phone: str | None = None,
        customer_name: str | None = None,
        neighborhood: str | None = None,
        job_id: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        """Campaign bridge: the job system reports a completed job (before any SMS)."""
        e = self.e
        if e.clients.get(client_id) is None:
            raise ValueError(f"unknown client_id {client_id!r}")
        now = e.clock.now()
        with e.db.transaction():
            row = e.conversations.find_by_contact(contact_id, client_id)
            if row is None:
                conv_id = e.ids.new("conv")
                e.conversations.create(conv_id, contact_id, client_id, now, phone, neighborhood)
            else:
                conv_id = row["id"]
                e.conversations.update_contact_details(conv_id, phone=phone, neighborhood=neighborhood)
            body = dict(payload or {})
            if job_id:
                body["job_id"] = job_id
            if customer_name:
                body["customer_name"] = customer_name
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conv_id, contact_id=contact_id,
                type=EventType.JOB_COMPLETED, created_at=now, payload=body,
            )
        return {"conversation_id": conv_id, "state": self.conversation_state(conv_id)}

    def ingest_event(self, *, conversation_id: str, contact_id: str, type: str, payload: dict) -> dict:
        try:
            etype = EventType(type)
        except ValueError:
            raise ValueError(f"unknown event type {type!r}") from None
        if etype not in EXTERNAL_EVENT_TYPES:
            raise ValueError(
                f"{etype.value} is written by the engine itself and cannot be posted; "
                f"allowed: {sorted(t.value for t in EXTERNAL_EVENT_TYPES)}"
            )
        row = self.e.conversations.get(conversation_id)
        if row is None:
            raise NotFound("unknown conversation")
        if row["contact_id"] != contact_id:
            raise ValueError("contact_id does not match the conversation")
        payload = payload or {}

        if etype == EventType.CAMPAIGN_TRIGGERED and payload.get("kind"):
            d = self.campaigns.trigger(conversation_id, str(payload["kind"]), source=payload.get("source", "events"))
            return {"type": etype.value, "campaign": d.as_dict()}

        if etype == EventType.SUPPRESSION_ACTIVATED:
            reason = SuppressionReason(payload.get("reason", "MANUAL")).value
            payload = {**payload, "reason": reason, "since": payload.get("since") or self.e.clock.now().isoformat()}

        with self.e.db.transaction():
            ev = self.e.events.append(
                id=self.e.ids.new("evt"),
                conversation_id=conversation_id,
                contact_id=contact_id,
                type=etype,
                created_at=self.e.clock.now(),
                payload=payload,
            )
            if etype in (EventType.SUPPRESSION_ACTIVATED, EventType.ESCALATED):
                self._invalidate_queue(conversation_id, contact_id)
        return ev.model_dump(mode="json")

    # -- worker -----------------------------------------------------------------

    def process_due(self, now: datetime | None = None) -> list[dict]:
        return [m.model_dump(mode="json") for m in self.send_worker.process_due(now)]

    def process_followups(self, now: datetime | None = None) -> list[dict]:
        return [o.as_dict() for o in self.followup_service.process_due(now)]

    def run_worker_pass(self, now: datetime | None = None) -> dict:
        """One background pass: due follow-ups become reviews, due sends go out."""
        followups = self.process_followups(now)
        sent = self.process_due(now)
        return {"sent": sent, "followups": followups}

    # -- campaigns --------------------------------------------------------------

    def trigger_campaign(self, conversation_id: str, kind: str, *, dry_run: bool = False,
                         source: str = "api") -> dict:
        if self.e.conversations.get(conversation_id) is None:
            raise NotFound("unknown conversation")
        return self.campaigns.trigger(conversation_id, kind, dry_run=dry_run, source=source).as_dict()

    # -- conversations ----------------------------------------------------------

    def list_conversations(
        self, *, client_id: str | None = None, limit: int | None = None, offset: int = 0,
        status: str | None = None,
    ) -> list[dict]:
        e = self.e
        open_counts = e.reviews.open_counts_by_conversation()
        out = []
        for cid in e.conversations.all_ids(client_id=client_id, limit=None if status else limit,
                                           offset=0 if status else offset):
            row = dict(e.conversations.get(cid))
            st = self._state(cid)
            traces = e.traces.for_conversation(cid)
            row.update(
                {
                    "name": st.identity.name,
                    "suppressed": st.suppression.active,
                    "suppression_reason": st.suppression.reason.value if st.suppression.reason else None,
                    "opted_out": st.opted_out,
                    "open_issues": sum(1 for i in st.open_issues if not i.resolved),
                    "open_reviews": open_counts.get(cid, 0),
                    "referral_status": st.referral_status.value,
                    "campaign_status": st.campaign_status.value,
                    "last_routing": traces[-1].routing if traces else None,
                    "messages": len(st.received_messages) + len(st.sent_messages),
                    "last_interaction": st.last_interaction.isoformat() if st.last_interaction else None,
                }
            )
            out.append(row)
        if status:
            status = status.lower()
            pred = {
                "suppressed": lambda r: r["suppressed"],
                "opted_out": lambda r: r["opted_out"],
                "open_issues": lambda r: r["open_issues"] > 0,
                "needs_review": lambda r: r["open_reviews"] > 0,
                "active": lambda r: not r["suppressed"],
            }.get(status)
            if pred is None:
                raise ValueError("status must be one of suppressed|opted_out|open_issues|needs_review|active")
            out = [r for r in out if pred(r)]
            out = out[offset: offset + limit] if limit is not None else out[offset:]
        return out

    def conversation_view(self, conversation_id: str) -> ConversationView | None:
        e = self.e
        row = e.conversations.get(conversation_id)
        if row is None:
            return None
        st = self._state(conversation_id)
        planned = [p.model_dump(mode="json") for p in e.planned.for_conversation(conversation_id)]
        return ConversationView(
            conversation=dict(row),
            state=st,
            messages=e.messages.list_for_conversation(conversation_id),
            events=[ev.model_dump(mode="json") for ev in e.events.list_for_conversation(conversation_id)],
            reviews=[dict(r) for r in e.reviews.for_conversation(conversation_id)],
            traces=[t.model_dump(mode="json") for t in e.traces.for_conversation(conversation_id)],
            followups=[dict(f) for f in e.followups.for_conversation(conversation_id)],
            planned=planned,
        )

    def conversation_state(self, conversation_id: str) -> dict | None:
        row = self.e.conversations.get(conversation_id)
        if row is None:
            return None
        return self._state(conversation_id).model_dump(mode="json")

    def trace(self, conversation_id: str, message_id: str | None = None) -> dict | None:
        traces = self.e.traces.for_conversation(conversation_id)
        if not traces:
            return None
        t = traces[-1] if message_id is None else next(
            (x for x in traces if x.message_id == message_id), None
        )
        if t is None:
            return None
        d = t.model_dump(mode="json")
        d["rendered"] = t.render()
        return d

    # -- suppression / issues ---------------------------------------------------

    def suppress(self, conversation_id: str, *, reason: str = "MANUAL", note: str | None = None) -> dict:
        e = self.e
        row = e.conversations.get(conversation_id)
        if row is None:
            raise NotFound("unknown conversation")
        reason_v = SuppressionReason(reason).value
        now = e.clock.now()
        with e.db.transaction():
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conversation_id, contact_id=row["contact_id"],
                type=EventType.SUPPRESSION_ACTIVATED, created_at=now,
                payload={"reason": reason_v, "since": now.isoformat(), "note": note or "manual"},
            )
            self._invalidate_queue(conversation_id, row["contact_id"])
        return self.conversation_state(conversation_id)

    def release(self, conversation_id: str, *, note: str | None = None) -> dict:
        e = self.e
        row = e.conversations.get(conversation_id)
        if row is None:
            raise NotFound("unknown conversation")
        st = self._state(conversation_id)
        if st.opted_out:
            raise ValueError("cannot release a contact who opted out")
        if not st.suppression.active:
            raise ValueError("conversation is not suppressed")
        now = e.clock.now()
        with e.db.transaction():
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conversation_id, contact_id=row["contact_id"],
                type=EventType.SUPPRESSION_RELEASED, created_at=now,
                payload={"released_at": now.isoformat(), "note": note or "manual release"},
            )
        return self.conversation_state(conversation_id)

    def resolve_issue(
        self, conversation_id: str, issue_id: str, *, note: str | None = None,
        release_suppression: bool = False, resolved_by: str | None = None,
    ) -> dict:
        """Staff mark an open issue resolved. Optionally release an OPEN_ISSUE
        suppression once no unresolved issue remains (never an opt-out)."""
        e = self.e
        row = e.conversations.get(conversation_id)
        if row is None:
            raise NotFound("unknown conversation")
        st = self._state(conversation_id)
        issue = next((i for i in st.open_issues if i.id == issue_id), None)
        if issue is None:
            raise NotFound("unknown issue")
        if issue.resolved:
            raise ValueError("issue is already resolved")
        now = e.clock.now()
        with e.db.transaction():
            e.events.append(
                id=e.ids.new("evt"), conversation_id=conversation_id, contact_id=row["contact_id"],
                type=EventType.ISSUE_RESOLVED, created_at=now,
                payload={"issue_id": issue_id, "note": note, "resolved_at": now.isoformat(),
                         "resolved_by": resolved_by},
            )
            remaining = [i for i in st.open_issues if not i.resolved and i.id != issue_id]
            if (release_suppression and not remaining and st.suppression.active
                    and st.suppression.reason == SuppressionReason.OPEN_ISSUE):
                e.events.append(
                    id=e.ids.new("evt"), conversation_id=conversation_id, contact_id=row["contact_id"],
                    type=EventType.SUPPRESSION_RELEASED, created_at=now,
                    payload={"released_at": now.isoformat(), "note": "all issues resolved"},
                )
        return self.conversation_state(conversation_id)

    # -- review --------------------------------------------------------------

    def review_queue(self, *, client_id: str | None = None) -> list[dict]:
        out = []
        for r in self.e.reviews.open_queue(client_id=client_id):
            r = dict(r)
            try:
                r["model_classification"] = json.loads(r.pop("model_classification_json", "{}") or "{}")
            except Exception:
                r["model_classification"] = {}
            conv = self.e.conversations.get(r["conversation_id"])
            msg = self.e.messages.get(r["message_id"])
            r["contact_id"] = conv["contact_id"] if conv else None
            r["client_id"] = conv["client_id"] if conv else None
            r["customer_message"] = msg.raw_text if msg else None
            out.append(r)
        return out

    def review_detail(self, review_id: str) -> dict | None:
        r = self.e.reviews.get(review_id)
        if not r:
            return None
        r = dict(r)
        conv = self.e.conversations.get(r["conversation_id"])
        view = self.conversation_view(r["conversation_id"])
        trace = self.trace(r["conversation_id"], r["message_id"])
        try:
            r["model_classification"] = json.loads(r.get("model_classification_json") or "{}")
        except Exception:
            r["model_classification"] = {}
        msg = self.e.messages.get(r["message_id"])
        client = self.e.clients.get(conv["client_id"]) if conv else None
        plan = self.e.planned.get(r["planned_response_id"]) if r.get("planned_response_id") else None
        return {
            "review": r,
            "contact_id": conv["contact_id"] if conv else None,
            "client_id": conv["client_id"] if conv else None,
            "customer_message": msg.raw_text if msg else None,
            "conversation": {
                "messages": [m.model_dump(mode="json") for m in view.messages] if view else [],
                "state": view.state.model_dump(mode="json") if view else {},
                "followups": view.followups if view else [],
            },
            "planned_response": plan.model_dump(mode="json") if plan else None,
            "trace": trace,
            "client_context": client.model_dump() if client else {},
        }

    def validate_review_text(self, review_id: str, text: str) -> dict:
        if self.e.reviews.get(review_id) is None:
            raise NotFound("review not found")
        v = self.reviews.validate_text(review_id, text)
        return {"passed": v.passed, "findings": [f.model_dump() for f in v.findings], "chars": len(text.strip())}

    def decide_review(
        self, review_id: str, *, action: str, edited_text: str | None = None,
        reviewer: str | None = None, notes: str | None = None,
    ) -> dict:
        if self.e.reviews.get(review_id) is None:
            raise NotFound("review not found")
        res = self.reviews.decide(
            review_id, action=ReviewAction(action), edited_text=edited_text,
            reviewer=reviewer, notes=notes,
        )
        return {
            "review_id": res.review_id,
            "action": res.action.value,
            "edited": res.edited,
            "blocked": res.blocked,
            "final_response": res.final_response,
            "planned_response_id": res.planned_response.id if res.planned_response else None,
            "send_after": (res.planned_response.send_after.isoformat()
                           if res.planned_response and res.planned_response.send_after else None),
            "validation_passed": res.validation.passed if res.validation else None,
            "validation_findings": [f.model_dump() for f in res.validation.findings] if res.validation else [],
        }

    def decide_review_for_conversation(self, conversation_id: str, **kw) -> dict:
        open_reviews = self.e.reviews.open_for_conversation(conversation_id)
        if not open_reviews:
            raise ValueError("no open review for this conversation")
        # the newest open review is the one for the latest customer message
        return self.decide_review(open_reviews[-1]["id"], **kw)

    # -- outbound ------------------------------------------------------------

    def outbound_queue(self, limit: int = 50) -> list[dict]:
        return [p.model_dump(mode="json") for p in self.e.planned.list_recent(limit)]

    # -- metrics / eval -------------------------------------------------------

    def operational_metrics(self) -> dict:
        d = self.metrics.compute().as_dict()
        d["provider"] = self.e.provider_name
        d["transport"] = getattr(self.e.transport, "name", "unknown")
        return d

    def classifier_eval(self, *, refresh: bool = False) -> dict:
        # the eval runs the whole synthetic set through the provider; with a real
        # LLM that is ~125 calls, so cache it per process
        if self._classifier_eval_cache is None or refresh:
            self._classifier_eval_cache = evaluate_classifier(self.e.classifier).summary()
        return self._classifier_eval_cache

    def automation_eligibility(self) -> list[dict]:
        return [x.__dict__ for x in compute_eligibility(self.e.db, self.e.config)]

    def automation_config(self) -> dict:
        return self.e.config.model_dump()

    # -- internals -----------------------------------------------------------------

    def _invalidate_queue(self, conversation_id: str, contact_id: str) -> None:
        v = self.e.events.input_version(conversation_id)
        self.e.queue.invalidate_stale(conversation_id, contact_id, v + 1, self.e.clock.now())

    def _state(self, conversation_id: str) -> ConversationState:
        row = self.e.conversations.get(conversation_id)
        return build_state(
            contact_id=row["contact_id"],
            conversation_id=conversation_id,
            client_id=row["client_id"],
            events=self.e.events.list_for_conversation(conversation_id),
            messages=self.e.messages.list_for_conversation(conversation_id),
        )
