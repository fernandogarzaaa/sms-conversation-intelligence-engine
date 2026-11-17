"""Unit: the state machine reducer (events -> ConversationState)."""

from __future__ import annotations

from datetime import datetime, timezone

from sms_engine.application.state_builder import build_state
from sms_engine.domain.enums import EventType
from sms_engine.domain.events import Event
from sms_engine.domain.messages import Direction, Message

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _ev(seq, type, payload=None, **kw):
    return Event(
        id=f"e{seq}", conversation_id="v", contact_id="c", type=type, seq=seq,
        created_at=NOW, payload=payload or {}, **kw
    )


def _msg(mid, direction=Direction.INBOUND, text="hi"):
    return Message(id=mid, conversation_id="v", direction=direction, raw_text=text, created_at=NOW)


def test_build_state_is_pure_fold_over_ordered_events():
    events = [
        _ev(1, EventType.SMS_RECEIVED, {"message_id": "m1"}),
        _ev(2, EventType.CLASSIFICATION_COMPLETED, {"source_message_id": "m1",
             "intents": [{"type": "PURE_POSITIVE", "confidence": 0.96}]}),
        _ev(3, EventType.STATE_UPDATED, {"facts": [
             {"key": "sentiment_positive_target", "value": "crew",
              "source_message_id": "m1", "confidence": 0.9}]}),
    ]
    st = build_state(contact_id="c", conversation_id="v", client_id="greenline",
                     events=events, messages=[_msg("m1")])
    assert st.version == 3
    assert st.received_messages == ["m1"]
    assert len(st.intents) == 1
    assert st.facts[0].key == "sentiment_positive_target"
    assert st.facts[0].source_message_id == "m1"  # provenance preserved


def test_identity_correction_records_previous_name():
    events = [
        _ev(1, EventType.SMS_RECEIVED, {"message_id": "m1"}),
        _ev(2, EventType.STATE_UPDATED, {"identity": {"name": "Olivia", "source_message_id": "m1"}}),
        _ev(3, EventType.SMS_RECEIVED, {"message_id": "m2"}),
        _ev(4, EventType.STATE_UPDATED, {"identity": {"name": "John", "source_message_id": "m2"}}),
    ]
    st = build_state(contact_id="c", conversation_id="v", client_id="greenline",
                     events=events, messages=[_msg("m1"), _msg("m2")])
    assert st.identity.name == "John"
    assert st.identity.previous_name == "Olivia"


def test_suppression_activate_then_release():
    events = [
        _ev(1, EventType.SMS_RECEIVED, {"message_id": "m1"}),
        _ev(2, EventType.SUPPRESSION_ACTIVATED, {"reason": "OPEN_ISSUE", "since": NOW.isoformat()}),
        _ev(3, EventType.SUPPRESSION_RELEASED, {"released_at": NOW.isoformat()}),
    ]
    st = build_state(contact_id="c", conversation_id="v", client_id="greenline",
                     events=events, messages=[_msg("m1")])
    assert st.suppression.active is False


def test_optout_sets_campaign_status_and_is_sticky():
    events = [
        _ev(1, EventType.SUPPRESSION_ACTIVATED, {"reason": "OPT_OUT", "since": NOW.isoformat()}),
    ]
    st = build_state(contact_id="c", conversation_id="v", client_id="greenline",
                     events=events, messages=[])
    assert st.opted_out
    assert st.campaign_status.value == "OPTED_OUT"


def test_open_issue_then_resolution():
    events = [
        _ev(1, EventType.STATE_UPDATED, {"open_issues": [
            {"id": "i1", "description": "grill area", "source_message_id": "m1"}]}),
        _ev(2, EventType.STATE_UPDATED, {"resolve_issues": ["i1"]}),
    ]
    st = build_state(contact_id="c", conversation_id="v", client_id="greenline",
                     events=events, messages=[])
    assert st.has_unresolved_issue is False
    assert st.open_issues[0].resolved is True


def test_events_applied_in_seq_order_regardless_of_list_order():
    events = [
        _ev(3, EventType.STATE_UPDATED, {"referral_status": "COMMITTED"}),
        _ev(1, EventType.SMS_RECEIVED, {"message_id": "m1"}),
        _ev(2, EventType.STATE_UPDATED, {"referral_status": "ASKED"}),
    ]
    st = build_state(contact_id="c", conversation_id="v", client_id="greenline",
                     events=events, messages=[_msg("m1")])
    assert st.referral_status.value == "COMMITTED"  # seq 3 wins
