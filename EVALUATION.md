# Evaluation

Two kinds of measurement: **classifier quality** against a labelled dataset, and
**operational metrics** from the live system. Plus the **learning loop** that
turns human decisions into automation eligibility.

## 1. Classifier quality

### Dataset

`data/seed/inbound_messages.jsonl` — **125** synthetic labelled inbound SMS
bodies, regenerated deterministically by `scripts/generate_seed_dataset.py`.
Clearly marked synthetic; not real the business customer data. Covers every intent
class plus adversarial rows (prompt injection, opt-out hidden in another message,
contradictory, long multi-signal, ambiguous).

`expected_intents` labels the **salient** intents for a message. The classifier
may also emit lower-signal intents (e.g. `REPEAT_BUSINESS_INTENT` alongside
`VOLUNTEERED_FUTURE_INTENT`) without being marked wrong.

### Method (`sms_engine.eval.evaluator`)

- **Salient recall** — fraction of labelled expected intents that appear in the
  prediction.
- **Exact match** — prediction set equals label set exactly.
- **Per-intent precision / recall / F1** — precision counts a predicted intent as
  a false positive only if it is *decision-bearing* (`PURE_POSITIVE`,
  `MIXED_SENTIMENT`, `NEGATIVE`, `OPEN_ISSUE`, `QUESTION`, `IDENTITY_CORRECTION`,
  `STOP_OPTOUT`, `WRONG_NUMBER`) and not in the label set. Rationale: an extra
  low-signal intent doesn't change routing; a wrong decision-bearing intent does.
- **Macro-F1** — unweighted mean of per-intent F1 over intents with support.

### Current result (MockProvider)

| Metric | Value |
|--------|-------|
| n | 125 |
| Exact match | **0.976** |
| Salient recall | **0.987** |
| Macro-F1 | **0.992** |
| Mismatches | 3 |

`test_eval.py` enforces a floor: salient recall ≥ 0.90, macro-F1 ≥ 0.85, and
precision ≥ 0.75 on `STOP_OPTOUT / NEGATIVE / MIXED_SENTIMENT / OPEN_ISSUE`.

Read the scores with the authorship in mind: the same author wrote the
labelled rows and the `MockProvider` rules, so the scores measure the rules
against their own author's intent, not against independent human judgement.
They are a regression guard for this prototype, not a claim about a real
model. A real model gets a new score from the same tool, on labels it did
not write.

### Known residual mismatches (not overfit away on purpose)

| id | expected | predicted | why it's acceptable |
|----|----------|-----------|---------------------|
| seed_0053 | IDENTITY_CORRECTION, PURE_POSITIVE | + MIXED_SENTIMENT | "…did a great job **though**" — discourse "though" read as contrast. Routes to human review either way. |
| seed_0068 | REPEAT_BUSINESS_INTENT | + PURE_POSITIVE | leading "Great, …" is genuine praise; harmless extra label. |
| seed_0088 | QUESTION, UNRELATED | QUESTION | "Is this the pizza place?" needs world knowledge to tag UNRELATED. Still routed to human review. |

None of the three change the routing decision in a way that could send an unsafe
message — the failure direction is "extra human review", which is the bias we want.

## 2. Operational metrics (`GET /metrics`, `sms_engine.eval.metrics`)

Computed from the event log, traces, and review rows:

| Metric | Meaning |
|--------|---------|
| `messages_processed` | inbound messages run through the pipeline |
| `automation_rate` | share routed `AUTO_REPLY` |
| `human_review_rate` | share routed `HUMAN_REVIEW` or `ESCALATE` |
| `suppression_rate` | share routed `SUPPRESS` or `ESCALATE` |
| `approval_rate` / `edit_rate` / `reject_rate` | over decided reviews |
| `avg_classification_confidence` | mean top-intent confidence over traces |
| `avg_pipeline_latency_ms` | end-to-end pipeline wall time |
| `responses_invalidated` | queued replies killed by a newer inbound message |
| `followups_scheduled` | future-intent tasks created |
| `routing_breakdown`, `by_intent` | counts by routing, and by primary intent |

After the seeded demo (`scripts/demo.py --seed-db`, scenarios A–G, 10 inbound):
automation 30%, human review 50%, suppression 20%, avg confidence ~0.94.

## 3. Learning loop (`GET /eval/automation-eligibility`, `sms_engine.eval.thresholds`)

Every human decision persists: model classification, model confidence, generated
response, human action, human edits, final response (`reviews` table).

`compute_eligibility` joins each decided review to the `primary_intent` on its
trace and reports, per intent:

```
sample, approvals_no_edit, approval_rate_no_edit,
currently_enabled, min_sample, min_approval, eligible, recommendation
```

An intent is `eligible` when `sample ≥ graduation_min_sample` **and**
`approval_rate_no_edit ≥ graduation_min_approval` (defaults 50 and 0.95, both in
`AutomationConfig`). Eligibility is a *recommendation*; enabling auto-send is a
deliberate change to `AutomationConfig`, never automatic (DECISIONS D7).

## 4. Cloud-vs-local readiness

`ProviderResponse` carries `model`, `latency_ms`, and `cost_usd`. The classifier
and generator record `model_used` and latency into every trace. Swapping in a
real `LLMProvider` and re-running `evaluate_classifier(MultiIntentClassifier(provider))`
plus the operational metrics gives a like-for-like comparison of latency, cost,
classification accuracy, and downstream human-approval rate. No assumption is
baked in about which wins.

## 5. Running it

```bash
python -m pytest tests/test_eval.py -q          # dataset coverage + quality floor
python -c "import json; from sms_engine.eval.evaluator import evaluate_classifier as e; print(json.dumps(e().summary(), indent=2))"
curl localhost:8099/metrics
curl localhost:8099/eval/automation-eligibility
```
