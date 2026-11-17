"""Integration: SMS -> classifier -> state -> policy -> response -> validator -> queue."""

from __future__ import annotations

from sms_engine.domain.enums import EventType


def test_full_pipeline_writes_every_observability_field(pipe, engine):
    r = pipe.handle_inbound(client_id="greenline", contact_id="p1", text="Everything went great, thanks!")
    t = r.trace
    assert t.message_text
    assert t.classification["intents"]
    assert t.state_before is not None and t.state_after is not None
    assert t.policy["routing"] == r.routing.value
    assert "retrieved_context" in t.model_dump()
    assert t.latency_ms >= 0
    assert t.model_used == "mock"
    # persisted
    stored = engine.traces.for_message(r.conversation_id, r.message.id)
    assert stored is not None


def test_auto_reply_is_planned_validated_and_timed(pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="p2", text="Everything went great, thanks!")
    assert r.routing.value == "AUTO_REPLY"
    assert r.planned_response.status == "PLANNED"
    assert r.planned_response.validation.passed
    assert r.planned_response.send_after is not None
    assert r.planned_response.variant_id


def test_auto_reply_actually_sends_when_due(pipe, engine):
    r = pipe.handle_inbound(client_id="greenline", contact_id="p3", text="Everything went great, thanks!")
    engine.clock.advance(hours=1)
    sent = pipe.process_due(engine.clock.now())
    assert len(sent) == 1
    assert sent[0].direction.value == "OUTBOUND"
    # state now knows a message was sent
    st = engine.events.list_for_conversation(r.conversation_id)
    assert EventType.RESPONSE_SENT in [e.type for e in st]


def test_event_log_is_append_only_and_ordered(pipe, engine):
    r = pipe.handle_inbound(client_id="greenline", contact_id="p4", text="Do you move pianos?")
    evs = engine.events.list_for_conversation(r.conversation_id)
    seqs = [e.seq for e in evs]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, len(seqs) + 1))


def test_question_auto_answer_is_grounded_in_faq(pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="p5", text="Do you guys move pianos?")
    assert r.routing.value == "AUTO_REPLY"
    assert r.retrieved.grounded
    assert "piano" in r.planned_response.text.lower()


def test_ungrounded_question_downgrades_to_human_review(pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="p6",
                            text="How much do you charge to haul a couch and a mattress?")
    assert r.routing.value == "HUMAN_REVIEW"
    assert r.review_id is not None


def test_two_customers_same_neighborhood_get_different_variants(pipe):
    a = pipe.handle_inbound(client_id="greenline", contact_id="n1", text="Everything went great, thanks!",
                            neighborhood="maple-heights")
    b = pipe.handle_inbound(client_id="greenline", contact_id="n2", text="Everything went great, thanks!",
                            neighborhood="maple-heights")
    assert a.planned_response.variant_id != b.planned_response.variant_id
