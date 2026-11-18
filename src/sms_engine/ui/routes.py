"""UI routes. Server-rendered; a little vanilla JS in the templates for actions.

Protection: HTTP Basic auth whenever a UI password or API keys are configured,
an Origin/Referer same-host check on every POST (Basic credentials are sent
automatically by browsers, so cross-site form posts must be refused), and
conservative security headers on every page.
"""

from __future__ import annotations

import base64
import hmac
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sms_engine import __version__
from sms_engine.application.service import NotFound, SmsEngineService
from sms_engine.application.settings import Settings

_TPL = Path(__file__).parent / "templates"

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    ),
}


# Plain-language reasons for the reviewer, keyed by policy rule id.
REASONS = {
    "R_STOP_OPTOUT": "The customer asked to stop receiving messages.",
    "R_ALREADY_OPTED_OUT": "This customer opted out earlier. Nothing can be sent.",
    "R_NEGATIVE": "The customer is unhappy. A person needs to handle this.",
    "R_WRONG_NUMBER": "This may be the wrong number. Confirm before anything else is sent.",
    "R_OPEN_ISSUE": "The customer reported a problem that is not fixed yet. No campaign asks.",
    "R_STATE_OPEN_ISSUE": "An earlier problem is still open for this customer.",
    "R_REPEATED_ISSUES": "More than one problem is open for this customer. Escalate.",
    "R_IDENTITY_CORRECTION": "The customer corrected their name. Confirm it before replying.",
    "R_DEFERRAL": "The customer asked to be contacted later.",
    "R_QUESTION_REVIEW": "The customer asked a question.",
    "R_QUESTION_AUTO_IF_GROUNDED": "The customer asked a question that no approved FAQ answers.",
    "R_POSITIVE_WITH_BLOCKER": "The message is positive but also contains something that needs a look.",
    "R_PURE_POSITIVE_REVIEW": "A positive message. Automatic replies are off or the confidence is low.",
    "R_UNRELATED": "The message is not about the job.",
    "R_LOW_CONFIDENCE": "The system is not sure what the customer means.",
    "R_NO_INTENT": "The system could not read an intent from the message.",
    "R_PROMPT_INJECTION": "The message contains text that tries to give the system instructions.",
    "R_PROCESSING_FAILED": "Automatic analysis failed. Read the message yourself.",
    "R_DEFAULT_UNKNOWN": "No rule matched this message.",
}


def review_reasons(policy: dict | None) -> list[str]:
    hits = (policy or {}).get("rule_hits") or []
    out = []
    for h in hits:
        if h.get("routing") in (None, "AUTO_REPLY") and h.get("rule_id") != "R_QUESTION_AUTO_IF_GROUNDED":
            continue
        text = REASONS.get(h.get("rule_id"), h.get("description", ""))
        if text and text not in out:
            out.append(text)
    return out


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TPL)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda x: f"{100 * float(x):.0f}%" if x is not None else "—"
    env.filters["round2"] = lambda x: f"{float(x):.2f}" if x is not None else "—"
    env.filters["ts"] = lambda x: (str(x)[:16].replace("T", " ") if x else "—")
    env.globals["review_reasons"] = review_reasons
    return env


def mount_ui(app: FastAPI, svc: SmsEngineService, settings: Settings | None = None) -> None:
    env = _env()
    settings = settings or Settings()
    passwords = [p for p in [settings.ui_password, *settings.api_keys] if p]

    def ui_auth(request: Request) -> str:
        if not passwords:
            return "reviewer"
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                user, pw = "", ""
            if pw and any(hmac.compare_digest(pw, p) for p in passwords):
                return user or settings.ui_user
        raise HTTPException(401, "authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="SMS Engine review"'})

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return  # non-browser clients (curl, tests) send neither
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc != host:
            raise HTTPException(403, "cross-site request refused")

    def render(name: str, request: Request | None = None, status: int = 200, **ctx) -> HTMLResponse:
        flash = request.query_params.get("flash") if request else None
        flash_kind = request.query_params.get("kind", "info") if request else "info"
        html = env.get_template(name).render(
            flash=flash, flash_kind=flash_kind, version=__version__,
            provider=svc.e.provider_name, **ctx,
        )
        return HTMLResponse(html, status_code=status, headers=_SECURITY_HEADERS)

    def redirect(url: str, flash: str | None = None, kind: str = "info") -> RedirectResponse:
        if flash:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}flash={quote(flash[:300])}&kind={kind}"
        return RedirectResponse(url, status_code=303)

    read = [Depends(ui_auth)]
    write = [Depends(ui_auth), Depends(same_origin)]

    @app.get("/", response_class=HTMLResponse, dependencies=read, include_in_schema=False)
    def dashboard(request: Request):
        return render(
            "dashboard.html", request,
            metrics=svc.operational_metrics(),
            classifier=svc.classifier_eval(),
            eligibility=svc.automation_eligibility(),
            conversations=svc.list_conversations(limit=50),
            queue=svc.review_queue(),
        )

    @app.get("/ui/inbox", response_class=HTMLResponse, dependencies=read, include_in_schema=False)
    def inbox(request: Request):
        return render("inbox.html", request, queue=svc.review_queue(),
                      conversations=svc.list_conversations(limit=200))

    @app.get("/ui/conversations/{conversation_id}", response_class=HTMLResponse, dependencies=read,
             include_in_schema=False)
    def conversation(request: Request, conversation_id: str):
        v = svc.conversation_view(conversation_id)
        if v is None:
            return render("not_found.html", request, status=404, what="conversation")
        open_reviews = [r for r in v.reviews if r["status"] == "OPEN"]
        return render(
            "conversation.html", request,
            v=v,
            state=v.state.model_dump(mode="json"),
            trace=svc.trace(conversation_id),
            open_review_id=open_reviews[-1]["id"] if open_reviews else None,
        )

    @app.get("/ui/reviews/{review_id}", response_class=HTMLResponse, dependencies=read, include_in_schema=False)
    def review(request: Request, review_id: str):
        d = svc.review_detail(review_id)
        if d is None:
            return render("not_found.html", request, status=404, what="review")
        return render("review.html", request, d=d)

    @app.post("/ui/reviews/{review_id}/validate", dependencies=write, include_in_schema=False)
    def ui_validate(review_id: str, text: str = Form("")):
        try:
            return JSONResponse(svc.validate_review_text(review_id, text))
        except NotFound:
            return JSONResponse({"error": "review not found"}, status_code=404)

    @app.post("/ui/reviews/{review_id}/decide", include_in_schema=False)
    def decide(
        review_id: str,
        action: str = Form(...),
        edited_text: str = Form(""),
        notes: str = Form(""),
        user: str = Depends(ui_auth),
        _o: None = Depends(same_origin),
    ):
        try:
            res = svc.decide_review(
                review_id, action=action, edited_text=edited_text or None,
                reviewer=user, notes=notes or None,
            )
        except (ValueError, NotFound) as exc:
            return redirect(f"/ui/reviews/{review_id}", str(exc), "bad")
        if res["blocked"]:
            codes = ", ".join(sorted({f["code"] for f in res["validation_findings"] if f["severity"] == "reject"}))
            return redirect(f"/ui/reviews/{review_id}",
                            f"Not sent: the validators rejected the text ({codes}). The review is closed; "
                            "nothing reached the customer.", "bad")
        msg = {
            "APPROVE": "Approved. The reply is queued",
            "EDIT_AND_SEND": "Edited reply validated and queued",
            "REJECT": "Draft rejected. Nothing will be sent",
            "SUPPRESS": "Conversation suppressed",
            "ESCALATE": "Escalated and suppressed",
        }.get(res["action"], "Decision recorded")
        if res.get("send_after"):
            msg += f" for {res['send_after'][:16].replace('T', ' ')} UTC"
        return redirect("/ui/inbox", msg + ".", "good")

    @app.post("/ui/simulate", dependencies=write, include_in_schema=False)
    def simulate(
        client_id: str = Form(""),
        contact_id: str = Form(...),
        text: str = Form(...),
        neighborhood: str = Form(""),
    ):
        client_id = client_id or (svc.e.clients.all()[0].client_id if svc.e.clients.all() else "")
        try:
            r = svc.inbound(client_id=client_id, contact_id=contact_id.strip()[:128], text=text[:2000],
                            neighborhood=neighborhood or None)
        except ValueError as exc:
            return redirect("/", str(exc), "bad")
        return redirect(f"/ui/conversations/{r.conversation_id}", f"Processed: routed {r.routing.value}.", "info")

    @app.post("/ui/process-due", dependencies=write, include_in_schema=False)
    def ui_process_due():
        out = svc.run_worker_pass()
        return redirect("/", f"Worker pass: {len(out['sent'])} sent, {len(out['followups'])} follow-up(s) processed.",
                        "info")

    @app.post("/ui/conversations/{conversation_id}/suppress", dependencies=write, include_in_schema=False)
    def ui_suppress(conversation_id: str, note: str = Form("")):
        try:
            svc.suppress(conversation_id, note=note or None)
        except (ValueError, NotFound) as exc:
            return redirect(f"/ui/conversations/{conversation_id}", str(exc), "bad")
        return redirect(f"/ui/conversations/{conversation_id}", "Suppressed. Queued messages were cancelled.", "good")

    @app.post("/ui/conversations/{conversation_id}/release", dependencies=write, include_in_schema=False)
    def ui_release(conversation_id: str, note: str = Form("")):
        try:
            svc.release(conversation_id, note=note or None)
        except (ValueError, NotFound) as exc:
            return redirect(f"/ui/conversations/{conversation_id}", str(exc), "bad")
        return redirect(f"/ui/conversations/{conversation_id}", "Suppression released.", "good")

    @app.post("/ui/conversations/{conversation_id}/issues/{issue_id}/resolve", include_in_schema=False)
    def ui_resolve(
        conversation_id: str, issue_id: str, note: str = Form(""), release: str = Form(""),
        user: str = Depends(ui_auth), _o: None = Depends(same_origin),
    ):
        try:
            svc.resolve_issue(conversation_id, issue_id, note=note or None,
                              release_suppression=bool(release), resolved_by=user)
        except (ValueError, NotFound) as exc:
            return redirect(f"/ui/conversations/{conversation_id}", str(exc), "bad")
        return redirect(f"/ui/conversations/{conversation_id}", "Issue marked resolved.", "good")

    @app.post("/ui/conversations/{conversation_id}/campaigns", dependencies=write, include_in_schema=False)
    def ui_campaign(conversation_id: str, kind: str = Form(...)):
        try:
            d = svc.trigger_campaign(conversation_id, kind, source="dashboard")
        except (ValueError, NotFound) as exc:
            return redirect(f"/ui/conversations/{conversation_id}", str(exc), "bad")
        if d["allowed"]:
            where = "held for review" if d["review_id"] else f"queued for {str(d['send_after'])[:16].replace('T', ' ')} UTC"
            return redirect(f"/ui/conversations/{conversation_id}",
                            f"{d['kind'].replace('_', ' ').capitalize()} {where}. ({d['reason']})", "good")
        return redirect(f"/ui/conversations/{conversation_id}", f"Blocked: {d['reason']}.", "bad")
