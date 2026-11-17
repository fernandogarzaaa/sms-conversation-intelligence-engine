# Architecture

> **Guiding principle:** The model generates language. The system owns decisions, state, safety, and truth.

This document was written **before** implementation and is kept current as
subsystems land (last updated for v0.2.0). The implementation log is in
[`IMPLEMENTATION_LOG.md`](IMPLEMENTATION_LOG.md); the reasoning behind each
choice is in [`DECISIONS.md`](DECISIONS.md).

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
SMS provider webhook / POST /messages/inbound          job system: POST /jobs/completed
   │  (signature check, idempotency on provider id)     campaign system: POST .../campaigns
   ▼                                                                │
[tx 1] store message + SMS_RECEIVED                                 │
       invalidate stale queued replies, supersede stale reviews     │
   │                                                                │
   ▼                                                                │
[tx 2] Conversation State  ── rebuilt from the event log            │
   │                                                                │
   ▼                                                                │
Multi-Intent Classifier ─► Entity Extraction ─► opt-out backstop    │
   │   (provider; rules fallback on outage)     (deterministic)     │
   ▼                                                                ▼
Policy / Safety Decision Engine ── deterministic      Campaign gate (state-derived)
   │                                                     │ template copy
   ├──► Human Review Queue  (HUMAN_REVIEW / ESCALATE)    │
   ▼ (AUTO_REPLY eligible only)                          │
Context Retrieval ─► Constrained Generation              │
   │                                                     │
   ▼                                                     ▼
Deterministic Validators  ◄── human-approved text ── outbound_guard(state)
   │
   ▼
Timed Send Queue ─► Send Worker ─► SmsTransport (log | twilio)
                    re-checks version, state, quiet hours; retries; at-most-once
```

If tx 2 raises, it rolls back and the message goes to a HIGH-risk review.
Every hop writes to the **event log** and contributes to a **decision trace**.

## 3. Layering (clean separation)

| Layer | Package | Responsibility | May import |
|-------|---------|----------------|------------|
| **domain** | `sms_engine.domain` | Pure data: state, intents, entities, events, policy results, context, validation, traces. No IO, no framework. | stdlib, pydantic |
| **policy** | `sms_engine.policy` | Deterministic decision + suppression engine, `outbound_guard`, `campaign_check`, opt-out backstop. Pure functions over domain objects. | domain |
| **ai** | `sms_engine.ai` | `LLMProvider` abstraction, `MockProvider`, HTTP providers (OpenAI-compatible, Anthropic), prompts; fail-safe classifier / extractor / generator that *call* a provider and parse its output. Never makes decisions. | domain |
| **context** | `sms_engine.context` | Client context packs, ingestion pipeline, retrieval. | domain |
| **application** | `sms_engine.application` | Use-cases / orchestration: the inbound pipeline, review, campaigns, follow-ups, send worker, background worker, timing, variants, send queue + invalidation, settings, the service facade. | domain, policy, ai, context, infrastructure (via interfaces) |
| **infrastructure** | `sms_engine.infrastructure` | SQLite persistence (thread-safe connection, transactions, migrations), repositories, SMS transports, `Clock`, seeded `Rng`, logging. | domain |
| **api** | `sms_engine.api` | FastAPI routes + request/response schemas. Thin. | application |
| **ui** | `sms_engine.ui` | Jinja2 human-review dashboard. Thin; read-mostly + review, campaign, and issue actions. | application |
| **cli** | `sms_engine.cli` | `sms-engine` command: serve, worker, demo, seed-db, eval, ingest-client, check-config, migrate. | application |
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
├── MockProvider              deterministic, rule-based, no network — default; tests + demo
└── HTTPLLMProvider           retries, timeouts, JSON repair, token + cost accounting
    ├── OpenAICompatibleProvider
    │   ├── OpenAIProvider
    │   ├── OpenRouterProvider
    │   └── LocalProvider     Ollama / vLLM / LM Studio
    └── AnthropicProvider     Messages API
```

`SMS_ENGINE_PROVIDER` selects one (`ai/factory.py`). With a real provider, the
classifier falls back to `MockProvider` on an outage and the pipeline forces
human review for that message. `sms-engine eval` scores any provider on the same
labelled set, recording `model_used`, latency, and cost, so a cloud-vs-local
comparison is a data question, not an assumption.

## 8a. Campaigns, follow-ups, and sending

- **Campaigns** (`application/campaigns.py`): a trigger is a request;
  `policy.campaign_check` decides against the state; copy comes from client
  templates; text is validated against `outbound_guard(state)`; the result is a
  `CAMPAIGN` planned response (or a review when `campaigns.require_review`).
- **Follow-ups** (`application/followups.py`): a volunteered future need is
  scheduled; when due it becomes a human-gated `FOLLOWUP` review, or is cancelled
  for a suppressed contact.
- **Send worker** (`application/send_worker.py`): claims due messages, re-checks
  version, state, and quiet hours, calls the `SmsTransport`, retries with backoff,
  records offers and links sent, and marks stuck sends failed.
- **Background worker** (`application/worker.py`): a loop around one worker pass,
  in the API process (`SMS_ENGINE_WORKER_INTERVAL`) or as `sms-engine worker`.

## 8b. Storage

One SQLite file, schema versioned with `PRAGMA user_version` and append-only
migrations. A `SafeConnection` serialises statements across threads and provides
nested transactions; repositories commit only outside a transaction. The
repositories are the only SQL touch points, so a move to Postgres is contained
in `infrastructure/`.

## 9. Observability

For every inbound message we persist: raw message, classification + confidence,
entities, state-before / state-after, every policy rule that fired, retrieved
context ids, generated response, validation results, final routing decision,
latency, model used. `GET /conversations/:id/trace` returns the human-readable
decision trace.

## 10. Testing strategy

Unit (classifier parsing, policy, suppression, state transitions, validators,
timing, queue invalidation, storage and concurrency, review guards, campaigns,
sending, follow-ups, model independence, providers with mocked HTTP) →
integration (SMS→…→validator) → e2e (scenarios A–G) → adversarial (prompt
injection, hidden opt-out, identity flips, rapid consecutive messages,
contradictions, long/malformed input) → API (auth, webhook signatures,
idempotency) → CLI. 225 tests, offline and deterministic. See
[EVALUATION.md](EVALUATION.md).
