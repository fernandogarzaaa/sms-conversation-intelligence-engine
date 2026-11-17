"""Unit: global suppression invariants (brief §6)."""

from __future__ import annotations

import pytest

from sms_engine.domain.enums import RoutingDecision, SuppressionReason


def _send(pipe, contact, text):
    return pipe.handle_inbound(client_id="greenline", contact_id=contact, text=text)


def test_open_complaint_blocks_all_downstream_campaign_actions(pipe):
    r = _send(pipe, "c1", "Everything was great except the garage is still full of junk.")
    assert r.state_after.suppression.active
    assert r.state_after.suppression.reason == SuppressionReason.OPEN_ISSUE
    assert r.planned_response is None  # nothing queued to send
    # a later "positive" message must NOT re-open campaign automation while issue stands
    r2 = _send(pipe, "c1", "Thanks!")
    assert r2.routing == RoutingDecision.HUMAN_REVIEW
    assert r2.planned_response is None


def test_optout_suppresses_everything_globally(pipe):
    r = _send(pipe, "c2", "Please stop texting me.")
    assert r.state_after.opted_out
    r2 = _send(pipe, "c2", "Actually the move was great, thanks!")
    assert r2.routing == RoutingDecision.SUPPRESS
    assert r2.planned_response is None


def test_optout_hidden_inside_positive_still_suppresses(pipe):
    r = _send(pipe, "c3", "Great job today. Also please remove me from any future messages.")
    assert r.state_after.opted_out
    assert r.routing == RoutingDecision.SUPPRESS


def test_committed_referral_is_not_asked_again(pipe):
    r = _send(pipe, "c4", "Everything went great, I'll definitely recommend you to my neighbors.")
    assert r.state_after.referral_status.value in ("COMMITTED", "COMPLETED")
    from sms_engine.policy import actions as A
    assert r.policy.forbids(A.REFERRAL_ASK)


def test_release_is_refused_after_optout(svc, pipe):
    r = _send(pipe, "c5", "stop")
    with pytest.raises(ValueError):
        svc.release(r.conversation_id)


def test_manual_suppress_then_release_reopens(svc, pipe):
    r = _send(pipe, "c6", "Thanks, everything was smooth.")
    svc.suppress(r.conversation_id, note="holding")
    assert svc.conversation_state(r.conversation_id)["suppression"]["active"] is True
    svc.release(r.conversation_id, note="ok")
    assert svc.conversation_state(r.conversation_id)["suppression"]["active"] is False
