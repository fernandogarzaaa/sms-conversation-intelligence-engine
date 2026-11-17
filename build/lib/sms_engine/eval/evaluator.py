"""Score the classifier against the labelled dataset.

Recall is measured against the labelled *salient* intents (every expected label
should appear in the prediction). Precision is measured against a stricter notion:
a predicted intent is a false positive only if it is a "decision-bearing" intent
the label set did not include — lower-signal extras like REPEAT_BUSINESS_INTENT
riding along with VOLUNTEERED_FUTURE_INTENT are not penalised.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sms_engine.ai.classifier import MultiIntentClassifier
from sms_engine.ai.mock_provider import MockProvider
from sms_engine.eval.dataset import SeedRow, load_seed

# intents that change routing/suppression — precision is enforced on these
DECISION_BEARING = {
    "PURE_POSITIVE", "MIXED_SENTIMENT", "NEGATIVE", "OPEN_ISSUE", "QUESTION",
    "IDENTITY_CORRECTION", "STOP_OPTOUT", "WRONG_NUMBER",
}


@dataclass
class IntentScore:
    intent: str
    support: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class ClassifierEvaluation:
    n: int
    exact_match: int
    salient_recall_hits: int
    salient_recall_total: int
    per_intent: dict[str, IntentScore] = field(default_factory=dict)
    mismatches: list[dict] = field(default_factory=list)
    model: str = "mock"

    @property
    def exact_match_rate(self) -> float:
        return self.exact_match / self.n if self.n else 0.0

    @property
    def salient_recall(self) -> float:
        return self.salient_recall_hits / self.salient_recall_total if self.salient_recall_total else 0.0

    @property
    def macro_f1(self) -> float:
        scores = [s.f1 for s in self.per_intent.values() if s.support]
        return sum(scores) / len(scores) if scores else 0.0

    def summary(self) -> dict:
        return {
            "n": self.n,
            "model": self.model,
            "exact_match_rate": round(self.exact_match_rate, 3),
            "salient_recall": round(self.salient_recall, 3),
            "macro_f1": round(self.macro_f1, 3),
            "per_intent": {
                k: {
                    "support": s.support,
                    "precision": round(s.precision, 3),
                    "recall": round(s.recall, 3),
                    "f1": round(s.f1, 3),
                }
                for k, s in sorted(self.per_intent.items())
            },
            "n_mismatches": len(self.mismatches),
        }


def evaluate_classifier(
    classifier: MultiIntentClassifier | None = None,
    rows: list[SeedRow] | None = None,
) -> ClassifierEvaluation:
    classifier = classifier or MultiIntentClassifier(MockProvider())
    rows = rows or load_seed()

    ev = ClassifierEvaluation(
        n=len(rows), exact_match=0, salient_recall_hits=0, salient_recall_total=0,
        model=classifier.provider.name,
    )

    def score(intent: str) -> IntentScore:
        return ev.per_intent.setdefault(intent, IntentScore(intent=intent))

    for row in rows:
        pred = {i.type.value for i in classifier.classify(row.text).intents}
        exp = set(row.expected_intents)

        for intent in exp:
            s = score(intent)
            s.support += 1
            ev.salient_recall_total += 1
            if intent in pred:
                s.tp += 1
                ev.salient_recall_hits += 1
            else:
                s.fn += 1

        for intent in pred:
            if intent in exp:
                continue
            if intent in DECISION_BEARING:
                score(intent).fp += 1

        if pred == exp:
            ev.exact_match += 1
        else:
            ev.mismatches.append(
                {"id": row.id, "text": row.text, "expected": sorted(exp), "predicted": sorted(pred)}
            )

    return ev
