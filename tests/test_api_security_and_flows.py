"""API: authentication, the provider webhook, and the v0.2 endpoints."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from sms_engine.api.app import create_app
from sms_engine.application.settings import Settings
from sms_engine.infrastructure.transport import twilio_signature

MAYA = "The crew was fantastic but there was a wasp nest so they couldn't clear the grill area."


def _client(**kw) -> TestClient:
    s = Settings(db_path=":memory:", **kw)
    return TestClient(create_app(settings=s))


@pytest.fixture
def open_client():
    return _client()


@pytest.fixture
def secured():
    return _client(api_keys=["k-123"], ui_password="pw")


def _basic(user: str, pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


# -- auth ----------------------------------------------------------------------------

def test_api_requires_key_when_configured(secured):
    assert secured.get("/health").status_code == 200  # always open
    assert secured.get("/conversations").status_code == 401
    assert secured.get("/conversations", headers={"X-API-Key": "wrong"}).status_code == 401
    assert secured.get("/conversations", headers={"X-API-Key": "k-123"}).status_code == 200
    assert secured.get("/conversations", headers={"Authorization": "Bearer k-123"}).status_code == 200


def test_ui_requires_basic_auth_when_configured(secured):
    r = secured.get("/ui/inbox")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert secured.get("/ui/inbox", headers=_basic("dana", "nope")).status_code == 401
    ok = secured.get("/ui/inbox", headers=_basic("dana", "pw"))
    assert ok.status_code == 200
    assert ok.headers["x-frame-options"] == "DENY"
    # an API key is accepted as the dashboard password too
    assert secured.get("/", headers=_basic("ops", "k-123")).status_code == 200


def test_ui_post_from_another_site_is_refused(open_client):
    r = open_client.post("/ui/simulate", data={"contact_id": "x", "text": "hi"},
                         headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    r = open_client.post("/ui/simulate", data={"contact_id": "x", "text": "hi"},
                         headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_reviewer_identity_comes_from_basic_auth(secured):
    j = secured.post("/messages/inbound", headers={"X-API-Key": "k-123"},
                     json={"client_id": "greenline", "contact_id": "who", "text": MAYA}).json()
    r = secured.post(f"/ui/reviews/{j['review_id']}/decide", headers=_basic("dana", "pw"),
                     data={"action": "REJECT"}, follow_redirects=False)
    assert r.status_code == 303
    rev = secured.get(f"/reviews/{j['review_id']}", headers={"X-API-Key": "k-123"}).json()
    assert rev["review"]["reviewer"] == "dana"


# -- Twilio webhook --------------------------------------------------------------------

def _twilio_form(sid="SM0001", body="Everything went great, thanks!", frm="+15125550111"):
    return {"MessageSid": sid, "From": frm, "To": "+15125550100", "Body": body, "AccountSid": "AC1"}


def test_twilio_webhook_rejects_a_bad_signature():
    c = _client(twilio_auth_token="tok")
    r = c.post("/webhooks/twilio/sms", data=_twilio_form(), headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403


def test_twilio_webhook_accepts_a_valid_signature_and_is_idempotent():
    c = _client(twilio_auth_token="tok", api_keys=["k"])
    form = _twilio_form()
    sig = twilio_signature("tok", "http://testserver/webhooks/twilio/sms", form)
    for _ in range(2):  # Twilio retries; the second delivery must be a no-op
        r = c.post("/webhooks/twilio/sms", data=form, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200 and "<Response>" in r.text
    convs = c.get("/conversations", headers={"X-API-Key": "k"}).json()
    assert len(convs) == 1 and convs[0]["phone"] == "+15125550111"
    detail = c.get(f"/conversations/{convs[0]['id']}", headers={"X-API-Key": "k"}).json()
    inbound = [m for m in detail["messages"] if m["direction"] == "INBOUND"]
    assert len(inbound) == 1 and inbound[0]["provider_message_id"] == "SM0001"


def test_twilio_webhook_uses_public_url_behind_a_proxy():
    c = _client(twilio_auth_token="tok", public_base_url="https://sms.example.com")
    form = _twilio_form(sid="SM0002")
    sig = twilio_signature("tok", "https://sms.example.com/webhooks/twilio/sms", form)
    assert c.post("/webhooks/twilio/sms", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200


def test_twilio_webhook_threads_messages_by_phone(open_client):
    open_client.post("/webhooks/twilio/sms", data=_twilio_form(sid="A1", body=MAYA))
    open_client.post("/webhooks/twilio/sms", data=_twilio_form(sid="A2", body="Any update on the grill area?"))
    convs = open_client.get("/conversations").json()
    assert len(convs) == 1 and convs[0]["messages"] == 2


def test_twilio_webhook_for_an_unknown_number_is_404(open_client):
    open_client.post("/clients", json={"client_id": "other", "name": "Other Co"})
    form = _twilio_form()
    form["To"] = "+19995550000"
    assert open_client.post("/webhooks/twilio/sms", data=form).status_code == 404


# -- v0.2 endpoints ------------------------------------------------------------------

def test_job_completed_then_campaign(open_client):
    j = open_client.post("/jobs/completed", json={
        "client_id": "greenline", "contact_id": "job1", "phone": "+15125550123",
        "customer_name": "Olivia", "job_id": "J-77"}).json()
    cid = j["conversation_id"]
    assert j["state"]["identity"]["name"] == "Olivia"
    dry = open_client.post(f"/conversations/{cid}/campaigns", json={"kind": "review_request", "dry_run": True}).json()
    assert dry["allowed"] and dry["planned_response_id"] is None and dry["text"].startswith("Hi Olivia,")
    real = open_client.post(f"/conversations/{cid}/campaigns", json={"kind": "review_request"}).json()
    assert real["allowed"] and real["planned_response_id"]
    assert open_client.post(f"/conversations/{cid}/campaigns", json={"kind": "nope"}).status_code == 422


def test_campaign_trigger_through_events(open_client):
    j = open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "ev1", "text": MAYA}).json()
    r = open_client.post("/events", json={"conversation_id": j["conversation_id"], "contact_id": "ev1",
                                           "type": "CAMPAIGN_TRIGGERED", "payload": {"kind": "review_request"}})
    assert r.status_code == 200 and r.json()["campaign"]["allowed"] is False


def test_internal_event_types_cannot_be_posted(open_client):
    j = open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "ev2", "text": "hi"}).json()
    r = open_client.post("/events", json={"conversation_id": j["conversation_id"], "contact_id": "ev2",
                                           "type": "RESPONSE_SENT", "payload": {}})
    assert r.status_code == 400
    r = open_client.post("/events", json={"conversation_id": "nope", "contact_id": "ev2",
                                           "type": "JOB_COMPLETED", "payload": {}})
    assert r.status_code == 404


def test_resolve_issue_endpoint(open_client):
    j = open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "res", "text": MAYA}).json()
    issue_id = j["state_after"]["open_issues"][0]["id"]
    r = open_client.post(f"/conversations/{j['conversation_id']}/issues/{issue_id}/resolve",
                         json={"note": "done", "release_suppression": True})
    assert r.status_code == 200 and r.json()["suppression"]["active"] is False
    assert open_client.post(f"/conversations/{j['conversation_id']}/issues/zzz/resolve", json={}).status_code == 404


def test_validate_endpoint_previews_findings(open_client):
    j = open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "val", "text": MAYA}).json()
    bad = open_client.post(f"/reviews/{j['review_id']}/validate", json={"text": "Leave us a review!"}).json()
    assert bad["passed"] is False
    good = open_client.post(f"/reviews/{j['review_id']}/validate", json={"text": "Thanks, flagged for the team."}).json()
    assert good["passed"] is True


def test_invalid_review_action_is_422(open_client):
    j = open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "bad", "text": MAYA}).json()
    assert open_client.post(f"/reviews/{j['review_id']}/decision", json={"action": "SHIP_IT"}).status_code == 422
    assert open_client.post("/reviews/nope/decision", json={"action": "REJECT"}).status_code == 404


def test_conversation_filters_and_pagination(open_client):
    for i, t in enumerate([MAYA, "Everything went great, thanks!", "STOP"]):
        open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": f"f{i}", "text": t})
    assert len(open_client.get("/conversations").json()) == 3
    assert len(open_client.get("/conversations?limit=2").json()) == 2
    assert [c["contact_id"] for c in open_client.get("/conversations?status=opted_out").json()] == ["f2"]
    assert {c["contact_id"] for c in open_client.get("/conversations?status=open_issues").json()} == {"f0"}
    assert open_client.get("/conversations?status=bogus").status_code == 400


def test_inbound_text_length_is_bounded(open_client):
    r = open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "x", "text": "a" * 5000})
    assert r.status_code == 422


def test_ops_endpoints(open_client):
    open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": "ops",
                                                 "text": "Everything went great, thanks!"})
    h = open_client.get("/health").json()
    assert h["ok"] and h["provider"] == "mock" and h["transport"] == "log" and h["schema_version"] >= 2
    prom = open_client.get("/metrics/prometheus").text
    assert 'sms_engine_routing_total{routing="AUTO_REPLY"} 1.0' in prom
    assert open_client.get("/config/automation").json()["intents"]["PURE_POSITIVE"]["enabled"] is True
    assert open_client.get("/outbound").json()[0]["status"] == "PLANNED"
    assert "followups" in open_client.post("/worker/run").json()


def test_all_ui_pages_render_for_every_flow(open_client):
    for cid, t in [("u1", MAYA), ("u2", "This is actually John. Everything went great. I'll definitely recommend you."),
                   ("u3", "STOP"), ("u4", "My daughter is moving next spring. I'll probably need help.")]:
        open_client.post("/messages/inbound", json={"client_id": "greenline", "contact_id": cid, "text": t})
    for page in ("/", "/ui/inbox"):
        assert open_client.get(page).status_code == 200
    for rv in open_client.get("/review-queue").json():
        page = open_client.get(f"/ui/reviews/{rv['id']}")
        assert page.status_code == 200 and "Why this needs you" in page.text
    for cv in open_client.get("/conversations").json():
        assert open_client.get(f"/ui/conversations/{cv['id']}").status_code == 200
    assert open_client.get("/ui/reviews/nope").status_code == 404
    assert open_client.get("/ui/conversations/nope").status_code == 404
