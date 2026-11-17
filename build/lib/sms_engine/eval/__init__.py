"""Evaluation & learning loop.

- `dataset`   : load the synthetic labelled dataset
- `evaluator` : run the classifier over it, score per-intent precision/recall/F1
- `metrics`   : operational metrics from the live DB (automation rate, approval
                rate, edit rate, suppression rate, latency, ...)
- `thresholds`: turn human-review outcomes into per-intent automation eligibility
"""

from sms_engine.eval.dataset import SeedRow, load_seed
from sms_engine.eval.evaluator import ClassifierEvaluation, evaluate_classifier
from sms_engine.eval.metrics import MetricsService, OperationalMetrics
from sms_engine.eval.thresholds import AutomationEligibility, compute_eligibility

__all__ = [
    "SeedRow",
    "load_seed",
    "ClassifierEvaluation",
    "evaluate_classifier",
    "MetricsService",
    "OperationalMetrics",
    "AutomationEligibility",
    "compute_eligibility",
]
