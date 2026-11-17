# Architecture

> **Guiding principle:** The model generates language. The system owns decisions, state, safety, and truth.

This document is written **before** implementation (per the operating instructions) and is
kept current as subsystems land. The implementation log is in
[`IMPLEMENTATION_LOG.md`](IMPLEMENTATION_LOG.md).

## 1. Problem being solved

A service business runs post-job SMS referral/review campaigns for moving & junk-removal
operators under a persona ("Agent"). The current system is:

- scheduled templates + fixed campaign timers
- binary positive/negative sentiment
- immediate automated replies
- little/no conversation memory
- independent campaign nodes
- static client configuration

Two observed production failures motivate the redesign:

| Case | Customer said | Current system did | Required behavior |
|------|---------------|--------------------|-------------------|
| **Maya** | "crew was fantastic BUT a wasp nest prevented clearing junk from the grill area" | classified positive → sent referral pitch → later sent review request | mixed sentiment → detect unresolved issue → **suppress campaign** → human review |
| **John / Olivia** | "This is actually John. Everything went great... I'll definitely recommend you... or if either of us need more service" | ignored identity correction, repeated referral terms, used wrong personalized link | one message → **multiple intents + entities + state mutation** |

## 2. High-level pipeline

```
SMS event
   │
   ▼
Conversation State Manager      ── load / rebuild persistent per-contact state
   │
   ▼
Multi-Intent Classifier         ── structured JSON, many intents + confidence
   │
   ▼
Entity Extraction               ── issue, people, dates, targets, links, opt-out
   │
   ▼
Policy / Safety Decision Engine ── deterministic; owns routing + suppression
   │
   ├───────────────► Human Review Queue        (SUPPRESS / HUMAN_REVIEW / ESCALATE)
   │
   ▼ (AUTO_REPLY eligible only)
Response Planner  ─► Context Retrieval ─► Constrained Response Generation
   │
   ▼
Deterministic Validators        ── money / URLs / required links / forbidden / claims / length
   │
   ▼
SMS Send Queue (timed)          ── human-plausible timing, variant selection, invalidation
```

Every hop writes to the **event log** and contributes to a **decision trace**.

## 3. Layering (clean separation)

| Layer | Package | Responsibility | May import |
|-------|---------|----------------|------------|
| **domain** | `sms_engine.domain` | Pure data: state, intents, entities, events, policy results, context, validation, traces. No IO, no framework. | stdlib, pydantic |
| **policy** | `sms_engine.policy` | Deterministic decision + suppression engine. Pure functions over domain objects. | domain |
| **ai** | `sms_engine.ai` | `LLMProvider` abstraction + `MockProvider`; classifier / extractor / generator that *call* a provider and parse its output. Never makes decisions. | domain |
| **context** | `sms_engine.context` | Client context packs, ingestion pipeline, retrieval. | domain |
| **application** | `sms_engine.application` | Use-cases / orchestration: the inbound pipeline, review service, metrics service, timing engine, variant pool, send queue + invalidation. | domain, policy, ai, context, infrastructure (via interfaces) |
| **infrastructure** | `sms_engine.infrastructure` | SQLite persistence, repositories, `Clock`, seeded `Rng`. | domain |
| **api** | `sms_engine.api` | FastAPI routes + request/response schemas. Thin. | application |
| **ui** | `sms_engine.ui` | Jinja2 human-review dashboard. Thin; read-mostly + review actions. | application |
| **observability** | `sms_engine.observability` | Decision-trace assembly, structured logging. | domain |
| **eval** | `sms_engine.eval` | Seed dataset loader, evaluator (metrics by intent), automation thresholds. | domain, infrastructure |

Dependency rule: arrows point **inward**. `domain` depends on nothing in the project.
`ai` and `policy` never depend on each other. The pipeline in `application` wires them.

## 4. Why deterministic policy lives *outside* the model

- **Safety is falsifiable.** Suppression on an open complaint must be provable by
  reading code and tests, not by trusting a prompt.
- **Auditability.** Operators need "why did/didn't Agent send X" answered as a
  rule id + trace, not a vibe.
- **Regression control.** Prompt drift / model swaps must not change whether a
  STOP request suppresses globally.
- **False-negative bias.** The policy engine is tuned to prefer *not acting*
  (human review) over a dangerous false positive. That bias is a set of explicit
  rules, not an emergent model property.

The model's authority is strictly: *propose intents/entities* and *draft
language*. Both outputs are parsed, bounded, and validated before they can affect
state or reach a customer.

## 5. Conversation state

Persistent, per `contact_id`. Rebuilt deterministically from the event log +
message history on every inbound message (so a late-arriving message can't be
applied to stale state). Shape and provenance rules: see
[state model](src/sms_engine/domain/state.py) and section 3 of the build brief.
Every `Fact` carries `value / source_message_id / timestamp / confidence`.

## 6. Events & state machine

Event types are persisted append-only (`JOB_COMPLETED`, `SMS_RECEIVED`,
`CLASSIFICATION_COMPLETED`, `SUPPRESSION_ACTIVATED`, …). The conversation
"machine" is a reducer: `state = fold(reduce, events)`. Race conditions become
explicit because ordering is by persisted event, and a new `SMS_RECEIVED`
invalidates any `RESPONSE_PLANNED` that has not yet been sent.

## 7. Queue invalidation (the race condition described here)

A planned response is stored with the **event-log version** it was computed
against. The send worker refuses to send if the conversation's current version is
higher. Inbound message #2 bumps the version → message #1's queued response is
`INVALIDATED` → state recomputed → reclassified → replanned. Deterministic and
unit-tested with a fixed clock + seeded RNG.

## 8. AI provider abstraction & local-model readiness

```
LLMProvider (ABC)
├── MockProvider     deterministic, rule-based, no network — used by all tests + demo
├── (OpenAIProvider) interface-only stub; wire when a key is available
└── (LocalProvider)  interface-only stub; for future cost/latency/quality comparison
```

The eval harness records `model_used`, latency, and (later) cost per call so a
cloud-vs-local comparison is a data question, not an assumption.

## 9. Observability

For every inbound message we persist: raw message, classification + confidence,
entities, state-before / state-after, every policy rule that fired, retrieved
context ids, generated response, validation results, final routing decision,
latency, model used. `GET /conversations/:id/trace` returns the human-readable
decision trace.

## 10. Testing strategy

Unit (classifier parsing, policy, suppression, state transitions, validators,
timing, queue invalidation) → integration (SMS→…→validator) → e2e (scenarios
A–G) → adversarial (prompt injection, hidden opt-out, identity flips, rapid
consecutive messages, contradictions, long/malformed input). See
[EVALUATION.md](EVALUATION.md).
