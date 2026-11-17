"""UI routes. Server-rendered; a little vanilla JS in the templates for actions."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sms_engine.application.service import SmsEngineService

_TPL = Path(__file__).parent / "templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TPL)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda x: f"{100 * float(x):.0f}%" if x is not None else "—"
    env.filters["round2"] = lambda x: f"{float(x):.2f}" if x is not None else "—"
    return env


def mount_ui(app: FastAPI, svc: SmsEngineService) -> None:
    env = _env()

    def render(name: str, **ctx) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**ctx))

    @app.get("/", response_class=HTMLResponse)
    def dashboard(_req: Request):
        return render(
            "dashboard.html",
            metrics=svc.operational_metrics(),
            classifier=svc.classifier_eval(),
            eligibility=svc.automation_eligibility(),
            conversations=svc.list_conversations(),
            queue=svc.review_queue(),
        )

    @app.get("/ui/inbox", response_class=HTMLResponse)
    def inbox(_req: Request):
        return render("inbox.html", queue=svc.review_queue(), conversations=svc.list_conversations())

    @app.get("/ui/conversations/{conversation_id}", response_class=HTMLResponse)
    def conversation(conversation_id: str):
        v = svc.conversation_view(conversation_id)
        if v is None:
            return HTMLResponse("not found", status_code=404)
        return render(
            "conversation.html",
            v=v,
            state=v.state.model_dump(mode="json"),
            trace=svc.trace(conversation_id),
        )

    @app.get("/ui/reviews/{review_id}", response_class=HTMLResponse)
    def review(review_id: str):
        d = svc.review_detail(review_id)
        if d is None:
            return HTMLResponse("not found", status_code=404)
        return render("review.html", d=d)

    @app.post("/ui/reviews/{review_id}/decide")
    def decide(
        review_id: str,
        action: str = Form(...),
        edited_text: str = Form(""),
        reviewer: str = Form("reviewer"),
        notes: str = Form(""),
    ):
        svc.decide_review(
            review_id, action=action, edited_text=edited_text or None,
            reviewer=reviewer or None, notes=notes or None,
        )
        return RedirectResponse("/ui/inbox", status_code=303)

    @app.post("/ui/simulate")
    def simulate(
        client_id: str = Form("greenline"),
        contact_id: str = Form(...),
        text: str = Form(...),
        neighborhood: str = Form(""),
    ):
        r = svc.inbound(client_id=client_id, contact_id=contact_id, text=text, neighborhood=neighborhood or None)
        return RedirectResponse(f"/ui/conversations/{r.conversation_id}", status_code=303)

    @app.post("/ui/process-due")
    def ui_process_due():
        svc.process_due()
        return RedirectResponse("/", status_code=303)

    @app.post("/ui/conversations/{conversation_id}/suppress")
    def ui_suppress(conversation_id: str, note: str = Form("")):
        svc.suppress(conversation_id, note=note or None)
        return RedirectResponse(f"/ui/conversations/{conversation_id}", status_code=303)

    @app.post("/ui/conversations/{conversation_id}/release")
    def ui_release(conversation_id: str, note: str = Form("")):
        try:
            svc.release(conversation_id, note=note or None)
        except ValueError:
            pass
        return RedirectResponse(f"/ui/conversations/{conversation_id}", status_code=303)
