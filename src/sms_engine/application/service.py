"""Facade over the engine: the single surface the API and UI call.

Keeps FastAPI/Jinja handlers thin and keeps orchestration in one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sms_engine.application.pipeline import InboundPipeline, PipelineResult
from sms_engine.application.review_service import ReviewService
from sms_engine.application.state_builder import build_state
from sms_engine.application.wiring import Engine
from sms_engine.context.ingestion import ContextIngestor, RawClientSources
from sms_engine.domain.enums import EventType, ReviewAction, SuppressionReason
from sms_engine.domain.messages import Message
from sms_engine.domain.state import ConversationState
from sms_engine.eval.evaluator import evaluate_classifier
from sms_engine.eval.metrics import MetricsService
from sms_engine.eval.thresholds import compute_eligibility


@dataclass
class ConversationView:
    conversation: dict
    state: ConversationState
    messages: list[Message]
    events: list[dict]
    reviews: list[dict]
    traces: list[dict]
    followups: list[dict]


class SmsEngineService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.pipeline = InboundPipeline(engine)
        self.reviews = ReviewService(engine)
        self.metrics = MetricsService(engine.db)

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
    ) -> PipelineResult:
        return self.pipeline.handle_inbound(
            client_id=client_id,
            contact_id=contact_id,
            text=text,
            conversation_id=conversation_id,
            phone=phone,
            neighborhood=neighborhood,
            received_at=received_at,
        )

    def ingest_event(self, *, conversation_id: str, contact_id: str, type: str, payload: dict) -> dict:
        etype = EventType(type)
        ev = self.e.events.append(
            id=self.e.ids.new("evt"),
            conversation_id=conversation_id,
            contact_id=contact_id,
            type=etype,
            created_at=self.e.clock.now(),
            payload=payload or {},
        )
        return ev.model_dump(mode="json")

    def process_due(self, now: datetime | None = None) -> list[dict]:
        return [m.model_dump(mode="json") for m in self.pipeline.process_due(now)]

    # -- conversations ----------------------------------------------------------

    def list_conversations(self) -> list[dict]:
        out = []
        for cid in self.e.conversations.all_ids():
            row = dict(self.e.conversations.get(cid))
            st = self._state(cid)
            open_reviews = [r for r in self.e.reviews.all() if r["conversation_id"] == cid and r["status"] == "OPEN"]
            last_trace = self.e.traces.for_conversation(cid)
            row.update(
                {
                    "name": st.identity.name,
                    "suppressed": st.suppression.active,
                    "suppression_reason": st.suppression.reason.value if st.suppression.reason else None,
                    "open_issues": sum(1 for i in st.open_issues if not i.resolved),
                    "open_reviews": len(open_reviews),
                    "last_routing": last_trace[-1].routing if last_trace else None,
                    "messages": len(st.received_messages) + len(st.sent_messages),
                    "last_interaction": st.last_interaction.isoformat() if st.last_interaction else None,
                }
            )
            out.append(row)
        return out

    def conversation_view(self, conversation_id: str) -> ConversationView | None:
        row = self.e.conversations.get(conversation_id)
        if row is None:
            return None
        st = self._state(conversation_id)
        return ConversationView(
            conversation=dict(row),
            state=st,
            messages=self.e.messages.list_for_conversation(conversation_id),
            events=[e.model_dump(mode="json") for e in self.e.events.list_for_conversation(conversation_id)],
            reviews=[dict(r) for r in self.e.reviews.all() if r["conversation_id"] == conversation_id],
            traces=[t.model_dump(mode="json") for t in self.e.traces.for_conversation(conversation_id)],
            followups=[dict(f) for f in self.e.followups.for_conversation(conversation_id)],
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

    # -- suppression ----------------------------------------------------------

    def suppress(self, conversation_id: str, *, reason: str = "MANUAL", note: str | None = None) -> dict:
        row = self.e.conversations.get(conversation_id)
        if row is None:
            raise ValueError("unknown conversation")
        now = self.e.clock.now()
        self.e.events.append(
            id=self.e.ids.new("evt"), conversation_id=conversation_id, contact_id=row["contact_id"],
            type=EventType.SUPPRESSION_ACTIVATED, created_at=now,
            payload={"reason": SuppressionReason(reason).value, "since": now.isoformat(), "note": note or "manual"},
        )
        # invalidate anything queued (bump past every plan's input version)
        v = self.e.events.input_version(conversation_id)
        self.e.queue.invalidate_stale(conversation_id, row["contact_id"], v + 1, now)
        return self.conversation_state(conversation_id)

    def release(self, conversation_id: str, *, note: str | None = None) -> dict:
        row = self.e.conversations.get(conversation_id)
        if row is None:
            raise ValueError("unknown conversation")
        st = self._state(conversation_id)
        if st.opted_out:
            raise ValueError("cannot release a contact who opted out")
        now = self.e.clock.now()
        self.e.events.append(
            id=self.e.ids.new("evt"), conversation_id=conversation_id, contact_id=row["contact_id"],
            type=EventType.SUPPRESSION_RELEASED, created_at=now,
            payload={"released_at": now.isoformat(), "note": note or "manual release"},
        )
        return self.conversation_state(conversation_id)

    # -- review --------------------------------------------------------------

    def review_queue(self) -> list[dict]:
        out = []
        for r in self.reviews.open_queue():
            r = dict(r)
            try:
                r["model_classification"] = json.loads(r.pop("model_classification_json", "{}") or "{}")
            except Exception:
                r["model_classification"] = {}
            conv = self.e.conversations.get(r["conversation_id"])
            msg = self.e.messages.get(r["message_id"])
            r["contact_id"] = conv["contact_id"] if conv else None
            r["customer_message"] = msg.raw_text if msg else None
            out.append(r)
        return out

    def review_detail(self, review_id: str) -> dict | None:
        r = self.reviews.get(review_id)
        if not r:
            return None
        conv = self.e.conversations.get(r["conversation_id"])
        view = self.conversation_view(r["conversation_id"])
        trace = self.trace(r["conversation_id"], r["message_id"])
        try:
            r["model_classification"] = json.loads(r.get("model_classification_json") or "{}")
        except Exception:
            r["model_classification"] = {}
        return {
            "review": r,
            "contact_id": conv["contact_id"] if conv else None,
            "client_id": conv["client_id"] if conv else None,
            "customer_message": (self.e.messages.get(r["message_id"]).raw_text if self.e.messages.get(r["message_id"]) else None),
            "conversation": {
                "messages": [m.model_dump(mode="json") for m in view.messages] if view else [],
                "state": view.state.model_dump(mode="json") if view else {},
                "followups": view.followups if view else [],
            },
            "trace": trace,
            "client_context": (self.e.clients.get(conv["client_id"]).model_dump() if conv and self.e.clients.get(conv["client_id"]) else {}),
        }

    def decide_review(
        self, review_id: str, *, action: str, edited_text: str | None = None,
        reviewer: str | None = None, notes: str | None = None,
    ) -> dict:
        res = self.reviews.decide(
            review_id, action=ReviewAction(action), edited_text=edited_text,
            reviewer=reviewer, notes=notes,
        )
        return {
            "review_id": res.review_id,
            "action": res.action.value,
            "edited": res.edited,
            "final_response": res.final_response,
            "planned_response_id": res.planned_response.id if res.planned_response else None,
            "validation_passed": res.validation.passed if res.validation else None,
            "validation_findings": [f.model_dump() for f in res.validation.findings] if res.validation else [],
        }

    def decide_review_for_conversation(self, conversation_id: str, **kw) -> dict:
        open_reviews = [r for r in self.e.reviews.open_queue() if r["conversation_id"] == conversation_id]
        if not open_reviews:
            raise ValueError("no open review for this conversation")
        return self.decide_review(open_reviews[0]["id"], **kw)

    # -- metrics / eval -------------------------------------------------------

    def operational_metrics(self) -> dict:
        return self.metrics.compute().as_dict()

    def classifier_eval(self) -> dict:
        return evaluate_classifier(self.e.classifier).summary()

    def automation_eligibility(self) -> list[dict]:
        return [e.__dict__ for e in compute_eligibility(self.e.db, self.e.config)]

    # -- internals -----------------------------------------------------------------

    def _state(self, conversation_id: str) -> ConversationState:
        row = self.e.conversations.get(conversation_id)
        return build_state(
            contact_id=row["contact_id"],
            conversation_id=conversation_id,
            client_id=row["client_id"],
            events=self.e.events.list_for_conversation(conversation_id),
            messages=self.e.messages.list_for_conversation(conversation_id),
        )
