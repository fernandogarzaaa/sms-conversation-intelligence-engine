"""Unit: human review cannot bypass guardrails; stale reviews; opt-out stickiness."""

from __future__ import annotations

import pytest

from sms_engine.domain.enums import EventType, ReviewAction, RoutingDecision, SuppressionReason

MAYA = "The crew was fantastic but there was a wasp nest so they couldn't clear the grill area."
JOHN = "This is actually John. Everything went great. I'll definitely recommend you."


def test_new_inbound_supersedes_the_open_review(engine, pipe):
    r1 = pipe.handle_inbound(client_id="greenline", contact_id="sup", text=MAYA)
    r2 = pipe.handle_inbound(client_id="greenline", contact_id="sup", text="Also, when can someone come back?")
    assert r1.review_id in r2.superseded_review_ids
    assert engine.reviews.get(r1.review_id)["status"] == "SUPERSEDED"
    assert engine.reviews.get(r2.review_id)["status"] == "OPEN"
    assert any(s.stage == "REVIEW_SUPERSEDED" for s in r2.trace.steps)
    # the stale draft cannot be approved any more
    with pytest.raises(ValueError, match="superseded"):
        pipe_svc_decide(engine, r1.review_id, ReviewAction.APPROVE)
    # and the open issue still keeps the newer message away from automation
    assert r2.routing != RoutingDecision.AUTO_REPLY


def pipe_svc_decide(engine, review_id, action, **kw):
    from sms_engine.application.review_service import ReviewService

    return ReviewService(engine).decide(review_id, action=action, **kw)


@pytest.mark.parametrize("text,code", [
    ("Glad to hear it! Know anyone who needs a move? Send them our way.", "CAMPAIGN_ACTION_FORBIDDEN"),
    ("Thanks Maya! If you have a sec, could you leave us a review?", "CAMPAIGN_ACTION_FORBIDDEN"),
    ("Sorry about that, here's 20% off your next job.", "MONEY_TERM"),
])
def test_human_edit_is_validated_against_current_state(engine, pipe, text, code):
    r = pipe.handle_inbound(client_id="greenline", contact_id=f"guard{abs(hash(text)) % 1000}", text=MAYA)
    res = pipe_svc_decide(engine, r.review_id, ReviewAction.EDIT_AND_SEND, edited_text=text, reviewer="t")
    assert res.blocked and res.planned_response is None
    assert code in {f.code.value for f in res.validation.rejects}
    row = engine.reviews.get(r.review_id)
    assert row["status"] == "DECIDED" and row["final_response"] is None


def test_plain_acknowledgement_on_open_issue_is_allowed(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="ackok", text=MAYA)
    res = pipe_svc_decide(engine, r.review_id, ReviewAction.EDIT_AND_SEND,
                          edited_text="Thanks Maya. I've flagged the grill area for the team.")
    assert not res.blocked and res.planned_response.status == "PLANNED"


def test_approve_with_changed_text_is_recorded_as_an_edit(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="appedit", text=JOHN)
    res = pipe_svc_decide(engine, r.review_id, ReviewAction.APPROVE,
                          edited_text="Thanks John, name updated. Appreciate you spreading the word.")
    assert res.action == ReviewAction.EDIT_AND_SEND and res.edited
    assert engine.reviews.get(r.review_id)["action"] == "EDIT_AND_SEND"


def test_approve_with_identical_text_stays_an_approval(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="appsame", text=JOHN)
    draft = engine.reviews.get(r.review_id)["generated_response"]
    res = pipe_svc_decide(engine, r.review_id, ReviewAction.APPROVE, edited_text="  " + draft + "\n")
    assert res.action == ReviewAction.APPROVE and not res.edited


def test_review_suppress_invalidates_queued_messages(engine, pipe):
    from sms_engine.application.campaigns import CampaignService

    r = pipe.handle_inbound(client_id="greenline", contact_id="revsup", text=JOHN)
    approved = pipe_svc_decide(engine, r.review_id, ReviewAction.APPROVE)
    assert approved.planned_response.status == "PLANNED"
    engine.clock.advance(hours=1)
    pipe.process_due(engine.clock.now())
    # a queued campaign step, then a reviewer suppresses from a later review
    camp = CampaignService(engine).trigger(r.conversation_id, "referral_link")
    assert camp.planned_response.status == "PLANNED"
    r2 = pipe.handle_inbound(client_id="greenline", contact_id="revsup", text="Who is this again? Did I hire you?")
    assert r2.review_id
    assert engine.planned.get(camp.planned_response.id).status == "INVALIDATED"  # newer inbound
    pipe_svc_decide(engine, r2.review_id, ReviewAction.SUPPRESS)
    assert all(p.status != "PLANNED" for p in engine.planned.for_conversation(r.conversation_id))
    assert engine.reviews.get(r2.review_id)["action"] == "SUPPRESS"


def test_repeated_issues_escalate(pipe):
    pipe.handle_inbound(client_id="greenline", contact_id="twice", text=MAYA)
    r = pipe.handle_inbound(client_id="greenline", contact_id="twice",
                            text="And they also scratched the dresser.")
    assert r.routing == RoutingDecision.ESCALATE
    assert any(h.rule_id == "R_REPEATED_ISSUES" for h in r.policy.rule_hits)


def test_opt_out_survives_a_later_manual_suppression(engine, pipe, svc):
    r = pipe.handle_inbound(client_id="greenline", contact_id="sticky", text="STOP")
    assert r.state_after.opted_out
    svc.suppress(r.conversation_id, reason="MANUAL", note="ops click")
    svc.ingest_event(conversation_id=r.conversation_id, contact_id="sticky",
                     type="SUPPRESSION_ACTIVATED", payload={"reason": "ESCALATED"})
    st = svc.conversation_state(r.conversation_id)
    assert st["suppression"]["reason"] == "OPT_OUT" and st["campaign_status"] == "OPTED_OUT"
    with pytest.raises(ValueError):
        svc.release(r.conversation_id)


def test_opt_out_upgrades_an_open_issue_suppression(pipe):
    pipe.handle_inbound(client_id="greenline", contact_id="upg", text=MAYA)
    r = pipe.handle_inbound(client_id="greenline", contact_id="upg", text="Actually just stop texting me.")
    assert r.state_after.opted_out
    assert r.state_after.suppression.reason == SuppressionReason.OPT_OUT
    r3 = pipe.handle_inbound(client_id="greenline", contact_id="upg", text="ok thanks")
    assert r3.routing == RoutingDecision.SUPPRESS


def test_staff_can_resolve_an_issue_and_release(svc, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="resolve", text=MAYA)
    issue = r.state_after.open_issues[0]
    st = svc.resolve_issue(r.conversation_id, issue.id, note="crew came back Tuesday", release_suppression=True)
    assert st["open_issues"][0]["resolved"] is True
    assert st["open_issues"][0]["resolution_note"] == "crew came back Tuesday"
    assert st["suppression"]["active"] is False
    with pytest.raises(ValueError):
        svc.resolve_issue(r.conversation_id, issue.id)


def test_review_required_event_is_logged(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="hrr", text=MAYA)
    evs = [e for e in engine.events.list_for_conversation(r.conversation_id) if e.type == EventType.HUMAN_REVIEW_REQUIRED]
    assert evs and evs[0].payload["review_id"] == r.review_id
