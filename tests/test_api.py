"""API surface: every endpoint from brief §19 responds and is wired to the engine."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sms_engine.api.app import create_app


@pytest.fixture
def client():
    app = create_app(db_path=":memory:")
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json()["ok"] is True


def test_inbound_returns_full_decision(client):
    r = client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api1",
        "text": "The crew was fantastic but there was a wasp nest so they couldn't clear the grill area.",
    })
    j = r.json()
    assert j["routing"] == "HUMAN_REVIEW"
    assert {"MIXED_SENTIMENT", "OPEN_ISSUE"} <= {i["type"] for i in j["intents"]}
    assert j["decision_trace"]
    assert j["review_id"]


def test_conversation_and_state_endpoints(client):
    j = client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api2", "text": "Everything went great, thanks!"}).json()
    cid = j["conversation_id"]
    assert client.get(f"/conversations/{cid}").status_code == 200
    st = client.get(f"/conversations/{cid}/state").json()
    assert st["contact_id"] == "api2"
    assert client.get(f"/conversations/{cid}/trace").json()["rendered"]


def test_review_queue_and_decision(client):
    j = client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api3",
        "text": "This is actually John. Everything went great. I'll definitely recommend you."}).json()
    q = client.get("/review-queue").json()
    assert any(x["id"] == j["review_id"] for x in q)
    d = client.post(f"/conversations/{j['conversation_id']}/review",
                    json={"action": "APPROVE", "reviewer": "t"})
    assert d.status_code == 200
    assert d.json()["action"] == "APPROVE"


def test_suppress_and_release(client):
    j = client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api4", "text": "Everything went great, thanks!"}).json()
    cid = j["conversation_id"]
    assert client.post(f"/conversations/{cid}/suppress", json={"note": "hold"}).json()["suppression"]["active"] is True
    assert client.post(f"/conversations/{cid}/release", json={}).json()["suppression"]["active"] is False


def test_events_ingestion_endpoint(client):
    j = client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api5", "text": "hi"}).json()
    ev = client.post("/events", json={
        "conversation_id": j["conversation_id"], "contact_id": "api5",
        "type": "JOB_COMPLETED", "payload": {"job_id": "J-9"}})
    assert ev.status_code == 200
    assert ev.json()["type"] == "JOB_COMPLETED"


def test_metrics_and_eval_endpoints(client):
    client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api6", "text": "Everything went great, thanks!"})
    m = client.get("/metrics").json()
    assert m["messages_processed"] >= 1
    assert client.get("/eval/classifier").json()["n"] >= 100
    assert isinstance(client.get("/eval/automation-eligibility").json(), list)


def test_dashboard_and_ui_pages_render(client):
    client.post("/messages/inbound", json={
        "client_id": "greenline", "contact_id": "api7", "text": "Do you move pianos?"})
    assert client.get("/").status_code == 200
    assert client.get("/ui/inbox").status_code == 200
    q = client.get("/review-queue").json()
    if q:
        assert client.get(f"/ui/reviews/{q[0]['id']}").status_code == 200
