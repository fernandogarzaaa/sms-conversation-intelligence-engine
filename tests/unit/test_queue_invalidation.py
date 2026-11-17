"""Unit: queue invalidation — never send a stale response (brief §8)."""

from __future__ import annotations

from sms_engine.domain.enums import EventType


def test_second_inbound_invalidates_first_queued_response(engine, pipe):
    r1 = pipe.handle_inbound(client_id="greenline", contact_id="cc", text="Everything went great, thanks!")
    assert r1.planned_response is not None
    plan_id = r1.planned_response.id
    v1 = r1.planned_response.built_at_version

    # customer sends a second message before the first reply is due
    pipe.handle_inbound(client_id="greenline", contact_id="cc",
                        text="Actually wait, the crew left a box in the driveway.")
    plan = engine.planned.get(plan_id)
    assert plan.status == "INVALIDATED"
    assert engine.events.current_version(r1.conversation_id) > v1
    # a RESPONSE_INVALIDATED event was written
    types = [e.type for e in engine.events.list_for_conversation(r1.conversation_id)]
    assert EventType.RESPONSE_INVALIDATED in types


def test_invalidated_response_is_never_sent(engine, pipe):
    r1 = pipe.handle_inbound(client_id="greenline", contact_id="cc2", text="Everything went great, thanks!")
    pipe.handle_inbound(client_id="greenline", contact_id="cc2", text="One more thing, was the shed cleared?")
    # advance well past any send_after
    engine.clock.advance(hours=6)
    sent = pipe.process_due(engine.clock.now())
    assert all(m.raw_text != r1.planned_response.text for m in sent) or not sent


def test_state_is_recomputed_and_reclassified_after_second_message(engine, pipe):
    pipe.handle_inbound(client_id="greenline", contact_id="cc3", text="Everything went great, thanks!")
    r2 = pipe.handle_inbound(client_id="greenline", contact_id="cc3",
                             text="Actually the movers couldn't take the piano, it's still here.")
    assert r2.routing.value == "HUMAN_REVIEW"
    assert r2.state_after.has_unresolved_issue


def test_send_worker_second_guard_invalidates_at_send_time(engine):
    from sms_engine.application.pipeline import InboundPipeline
    pipe = InboundPipeline(engine)
    r1 = pipe.handle_inbound(client_id="greenline", contact_id="cc4", text="Everything went great, thanks!")
    plan_id = r1.planned_response.id
    # manually append a later inbound event WITHOUT going through invalidate_stale
    engine.events.append(
        id=engine.ids.new("evt"), conversation_id=r1.conversation_id, contact_id="cc4",
        type=EventType.SMS_RECEIVED, created_at=engine.clock.now(), payload={"message_id": "mX"},
    )
    engine.clock.advance(hours=2)
    due = engine.queue.due(engine.clock.now())
    assert all(p.id != plan_id for p in due)
    assert engine.planned.get(plan_id).status == "INVALIDATED"
