"""Unit: deterministic policy engine routing / risk / allowed actions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sms_engine.application.config import AutomationConfig
from sms_engine.context.packs import default_context
from sms_engine.domain.classification import Classification, IntentPrediction
from sms_engine.domain.entities import Entities
from sms_engine.domain.enums import IntentType, RiskLevel, RoutingDecision, SuppressionReason
from sms_engine.domain.state import ConversationState, OpenIssue, Offer
from sms_engine.policy import actions as A
from sms_engine.policy.engine import PolicyEngine, PolicyInput

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _state(**kw) -> ConversationState:
    base = dict(contact_id="c", conversation_id="v", client_id="greenline")
    base.update(kw)
    return ConversationState(**base)


def _cls(*pairs) -> Classification:
    return Classification(intents=[IntentPrediction(type=t, confidence=c) for t, c in pairs])


def _decide(cls, ent=None, state=None, config=None):
    eng = PolicyEngine(config or AutomationConfig.default())
    return eng.decide(PolicyInput(
        classification=cls, entities=ent or Entities(),
        state_before=state or _state(), client=default_context(),
    ))


def test_pure_positive_high_conf_is_auto_when_enabled():
    out = _decide(_cls((IntentType.PURE_POSITIVE, 0.96)))
    assert out.routing == RoutingDecision.AUTO_REPLY
    assert out.risk == RiskLevel.LOW


def test_pure_positive_low_conf_goes_to_review():
    out = _decide(_cls((IntentType.PURE_POSITIVE, 0.72)))
    assert out.routing == RoutingDecision.HUMAN_REVIEW


def test_pure_positive_routes_to_review_under_conservative_config():
    out = _decide(_cls((IntentType.PURE_POSITIVE, 0.99)), config=AutomationConfig.conservative())
    assert out.routing == RoutingDecision.HUMAN_REVIEW


def test_mixed_sentiment_suppresses_and_reviews_and_forbids_campaign():
    out = _decide(_cls((IntentType.MIXED_SENTIMENT, 0.96), (IntentType.OPEN_ISSUE, 0.93)))
    assert out.routing == RoutingDecision.HUMAN_REVIEW
    assert out.suppression_activate == SuppressionReason.OPEN_ISSUE
    for act in (A.REFERRAL_ASK, A.REVIEW_REQUEST, A.PROMOTIONAL_MESSAGE):
        assert out.forbids(act)
    assert out.risk == RiskLevel.HIGH


def test_negative_escalates_and_suppresses():
    out = _decide(_cls((IntentType.NEGATIVE, 0.9)))
    assert out.routing == RoutingDecision.ESCALATE
    assert out.risk == RiskLevel.CRITICAL
    assert out.forbids(A.ANY_OUTBOUND)


def test_stop_optout_is_global_suppression():
    out = _decide(_cls((IntentType.STOP_OPTOUT, 0.98)))
    assert out.routing == RoutingDecision.SUPPRESS
    assert out.suppression_activate == SuppressionReason.OPT_OUT
    assert out.forbids(A.ANY_OUTBOUND)


def test_identity_correction_forces_review_even_with_positive():
    out = _decide(_cls((IntentType.IDENTITY_CORRECTION, 0.95), (IntentType.PURE_POSITIVE, 0.96),
                        (IntentType.REFERRAL_INTENT, 0.9)))
    assert out.routing == RoutingDecision.HUMAN_REVIEW


def test_question_high_conf_is_auto_but_requires_grounding():
    out = _decide(_cls((IntentType.QUESTION, 0.9)))
    assert out.routing == RoutingDecision.AUTO_REPLY
    assert out.requires_grounding is True


def test_low_confidence_prefers_human_review():
    out = _decide(_cls((IntentType.UNRELATED, 0.4)))
    assert out.routing == RoutingDecision.HUMAN_REVIEW


def test_prior_unresolved_issue_keeps_campaign_suppressed():
    st = _state(open_issues=[OpenIssue(id="i1", description="x", source_message_id="m", opened_at=NOW)])
    out = _decide(_cls((IntentType.PURE_POSITIVE, 0.96)), state=st)
    assert out.routing == RoutingDecision.HUMAN_REVIEW
    assert out.forbids(A.REFERRAL_ASK)


def test_referral_commitment_forbids_asking_again():
    out = _decide(_cls((IntentType.PURE_POSITIVE, 0.96), (IntentType.REFERRAL_INTENT, 0.9)))
    assert out.forbids(A.REFERRAL_ASK)


def test_already_presented_offer_is_forbidden_to_repeat():
    st = _state(offers_presented=[Offer(kind="review_link", detail="", presented_at=NOW)])
    out = _decide(_cls((IntentType.PURE_POSITIVE, 0.96)), state=st)
    assert out.forbids(A.repeat_offer("review_link"))


def test_no_rule_match_defaults_to_review():
    out = _decide(Classification(intents=[]))
    assert out.routing == RoutingDecision.HUMAN_REVIEW
