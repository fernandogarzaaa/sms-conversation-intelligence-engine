"""Unit: campaign gating, the send worker's guards and retries, follow-up processing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sms_engine.application.campaigns import CampaignService, referral_link_for
from sms_engine.application.config import AutomationConfig, CampaignConfig
from sms_engine.application.followups import FollowupService
from sms_engine.application.pipeline import InboundPipeline
from sms_engine.application.review_service import ReviewService
from sms_engine.application.send_worker import SendWorker
from sms_engine.application.service import SmsEngineService
from sms_engine.application.wiring import deterministic_engine
from sms_engine.context.packs import default_context
from sms_engine.domain.enums import EventType, ReviewAction
from sms_engine.infrastructure.transport import FailingTransport, LogTransport

START = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)  # 09:30 in Austin
MAYA = "The crew was fantastic but there was a wasp nest so they couldn't clear the grill area."
JOHN = "This is actually John. Everything went great. I'll definitely recommend you."


def _engine(**kw):
    e = deterministic_engine(START, seed=42, **kw)
    e.clients.upsert(default_context())
    return e


def _drain(engine):
    engine.clock.advance(hours=2)
    return SendWorker(engine).process_due(engine.clock.now())


# -- campaigns ------------------------------------------------------------------

def test_review_request_is_sent_once(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="camp_a", text="Everything went great, thanks!")
    _drain(engine)
    camps = CampaignService(engine)
    d = camps.trigger(r.conversation_id, "review_request")
    assert d.allowed and d.planned_response.status == "PLANNED"
    assert default_context().review_link in d.text
    sent = _drain(engine)
    assert [m.raw_text for m in sent] == [d.text]
    again = camps.trigger(r.conversation_id, "review_request")
    assert not again.allowed and "already sent" in again.reason


def test_maya_campaign_is_blocked_by_suppression(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="camp_maya", text=MAYA)
    for kind in ("review_request", "referral_ask", "referral_link"):
        d = CampaignService(engine).trigger(r.conversation_id, kind)
        assert not d.allowed and "suppressed" in d.reason
    evs = [e for e in engine.events.list_for_conversation(r.conversation_id) if e.type == EventType.CAMPAIGN_TRIGGERED]
    assert len(evs) == 3 and not any(e.payload["allowed"] for e in evs)


def test_john_gets_his_own_link_instead_of_the_pitch(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="john_1234", text=JOHN)
    ReviewService(engine).decide(r.review_id, action=ReviewAction.APPROVE)
    _drain(engine)
    d = CampaignService(engine).trigger(r.conversation_id, "referral_ask")
    assert d.allowed and d.requested_kind == "referral_ask" and d.kind == "referral_link"
    link = referral_link_for(default_context(), "john_1234")
    assert link == "https://ref.greenline.example/john_1234"
    assert link in d.text and d.text.startswith("Hi John,")
    assert "know anyone" not in d.text.lower()
    _drain(engine)
    st = SmsEngineService(engine).conversation_state(r.conversation_id)
    assert "referral_link" in {o["kind"] for o in st["offers_presented"]}
    assert link in st["links_sent"]
    assert not CampaignService(engine).trigger(r.conversation_id, "referral_link").allowed


def test_referral_ask_records_asked_status(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="ask1", text="Everything went great, thanks!")
    _drain(engine)
    d = CampaignService(engine).trigger(r.conversation_id, "referral_ask")
    assert d.allowed and d.kind == "referral_ask"
    _drain(engine)
    st = SmsEngineService(engine).conversation_state(r.conversation_id)
    assert st["referral_status"] == "ASKED"
    assert {"referral_terms", "referral_link"} <= {o["kind"] for o in st["offers_presented"]}


def test_campaign_waits_while_a_review_is_open(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="openrev", text=JOHN)
    d = CampaignService(engine).trigger(r.conversation_id, "review_request")
    assert not d.allowed and "review is open" in d.reason


def test_dry_run_writes_nothing(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="dry", text="Everything went great, thanks!")
    before = len(engine.events.list_for_conversation(r.conversation_id))
    d = CampaignService(engine).trigger(r.conversation_id, "review_request", dry_run=True)
    assert d.allowed and d.text and d.planned_response is None
    assert len(engine.events.list_for_conversation(r.conversation_id)) == before


def test_campaigns_can_require_review():
    e = _engine(config=AutomationConfig(campaigns=CampaignConfig(require_review=True)))
    pipe = InboundPipeline(e)
    r = pipe.handle_inbound(client_id="greenline", contact_id="gated", text="Everything went great, thanks!")
    d = CampaignService(e).trigger(r.conversation_id, "review_request")
    assert d.allowed and d.review_id and d.planned_response.status == "PENDING_REVIEW"
    res = ReviewService(e).decide(d.review_id, action=ReviewAction.APPROVE)
    assert res.planned_response.id == d.planned_response.id
    assert e.planned.get(d.planned_response.id).status == "PLANNED"
    # campaign reviews do not pollute the reply learning loop
    from sms_engine.eval.thresholds import compute_eligibility

    assert compute_eligibility(e.db, e.config) == []


def test_unknown_campaign_kind_is_rejected(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="bad", text="Everything went great, thanks!")
    with pytest.raises(ValueError):
        CampaignService(engine).trigger(r.conversation_id, "spam_blast")


# -- send worker ------------------------------------------------------------------

def test_opt_out_between_plan_and_send_cancels(engine, pipe, svc):
    r = pipe.handle_inbound(client_id="greenline", contact_id="late_stop", text="Everything went great, thanks!")
    plan = r.planned_response
    # the provider reports a STOP through its own channel (not an inbound SMS)
    svc.ingest_event(conversation_id=r.conversation_id, contact_id="late_stop",
                     type="SUPPRESSION_ACTIVATED", payload={"reason": "OPT_OUT"})
    assert _drain(engine) == []
    assert engine.planned.get(plan.id).status in ("INVALIDATED", "CANCELLED")


def test_transport_failure_is_retried_then_delivered():
    transport = FailingTransport(failures=1)
    e = _engine(transport=transport)
    r = InboundPipeline(e).handle_inbound(client_id="greenline", contact_id="retry", phone="+15125550199",
                                          text="Everything went great, thanks!")
    e.clock.advance(hours=1)
    assert SendWorker(e).process_due(e.clock.now()) == []
    p = e.planned.get(r.planned_response.id)
    assert p.status == "PLANNED" and p.send_attempts == 1 and "outage" in p.last_error
    e.clock.advance(minutes=10)
    sent = SendWorker(e).process_due(e.clock.now())
    assert len(sent) == 1 and sent[0].provider_message_id == "FAKE0002"
    assert transport.sent[0].to == "+15125550199" and transport.sent[0].from_ == "+15125550100"


def test_transport_gives_up_after_max_attempts():
    e = _engine(transport=FailingTransport(failures=99), config=AutomationConfig())
    r = InboundPipeline(e).handle_inbound(client_id="greenline", contact_id="giveup", text="Everything went great, thanks!")
    for _ in range(4):
        e.clock.advance(hours=1)
        SendWorker(e).process_due(e.clock.now())
    p = e.planned.get(r.planned_response.id)
    assert p.status == "FAILED" and p.send_attempts == 3
    types = [ev.type for ev in e.events.list_for_conversation(r.conversation_id)]
    assert EventType.RESPONSE_FAILED in types


def test_non_retryable_failure_fails_immediately():
    e = _engine(transport=FailingTransport(failures=99, retryable=False))
    r = InboundPipeline(e).handle_inbound(client_id="greenline", contact_id="nonretry", text="Everything went great, thanks!")
    e.clock.advance(hours=1)
    SendWorker(e).process_due(e.clock.now())
    assert e.planned.get(r.planned_response.id).status == "FAILED"


def test_catch_up_at_night_is_deferred_to_the_morning(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="night", text="Everything went great, thanks!")
    # worker was down; it wakes at 23:40 Austin time (04:40 UTC next day)
    late = datetime(2026, 8, 21, 4, 40, tzinfo=timezone.utc)
    assert SendWorker(engine).process_due(late) == []
    p = engine.planned.get(r.planned_response.id)
    assert p.status == "PLANNED"
    local_hour = (p.send_after.astimezone(timezone.utc).hour - 5) % 24
    assert 8 <= local_hour <= 10


def test_stuck_sends_are_marked_failed(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="stuck", text="Everything went great, thanks!")
    engine.planned.set_status(r.planned_response.id, "SENDING")
    engine.clock.advance(hours=1)
    ids = SendWorker(engine).recover_stuck(engine.clock.now())
    assert ids == [r.planned_response.id]
    assert engine.planned.get(r.planned_response.id).status == "FAILED"


def test_sent_message_records_transport_metadata(engine, pipe):
    pipe.handle_inbound(client_id="greenline", contact_id="meta", text="Everything went great, thanks!")
    sent = _drain(engine)
    assert sent and sent[0].meta["transport"] == "log"
    assert isinstance(engine.transport, LogTransport) and engine.transport.sent


# -- follow-ups -------------------------------------------------------------------

def test_due_followup_becomes_a_review_and_can_be_sent(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="fut",
                            text="My daughter is moving next spring. I'll probably need help.")
    assert r.followup_ids
    fs = FollowupService(engine)
    assert fs.process_due(engine.clock.now()) == []  # not due yet
    engine.clock.advance(days=31)
    out = fs.process_due(engine.clock.now())
    assert len(out) == 1 and out[0].status == "REVIEW_OPENED"
    review = engine.reviews.get(out[0].review_id)
    assert review["kind"] == "FOLLOWUP" and "daughter" in review["generated_response"]
    ReviewService(engine).decide(review["id"], action=ReviewAction.APPROVE)
    assert engine.followups.get(r.followup_ids[0])["status"] == "SENT_FOR_DELIVERY"
    sent = _drain(engine)
    assert any("daughter" in m.raw_text for m in sent)


def test_followup_is_cancelled_for_a_suppressed_contact(engine, pipe, svc):
    r = pipe.handle_inbound(client_id="greenline", contact_id="futstop",
                            text="My son is moving next summer, might need help.")
    svc.suppress(r.conversation_id, note="customer asked by phone")
    engine.clock.advance(days=31)
    out = FollowupService(engine).process_due(engine.clock.now())
    assert out[0].status == "CANCELLED"
