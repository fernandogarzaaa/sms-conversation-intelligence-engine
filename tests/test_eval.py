"""Evaluation harness sanity + a quality floor for the mock classifier."""

from __future__ import annotations

from sms_engine.eval.dataset import load_seed
from sms_engine.eval.evaluator import evaluate_classifier
from sms_engine.eval.metrics import MetricsService
from sms_engine.eval.thresholds import compute_eligibility


def test_seed_dataset_covers_every_intent_class():
    rows = load_seed()
    assert len(rows) >= 100
    seen = set()
    for r in rows:
        seen.update(r.expected_intents)
    required = {
        "PURE_POSITIVE", "MIXED_SENTIMENT", "NEGATIVE", "OPEN_ISSUE", "QUESTION",
        "IDENTITY_CORRECTION", "VOLUNTEERED_FUTURE_INTENT", "STOP_OPTOUT",
        "WRONG_NUMBER", "UNRELATED", "REPEAT_BUSINESS_INTENT", "REFERRAL_INTENT",
        "INLINE_REFERRAL", "DEFERRAL",
    }
    assert required <= seen


def test_mock_classifier_meets_quality_floor():
    ev = evaluate_classifier()
    s = ev.summary()
    assert s["salient_recall"] >= 0.90
    assert s["macro_f1"] >= 0.85
    # decision-bearing intents must not have terrible precision
    for name in ("STOP_OPTOUT", "NEGATIVE", "MIXED_SENTIMENT", "OPEN_ISSUE"):
        assert ev.per_intent[name].precision >= 0.75, name


def test_metrics_and_eligibility_run_on_empty_db(engine):
    m = MetricsService(engine.db).compute()
    assert m.messages_processed == 0
    assert compute_eligibility(engine.db, engine.config) == []


def test_human_decisions_feed_the_learning_loop(svc, pipe, engine):
    # generate several PURE_POSITIVE reviews by disabling automation, then approve them
    from sms_engine.application.config import AutomationConfig
    engine.config = AutomationConfig.conservative()
    engine.policy.config = engine.config
    for i in range(4):
        r = pipe.handle_inbound(client_id="greenline", contact_id=f"ll{i}", text="Everything went great, thanks!")
        assert r.review_id
        svc.decide_review(r.review_id, action="APPROVE", reviewer="t")
    elig = {e["intent"]: e for e in svc.automation_eligibility()}
    assert "PURE_POSITIVE" in elig
    assert elig["PURE_POSITIVE"]["sample"] == 4
    assert elig["PURE_POSITIVE"]["approvals_no_edit"] == 4
