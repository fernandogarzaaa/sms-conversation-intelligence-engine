"""FastAPI application factory.

    create_app(settings=Settings.from_env())

Security model (see SECURITY.md):
  - JSON API: API key (X-API-Key or `Authorization: Bearer`) when
    SMS_ENGINE_API_KEYS is set. Unset = local development, a warning is logged.
  - Review dashboard: HTTP Basic when SMS_ENGINE_UI_PASSWORD (or API keys) is set.
  - Provider webhook: authenticated by the provider's signature, not an API key.
  - /health is always open (load balancers).
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from sms_engine import __version__
from sms_engine.api.schemas import (
    CampaignTriggerIn,
    ClientIn,
    EventIn,
    InboundMessageIn,
    JobCompletedIn,
    ReleaseIn,
    ResolveIssueIn,
    ReviewDecisionIn,
    SuppressIn,
    ValidateTextIn,
)
from sms_engine.application.service import NotFound, SmsEngineService
from sms_engine.application.settings import Settings
from sms_engine.application.wiring import Engine, build_engine
from sms_engine.application.worker import BackgroundWorker
from sms_engine.context.packs import all_seed_contexts
from sms_engine.infrastructure.logging_setup import configure_logging
from sms_engine.infrastructure.transport import build_transport, verify_twilio_signature

log = logging.getLogger("sms_engine.api")

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _provider_from_env():
    try:
        from sms_engine.ai.factory import provider_from_env
    except ImportError:  # pragma: no cover - factory ships with the package
        return None
    return provider_from_env()


def create_app(
    db_path: str | None = None,
    *,
    ingest_seed_clients: bool | None = None,
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if db_path is not None:
        settings.db_path = db_path
    if ingest_seed_clients is not None:
        settings.seed_clients = ingest_seed_clients
    configure_logging(settings.log_level, json_lines=settings.log_json)

    if engine is None:
        engine = build_engine(
            db_path=settings.db_path,
            provider=_provider_from_env(),
            config=settings.automation_config(),
            default_timezone=settings.default_timezone,
            transport=build_transport(
                settings.transport,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            ),
        )
    svc = SmsEngineService(engine)

    if settings.seed_clients:
        for ctx in all_seed_contexts(settings.clients_dir):
            if engine.clients.get(ctx.client_id) is None:
                engine.clients.upsert(ctx)

    if not settings.auth_enabled:
        log.warning("API authentication is DISABLED (set SMS_ENGINE_API_KEYS); local development only")

    worker = BackgroundWorker(svc, settings.worker_interval_seconds) if settings.worker_interval_seconds > 0 else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if worker:
            worker.start()
        try:
            yield
        finally:
            if worker:
                worker.stop()

    app = FastAPI(
        title="SMS Conversation Intelligence Engine",
        version=__version__,
        description=(
            "Event-driven conversation intelligence for post-job SMS. The model writes "
            "text; the system keeps the state, makes the decisions, and stops when a "
            "person must take over."
        ),
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.svc = svc
    app.state.settings = settings
    app.state.worker = worker

    # ---- errors ------------------------------------------------------------

    @app.exception_handler(NotFound)
    async def _not_found(_req: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    # ---- auth ----------------------------------------------------------------

    def require_api_key(request: Request) -> None:
        if not settings.api_keys:
            return
        key = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if not key and auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        if not key or not any(hmac.compare_digest(key, k) for k in settings.api_keys):
            raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})

    # ---- open routes -----------------------------------------------------------

    @app.get("/health", tags=["ops"])
    def health():
        return {
            "ok": engine.db.ping(),
            "version": __version__,
            "provider": engine.provider_name,
            "model": engine.provider_model,
            "transport": getattr(engine.transport, "name", "unknown"),
            "schema_version": engine.db.version,
            "worker": {"enabled": worker is not None, "running": bool(worker and worker.running),
                       "passes": worker.passes if worker else 0,
                       "last_error": worker.last_error if worker else None},
            "auth": settings.auth_enabled,
        }

    # ---- provider webhook (signature-authenticated) ------------------------

    @app.post("/webhooks/twilio/sms", tags=["webhooks"])
    async def twilio_inbound(request: Request):
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if settings.twilio_auth_token and settings.twilio_validate_signatures:
            url = (settings.public_base_url + request.url.path) if settings.public_base_url else str(request.url)
            if settings.public_base_url and request.url.query:
                url += "?" + request.url.query
            if not verify_twilio_signature(
                settings.twilio_auth_token, url, params, request.headers.get("x-twilio-signature", "")
            ):
                log.warning("rejected webhook with an invalid Twilio signature")
                raise HTTPException(403, "invalid signature")
        elif settings.auth_enabled:
            # without a Twilio token there is no signature to check; fall back to the API key
            require_api_key(request)
        to, frm, body, sid = params.get("To", ""), params.get("From", ""), params.get("Body", ""), params.get("MessageSid")
        if not frm:
            raise HTTPException(400, "missing From")
        client_id = svc.client_for_number(to)
        if client_id is None:
            log.warning("webhook for a number no client owns", extra={"to": to})
            return Response(_EMPTY_TWIML, status_code=404, media_type="application/xml")
        r = svc.inbound_from_phone(client_id=client_id, phone=frm, text=body, provider_message_id=sid)
        log.info("twilio inbound", extra={"conversation_id": r.conversation_id, "routing": r.routing.value,
                                          "duplicate": r.duplicate})
        # replies go out through the send queue (timed, validated), never inline TwiML
        return Response(_EMPTY_TWIML, media_type="application/xml")

    # ---- JSON API (API-key protected) ----------------------------------------

    api = APIRouter(dependencies=[Depends(require_api_key)])

    @api.post("/messages/inbound", tags=["messages"])
    def inbound(body: InboundMessageIn):
        r = svc.inbound(
            client_id=body.client_id, contact_id=body.contact_id, text=body.text,
            conversation_id=body.conversation_id, phone=body.phone, neighborhood=body.neighborhood,
            provider_message_id=body.provider_message_id, contact_name=body.contact_name,
        )
        return _pipeline_result_json(r)

    @api.post("/jobs/completed", tags=["campaigns"])
    def job_completed(body: JobCompletedIn):
        return svc.job_completed(
            client_id=body.client_id, contact_id=body.contact_id, phone=body.phone,
            customer_name=body.customer_name, neighborhood=body.neighborhood, job_id=body.job_id,
            payload=body.payload,
        )

    @api.post("/events", tags=["events"])
    def ingest_event(body: EventIn):
        return svc.ingest_event(
            conversation_id=body.conversation_id, contact_id=body.contact_id,
            type=body.type, payload=body.payload,
        )

    @api.post("/process-due", tags=["worker"])
    def process_due():
        return {"sent": svc.process_due()}

    @api.post("/process-followups", tags=["worker"])
    def process_followups():
        return {"followups": svc.process_followups()}

    @api.post("/worker/run", tags=["worker"])
    def worker_run():
        return svc.run_worker_pass()

    @api.get("/outbound", tags=["worker"])
    def outbound(limit: int = 50):
        return svc.outbound_queue(limit=max(1, min(limit, 500)))

    @api.get("/conversations", tags=["conversations"])
    def list_conversations(client_id: str | None = None, status: str | None = None,
                           limit: int = 100, offset: int = 0):
        return svc.list_conversations(client_id=client_id, status=status,
                                      limit=max(1, min(limit, 500)), offset=max(0, offset))

    @api.get("/conversations/{conversation_id}", tags=["conversations"])
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
            "planned_responses": v.planned,
            "traces": v.traces,
        }

    @api.get("/conversations/{conversation_id}/state", tags=["conversations"])
    def get_state(conversation_id: str):
        s = svc.conversation_state(conversation_id)
        if s is None:
            raise HTTPException(404, "conversation not found")
        return s

    @api.get("/conversations/{conversation_id}/trace", tags=["conversations"])
    def get_trace(conversation_id: str, message_id: str | None = None):
        t = svc.trace(conversation_id, message_id)
        if t is None:
            raise HTTPException(404, "no trace")
        return t

    @api.post("/conversations/{conversation_id}/review", tags=["review"])
    def review_conversation(conversation_id: str, body: ReviewDecisionIn):
        return svc.decide_review_for_conversation(
            conversation_id, action=body.action, edited_text=body.edited_text,
            reviewer=body.reviewer, notes=body.notes,
        )

    @api.post("/conversations/{conversation_id}/suppress", tags=["conversations"])
    def suppress(conversation_id: str, body: SuppressIn):
        return svc.suppress(conversation_id, reason=body.reason, note=body.note)

    @api.post("/conversations/{conversation_id}/release", tags=["conversations"])
    def release(conversation_id: str, body: ReleaseIn):
        return svc.release(conversation_id, note=body.note)

    @api.post("/conversations/{conversation_id}/issues/{issue_id}/resolve", tags=["conversations"])
    def resolve_issue(conversation_id: str, issue_id: str, body: ResolveIssueIn):
        return svc.resolve_issue(conversation_id, issue_id, note=body.note,
                                 release_suppression=body.release_suppression, resolved_by=body.resolved_by)

    @api.post("/conversations/{conversation_id}/campaigns", tags=["campaigns"])
    def trigger_campaign(conversation_id: str, body: CampaignTriggerIn):
        return svc.trigger_campaign(conversation_id, body.kind, dry_run=body.dry_run)

    @api.get("/review-queue", tags=["review"])
    def review_queue(client_id: str | None = None):
        return svc.review_queue(client_id=client_id)

    @api.get("/reviews/{review_id}", tags=["review"])
    def review_detail(review_id: str):
        d = svc.review_detail(review_id)
        if d is None:
            raise HTTPException(404, "review not found")
        return d

    @api.post("/reviews/{review_id}/validate", tags=["review"])
    def validate_review_text(review_id: str, body: ValidateTextIn):
        return svc.validate_review_text(review_id, body.text)

    @api.post("/reviews/{review_id}/decision", tags=["review"])
    def decide_review(review_id: str, body: ReviewDecisionIn):
        return svc.decide_review(
            review_id, action=body.action, edited_text=body.edited_text,
            reviewer=body.reviewer, notes=body.notes,
        )

    @api.get("/metrics", tags=["ops"])
    def metrics():
        return svc.operational_metrics()

    @api.get("/metrics/prometheus", tags=["ops"], response_class=PlainTextResponse)
    def metrics_prometheus():
        return PlainTextResponse(_prometheus(svc.operational_metrics()), media_type="text/plain; version=0.0.4")

    @api.get("/eval/classifier", tags=["eval"])
    def eval_classifier(refresh: bool = False):
        return svc.classifier_eval(refresh=refresh)

    @api.get("/eval/automation-eligibility", tags=["eval"])
    def eval_eligibility():
        return svc.automation_eligibility()

    @api.get("/config/automation", tags=["ops"])
    def automation_config():
        return svc.automation_config()

    @api.get("/clients", tags=["clients"])
    def clients():
        return svc.list_clients()

    @api.get("/clients/{client_id}", tags=["clients"])
    def client(client_id: str):
        return svc.get_client(client_id)

    @api.post("/clients", tags=["clients"])
    def add_client(body: ClientIn):
        return svc.ingest_client(body.model_dump())

    app.include_router(api)

    # ---- UI (mounted last) ----------------------------------------------------
    from sms_engine.ui.routes import mount_ui

    mount_ui(app, svc, settings)
    return app


def _pipeline_result_json(r) -> dict:
    return {
        "conversation_id": r.conversation_id,
        "contact_id": r.contact_id,
        "message_id": r.message.id,
        "duplicate": r.duplicate,
        "error": r.error,
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
        "invalidated_response_ids": r.invalidated_ids,
        "superseded_review_ids": r.superseded_review_ids,
        "state_after": r.state_after.model_dump(mode="json"),
        "trace": r.trace.model_dump(mode="json") if r.trace else None,
        "decision_trace": r.trace.render() if r.trace else None,
        "latency_ms": r.latency_ms,
    }


def _prometheus(m: dict) -> str:
    lines: list[str] = []

    def metric(name: str, value, help_: str, mtype: str = "gauge", labels: dict | None = None) -> None:
        full = f"sms_engine_{name}"
        if not any(line.startswith(f"# HELP {full} ") for line in lines):
            lines.append(f"# HELP {full} {help_}")
            lines.append(f"# TYPE {full} {mtype}")
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in labels.items()) + "}"
        lines.append(f"{full}{lab} {float(value or 0)}")

    metric("messages_total", m.get("inbound_messages"), "Messages stored", "counter", {"direction": "inbound"})
    metric("messages_total", m.get("outbound_messages"), "Messages stored", "counter", {"direction": "outbound"})
    for route, n in (m.get("routing_breakdown") or {}).items():
        metric("routing_total", n, "Inbound decisions by routing", "counter", {"routing": route})
    for status, n in (m.get("planned_by_status") or {}).items():
        metric("planned_responses", n, "Planned responses by status", "gauge", {"status": status})
    for status, n in (m.get("followups_by_status") or {}).items():
        metric("followups", n, "Follow-ups by status", "gauge", {"status": status})
    metric("reviews_open", m.get("reviews_open"), "Open human reviews")
    metric("reviews_decided_total", m.get("reviews_decided"), "Decided human reviews", "counter")
    metric("reviews_superseded_total", m.get("reviews_superseded"), "Reviews closed by a newer message", "counter")
    metric("responses_invalidated_total", m.get("responses_invalidated"), "Queued responses invalidated", "counter")
    metric("outbound_failed_total", m.get("outbound_failed"), "Outbound sends that gave up", "counter")
    metric("processing_failures_total", m.get("processing_failures"), "Inbound analyses that failed safe", "counter")
    metric("campaigns_allowed_total", m.get("campaigns_allowed"), "Campaign steps allowed", "counter")
    metric("campaigns_blocked_total", m.get("campaigns_blocked"), "Campaign steps blocked", "counter")
    metric("approval_rate", m.get("approval_rate"), "Share of decided reviews approved")
    metric("automation_rate", m.get("automation_rate"), "Share of inbound routed AUTO_REPLY")
    metric("avg_pipeline_latency_ms", m.get("avg_pipeline_latency_ms"), "Mean inbound pipeline latency")
    return "\n".join(lines) + "\n"


app = None  # lazily created by `uvicorn sms_engine.api.app:get_app --factory`


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app


if os.environ.get("SMS_ENGINE_EAGER_APP") == "1":  # pragma: no cover - for `uvicorn sms_engine.api.app:app`
    app = create_app()
