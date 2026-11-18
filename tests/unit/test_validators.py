"""Unit: deterministic response validators (brief §11)."""

from __future__ import annotations

from datetime import datetime, timezone

from sms_engine.context.packs import default_context
from sms_engine.domain.context import RetrievedContext
from sms_engine.domain.enums import RiskLevel, RoutingDecision, SuppressionReason, ValidationCode
from sms_engine.domain.policy import PolicyOutcome
from sms_engine.domain.state import ConversationState, Suppression
from sms_engine.policy import actions as A
from sms_engine.validation.engine import ResponseValidator, ValidationContext

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
CLIENT = default_context()


def _ctx(policy=None, state=None, retrieved=None, actions=None):
    return ValidationContext(
        client=CLIENT,
        policy=policy or PolicyOutcome(routing=RoutingDecision.AUTO_REPLY, risk=RiskLevel.LOW,
                                       allowed_actions=[A.ACKNOWLEDGEMENT]),
        state=state or ConversationState(contact_id="c", conversation_id="v", client_id="greenline"),
        retrieved=retrieved or RetrievedContext(client_id="greenline"),
        actions=actions or [A.ACKNOWLEDGEMENT],
        intents=[],
    )


V = ResponseValidator()


def test_clean_ack_passes():
    assert V.validate("Glad the crew took care of everything. Thanks for the note.", _ctx()).passed


def test_money_term_is_rejected():
    res = V.validate("Here is a $50 discount for the trouble.", _ctx())
    assert not res.passed
    assert any(f.code == ValidationCode.MONEY_TERM for f in res.rejects)


def test_percent_discount_is_rejected():
    res = V.validate("We can give you 10% off next time.", _ctx())
    assert not res.passed


def test_disallowed_url_is_rejected():
    res = V.validate("See https://totally-not-greenline.example/win", _ctx())
    assert any(f.code == ValidationCode.DISALLOWED_URL for f in res.rejects)


def test_allowlisted_url_is_ok():
    res = V.validate("Your link: https://ref.greenline.example/abc", _ctx())
    assert res.passed


def test_forbidden_claim_is_rejected():
    res = V.validate("We guarantee this will never happen again.", _ctx())
    assert any(f.code in (ValidationCode.FORBIDDEN_PHRASE, ValidationCode.UNSUPPORTED_CLAIM)
               for f in res.rejects)


def test_overreaching_commitment_is_rejected():
    res = V.validate("I'll send someone today to fix it.", _ctx())
    assert any(f.code == ValidationCode.OVERREACHING_COMMITMENT for f in res.rejects)


def test_too_long_is_rejected():
    res = V.validate("x" * (CLIENT.max_sms_chars + 1), _ctx())
    assert any(f.code == ValidationCode.TOO_LONG for f in res.rejects)


def test_empty_is_rejected():
    assert not V.validate("   ", _ctx()).passed


def test_opt_out_state_blocks_any_text():
    st = ConversationState(contact_id="c", conversation_id="v", client_id="greenline",
                           suppression=Suppression(active=True, reason=SuppressionReason.OPT_OUT))
    res = V.validate("Hi there!", _ctx(state=st))
    assert not res.passed
    assert any(f.code == ValidationCode.OPT_OUT_NOT_HONORED for f in res.rejects)


def test_referral_ask_when_forbidden_is_rejected():
    pol = PolicyOutcome(routing=RoutingDecision.AUTO_REPLY, risk=RiskLevel.LOW,
                        forbidden_actions=[A.REFERRAL_ASK], allowed_actions=[A.ACKNOWLEDGEMENT])
    res = V.validate("Thanks! Could you refer a friend to us?", _ctx(policy=pol))
    assert any(f.code == ValidationCode.REFERRAL_ASK_AFTER_COMMIT for f in res.rejects)


def test_ungrounded_answer_is_rejected():
    res = V.validate("Yes we move pianos for $200.", _ctx(actions=[A.ANSWER_QUESTION]))
    assert not res.passed


def test_grounded_answer_passes():
    rc = RetrievedContext(client_id="greenline", grounded=True)
    res = V.validate("We move upright pianos with advance notice.", _ctx(actions=[A.ANSWER_QUESTION], retrieved=rc))
    assert res.passed


def test_generic_filler_is_warned_not_rejected():
    res = V.validate("Thanks for reaching out! We appreciate your feedback.", _ctx())
    # exclamation + filler -> warnings, but not a hard reject
    assert res.passed
    assert res.warnings
