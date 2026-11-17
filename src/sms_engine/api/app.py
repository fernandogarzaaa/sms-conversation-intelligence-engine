"""FastAPI application factory."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from sms_engine.api.schemas import (
    ClientIn,
    EventIn,
    InboundMessageIn,
    ReleaseIn,
    ReviewDecisionIn,
    SuppressIn,
)
from sms_engine.application.service import SmsEngineService
from sms_engine.application.wiring import build_engine
from sms_engine.context.packs import all_seed_contexts


def create_app(db_path: str | None = None, *, ingest_seed_clients: bool = True) -> FastAPI:
    db_path = db_path or os.environ.get("SMS_ENGINE_DB", "sms_engine.db")
    engine = build_engine(db_path=db_path)
    svc = SmsEngineService(engine)

    if ingest_seed_clients:
        for ctx in all_seed_contexts():
            if engine.clients.get(ctx.client_id) is None:
                engine.clients.upsert(ctx)

    app = FastAPI(title="SMS Conversation Intelligence Engine", version="0.1.0")
    app.state.engine = engine
    app.state.svc = svc

    # ---- API ---------------------------------------------------------------

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/health")
    def health():
        return {"ok": True, "provider": engine.classifier.provider.name}

    @app.post("/messages/inbound")
    def inbound(body: InboundMessageIn):
        r = svc.inbound(
            client_id=body.client_id, contact_id=body.contact_id, text=body.text,
            conversation_id=body.conversation_id, phone=body.phone, neighborhood=body.neighborhood,
        )
        return _pipeline_result_json(r)

    @app.post("/events")
    def ingest_event(body: EventIn):
        return svc.ingest_event(
            conversation_id=body.conversation_id, contact_id=body.contact_id,
            type=body.type, payload=body.payload,
        )

    @app.post("/process-due")
    def process_due():
        return {"sent": svc.process_due()}

    @app.get("/conversations")
    def list_conversations():
        return svc.list_conversations()

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str):
        v = svc.conversation_view(conversation_id)
        if v is None:
            raise HTTPException(404, "conversation not found")
        return {
            "conversation": v.conversation,
            "state": v.state.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in v.messages],
            "events": v.events,
            "reviews": v.reviews,
            "followups": v.followups,
            "traces": v.traces,
        }

    @app.get("/conversations/{conversation_id}/state")
    def get_state(conversation_id: str):
        s = svc.conversation_state(conversation_id)
        if s is None:
            raise HTTPException(404, "conversation not found")
        return s

    @app.get("/conversations/{conversation_id}/trace")
    def get_trace(conversation_id: str, message_id: str | None = None):
        t = svc.trace(conversation_id, message_id)
        if t is None:
            raise HTTPException(404, "no trace")
        return t

    @app.post("/conversations/{conversation_id}/review")
    def review_conversation(conversation_id: str, body: ReviewDecisionIn):
        return svc.decide_review_for_conversation(
            conversation_id, action=body.action, edited_text=body.edited_text,
            reviewer=body.reviewer, notes=body.notes,
        )

    @app.post("/conversations/{conversation_id}/suppress")
    def suppress(conversation_id: str, body: SuppressIn):
        return svc.suppress(conversation_id, reason=body.reason, note=body.note)

    @app.post("/conversations/{conversation_id}/release")
    def release(conversation_id: str, body: ReleaseIn):
        return svc.release(conversation_id, note=body.note)

    @app.get("/review-queue")
    def review_queue():
        return svc.review_queue()

    @app.get("/reviews/{review_id}")
    def review_detail(review_id: str):
        d = svc.review_detail(review_id)
        if d is None:
            raise HTTPException(404, "review not found")
        return d

    @app.post("/reviews/{review_id}/decision")
    def decide_review(review_id: str, body: ReviewDecisionIn):
        return svc.decide_review(
            review_id, action=body.action, edited_text=body.edited_text,
            reviewer=body.reviewer, notes=body.notes,
        )

    @app.get("/metrics")
    def metrics():
        return svc.operational_metrics()

    @app.get("/eval/classifier")
    def eval_classifier():
        return svc.classifier_eval()

    @app.get("/eval/automation-eligibility")
    def eval_eligibility():
        return svc.automation_eligibility()

    @app.get("/clients")
    def clients():
        return svc.list_clients()

    @app.post("/clients")
    def add_client(body: ClientIn):
        return svc.ingest_client(body.model_dump())

    # ---- UI (mounted last) ----------------------------------------------------
    from sms_engine.ui.routes import mount_ui

    mount_ui(app, svc)
    return app


def _pipeline_result_json(r) -> dict:
    return {
        "conversation_id": r.conversation_id,
        "contact_id": r.contact_id,
        "message_id": r.message.id,
        "routing": r.routing.value,
        "risk": r.policy.risk.value,
        "intents": [i.model_dump() for i in r.classification.intents],
        "entities": r.entities.model_dump(exclude={"raw"}),
        "policy": r.policy.model_dump(),
        "retrieved_context": r.retrieved.model_dump(),
        "draft_text": r.draft_text,
        "validation": r.validation.model_dump() if r.validation else None,
        "planned_response": r.planned_response.model_dump(mode="json") if r.planned_response else None,
        "review_id": r.review_id,
        "followup_ids": r.followup_ids,
        "state_after": r.state_after.model_dump(mode="json"),
        "trace": r.trace.model_dump(mode="json") if r.trace else None,
        "decision_trace": r.trace.render() if r.trace else None,
        "latency_ms": r.latency_ms,
    }


app = None  # lazily created by `uvicorn sms_engine.api.app:get_app`


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
