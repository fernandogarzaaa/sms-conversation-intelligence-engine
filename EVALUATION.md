# Evaluation

Two kinds of measurement: **classifier quality** against a labelled dataset, and
**operational metrics** from the live system. Plus the **learning loop** that
turns human decisions into automation eligibility.

## 1. Classifier quality

### Dataset

`data/seed/inbound_messages.jsonl` — **125** synthetic labelled inbound SMS
bodies, regenerated deterministically by `scripts/generate_seed_dataset.py`.
Clearly marked synthetic; not real customer data. Covers every intent
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

## 4. Cloud-vs-local readiness — and a live result

`ProviderResponse` carries `model`, `latency_ms`, `cost_usd`, and token counts.
The classifier and generator record `model_used` and latency into every trace.
Real providers are implemented (OpenAI, OpenRouter, local OpenAI-compatible
servers, Anthropic), so the comparison is now a command:

```bash
SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=<model> sms-engine eval --mismatches
```

A live run through OpenRouter scored **0.984 exact match, 0.993 salient recall,
0.996 macro-F1** (MockProvider: 0.976 / 0.987 / 0.992), and all 35 scenario
checks passed through the full pipeline. The prompt was tuned once while looking
at this same set, so treat it as a development-set score; a held-out set of real,
labelled review outcomes (section 3) is the honest test. Full details, the model,
the remaining mismatches, latency, and cost: [docs/LLM_EVAL_RESULTS.md](docs/LLM_EVAL_RESULTS.md).

Two system properties do not depend on the provider's score and are tested with
deliberately bad models (`tests/unit/test_model_independence.py`):

- a model that never flags opt-outs still cannot cause an opt-out to be missed
  (deterministic backstop, DECISIONS D16);
- a model that is down cannot cause an automated send (rules fallback + forced
  human review, `R_PROVIDER_DEGRADED`).

## 5. Running it

```bash
pytest tests/test_eval.py                        # dataset coverage + quality floor
sms-engine eval --mismatches                     # the configured provider (default: mock)
sms-engine eval --json > eval.json               # machine-readable
curl localhost:8099/metrics
curl localhost:8099/metrics/prometheus
curl localhost:8099/eval/automation-eligibility
```

The operational metrics now also report superseded reviews, campaign steps
allowed and blocked (with the reasons), failed sends, analyses that failed safe,
and planned responses and follow-ups by status.
