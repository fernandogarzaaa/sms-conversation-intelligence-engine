# Technical Handoff — SMS Conversation Intelligence Engine

**Audience:** the engineering team.
**Status:** v0.2.0 — the intelligence / application layer plus the integration seams (real model providers, Twilio transport and webhook, auth, worker). 230 tests passing. See [CHANGELOG.md](CHANGELOG.md) for what changed from the v0.1 prototype.
**Read first:** [ARCHITECTURE.md](ARCHITECTURE.md), [DECISIONS.md](DECISIONS.md), [SECURITY.md](SECURITY.md), [EVALUATION.md](EVALUATION.md), [DEMO.md](DEMO.md).

> ### The one thing to be clear about
>
> This prototype implements **conversation state, multi-intent classification,
> the deterministic policy/suppression engine, grounded context retrieval,
> constrained response generation, deterministic validation, human review, and
> the evaluation/learning loop.**
>
> Since v0.2 it **can** connect to real infrastructure, but nothing is connected
> by default: the default model is the deterministic rule-based `MockProvider`,
> the default SMS transport is `log` (nothing leaves the machine), and auth is
> off until keys are set. Real model providers (OpenAI, OpenRouter, local,
> Anthropic) and a Twilio transport + signed inbound webhook are implemented
> behind configuration. There is still **no CRM connection, no hookup to the
> existing campaign/timer system, and no production datastore** (SQLite).
> Section 19 is the explicit boundary. Section 20 is the integration path.

---

## 1. Problem being solved

A service business runs post-job referral/review SMS campaigns for moving & junk-removal
operators under a persona, **"Agent"**. The current stack is scheduled templates
on fixed timers with a binary positive/negative sentiment classifier, immediate
automated replies, and effectively no conversation memory. Campaign nodes act
independently of each other and of conversation context.

The goal is to replace

```
templates + timers + binary sentiment
```

with

```
event-driven conversation intelligence
  + persistent per-contact state
  + multi-intent classification
  + grounded, context-aware responses
  + deterministic safety / suppression
  + human review
  + measurable learning
```

around one principle: **the model generates language; the system owns decisions,
state, safety, and truth.** The LLM is never in the control path.

## 2. Current behavior

```
CURRENT SYSTEM

SMS Provider
     ↓
Campaign / Timer          scheduled nodes, fixed delays, independent of each other
     ↓
Template                  chosen by binary positive/negative sentiment
     ↓
Customer
```

- Scheduled templates, fixed campaign timers.
- Binary positive / negative classification.
- Immediate automated replies.
- Little/no conversation memory.
- Independent campaign nodes.
- Static client configuration.

Two observed production failures motivate the redesign.

## 3. Observed Maya failure

**Customer:** *"crew was fantastic BUT a wasp nest prevented clearing junk from
the grill area."*

**Current system:** classified the message **positive** → sent the referral
pitch → later sent a review request, with the issue still unresolved.

**Required behavior:** `mixed sentiment → detect unresolved issue → suppress
campaign → human review`. Nothing automated goes out while an issue is open.

**In this prototype (scenario B, `tests/e2e/test_scenarios.py`):**
`MIXED_SENTIMENT` + `OPEN_ISSUE` are both detected → the policy engine activates
**global suppression** (`SUPPRESSION_ACTIVATED / OPEN_ISSUE`) and forbids
`referral_ask`, `review_request`, `promotional_message`, `referral_link_send`,
`discount_offer` → routes to `HUMAN_REVIEW` → no `PlannedResponse` is queued. The
issue clause and location (`grill area`) are extracted and stored with
provenance.

## 4. Observed John failure

**Customer:** *"This is actually John. Everything went great… I will definitely
recommend you… or if either of us need more service."*

**Current system:** ignored the identity correction, repeated referral terms
already presented, and used the wrong personalized referral link.

**Required behavior:** `one message → multiple intents + entities + state
mutation`.

**In this prototype (scenario C):** one message yields `IDENTITY_CORRECTION` +
`PURE_POSITIVE` + `REFERRAL_INTENT`. State mutates: `identity.name = "John"`,
`identity.previous_name = "Olivia"`, `referral_status = COMMITTED`. Because
referral intent is now committed, `referral_ask` becomes a **forbidden action**
(rule `R_NO_REPEAT_REFERRAL_ASK`) so Agent does not ask again. Identity
correction forces `HUMAN_REVIEW` before any send. The generated draft references
the actual conversation ("Thanks John, I've updated your name on our end…").

## 5. Architecture

```
PROTOTYPE — what runs today

SMS Event  (HTTP POST /messages/inbound)
     ↓
Conversation State          rebuilt from the append-only event log every message
     ↓
Multi-Intent Intelligence   structured JSON: many intents + per-intent confidence
     ↓  (+ Entity Extraction)
Policy Engine               DETERMINISTIC — routing, risk, suppression, allowed/forbidden actions
     ↓
Context                     per-client context pack + grounded-FAQ retrieval
     ↓
Response                    constrained generation, only on the AUTO path
     ↓
Validation                  deterministic guardrail battery — money, links, claims, commitments, opt-out
     ↓
Human / Auto                review queue, or timed send queue with race-invalidation
```

**Layering (clean separation, dependencies point inward):**

| Layer | Package | Responsibility |
|-------|---------|----------------|
| domain | `sms_engine.domain` | pure data: state, intents, entities, events, policy results, context, validation, decision trace. No IO, no framework. |
| policy | `sms_engine.policy` | deterministic routing + suppression engine. Pure functions over domain objects. |
| ai | `sms_engine.ai` | `LLMProvider` abstraction + `MockProvider`; classifier / extractor / generator that *call* a provider and parse its output. **Never decides anything.** |
| context | `sms_engine.context` | client context-pack ingestion + retrieval. |
| validation | `sms_engine.validation` | deterministic response validator battery. |
| application | `sms_engine.application` | the inbound pipeline, the state machine, review service, timing engine, variant pool, send queue, metrics facade, composition root. |
| infrastructure | `sms_engine.infrastructure` | SQLite persistence, `Clock`, seeded `Rng`, id factory. |
| eval | `sms_engine.eval` | dataset loader, classifier evaluation, operational metrics, automation-eligibility. |
| api / ui | `sms_engine.api` / `sms_engine.ui` | FastAPI endpoints + server-rendered review dashboard. Thin. |

`domain` depends on nothing in the project. `ai` and `policy` never depend on
each other — the pipeline in `application` wires them. ~5,600 LOC in `src/`.

## 6. Components implemented

| Component | Where | State |
|-----------|-------|-------|
| Domain models (14 intent types, events, state, policy, validation, trace) | `domain/` | ✅ complete |
| Append-only event store + per-conversation monotonic `seq` | `infrastructure/repositories.py::EventRepo` | ✅ |
| Conversation state machine (pure reducer) | `application/state_builder.py` | ✅ |
| Deterministic policy / suppression engine (14 rules) | `policy/engine.py` | ✅ |
| Multi-intent classifier (parse + bound provider output) | `ai/classifier.py` | ✅ |
| Entity extractor | `ai/extractor.py` | ✅ |
| `LLMProvider` abstraction | `ai/provider.py` | ✅ interface |
| `MockProvider` (deterministic rule engine) | `ai/mock_provider.py` | ✅ — **stands in for the LLM** |
| `OpenAIProvider` / `OpenRouterProvider` / `LocalProvider` / `AnthropicProvider` | `ai/llm.py`, `ai/factory.py` | ✅ real HTTP providers, selected by `SMS_ENGINE_PROVIDER` (default: mock) |
| Deterministic opt-out backstop + rules fallback on model outage | `policy/optout.py`, `ai/classifier.py` | ✅ |
| Client context-pack ingestion | `context/ingestion.py` | ✅ |
| Context retrieval + grounded-FAQ gate | `context/retrieval.py` | ✅ (keyword overlap, not embeddings) |
| Constrained response generator | `ai/generator.py` + `mock_provider.generate_reply` | ✅ |
| Deterministic validator battery | `validation/engine.py` | ✅ |
| Timing engine (seeded RNG, night hold, jitter) | `application/timing.py` | ✅ |
| Variant pool (no repeat / neighborhood spread) | `application/variants.py` | ✅ |
| Send queue + race-invalidation | `application/send_queue.py` | ✅ |
| Inbound pipeline (orchestration) | `application/pipeline.py` | ✅ |
| Human review service (decision → eval data) | `application/review_service.py` | ✅ |
| Evaluation: classifier scoring, operational metrics, automation-eligibility | `eval/` | ✅ |
| Decision trace (full observability record) | `domain/trace.py`, assembled in pipeline | ✅ |
| HTTP API (FastAPI) | `api/` | ✅ API-key auth when `SMS_ENGINE_API_KEYS` is set |
| Twilio inbound webhook (signature + idempotency) | `api/app.py` | ✅ |
| Human-review dashboard (Jinja2) | `ui/` | ✅ Basic auth, same-origin POSTs, live validation |
| Campaign gate + campaign service (review request / referral ask / referral link) | `policy/engine.py::campaign_check`, `application/campaigns.py` | ✅ |
| Follow-up processing (due → human-gated review) | `application/followups.py` | ✅ |
| Send worker (state + quiet-hours guards, retries) + SMS transports | `application/send_worker.py`, `infrastructure/transport.py` | ✅ `log` (default), `twilio` |
| Background worker / CLI | `application/worker.py`, `cli.py` | ✅ |
| Seed dataset (125 labelled synthetic inbound messages) | `data/seed/` | ✅ **synthetic, clearly marked** |
| Demo runner (scenarios A–G + traces) | `scripts/demo.py` | ✅ |
| Persistence | SQLite (`infrastructure/db.py`) | ✅ thread-safe, transactional, migrated — ⚠️ one server process (Postgres for more) |

## 7. Conversation state model

`ConversationState` (`domain/state.py`) is **a projection of the event log**, not
a stored record. On every inbound message it is recomputed by
`build_state(...)` = `fold(apply, events ordered by seq)` (`state_builder.py`), so
a late or rapid message can never be applied on top of a stale snapshot.

Shape (per `contact_id` / `conversation_id` / `client_id`):

- `identity` — `name`, `previous_name`, `verified`, `source_message_id`, `updated_at`
- `sent_messages` / `received_messages` — message-id lists
- `facts` — each `Fact` carries **`value` / `source_message_id` / `timestamp` / `confidence`** (mandatory provenance)
- `intents` — `IntentRecord` history (type, confidence, source message, timestamp)
- `open_issues` — `OpenIssue` (id, description, source, opened_at, resolved, resolved_at)
- `offers_presented` — `Offer` (kind, detail, presented_at) — "already said this, don't repeat it"
- `links_sent` — de-dupe outbound links
- `referral_status` — `NONE | ASKED | COMMITTED | COMPLETED | DECLINED`
- `future_intents` — `FutureIntent` (who / what / when_text / source / followup_scheduled_for)
- `campaign_status` — `PENDING | ACTIVE | SUPPRESSED | COMPLETED | OPTED_OUT`
- `suppression` — `active`, `reason`, `since`, `released_at`, `note`
- `last_interaction`, `version` (= seq of the last event folded)

Derived (pure, no IO): `has_unresolved_issue`, `opted_out`,
`referral_already_committed`, `offer_kinds_presented()`, `latest_intent_types()`.

**Events** (append-only, `domain/enums.py::EventType`):
`JOB_COMPLETED`, `CAMPAIGN_TRIGGERED`, `SMS_RECEIVED`, `CLASSIFICATION_COMPLETED`,
`STATE_UPDATED`, `RESPONSE_PLANNED`, `HUMAN_REVIEW_REQUIRED`, `RESPONSE_APPROVED`,
`RESPONSE_REJECTED`, `RESPONSE_INVALIDATED`, `RESPONSE_SENT`,
`SUPPRESSION_ACTIVATED`, `SUPPRESSION_RELEASED`, `SCHEDULED_FOLLOWUP`,
`ESCALATED`. `STATE_UPDATED` carries the consolidated per-message delta
(identity, facts, open_issues, resolve_issues, future_intents, offers,
links_sent, referral_status, campaign_status).

## 8. Classification model

**Not** a binary sentiment classifier. `MultiIntentClassifier.classify(text,
state_summary)` returns a `Classification`: a list of `IntentPrediction`
(`type`, `confidence 0–1`, `rationale`) plus entity hints, `model_used`,
`latency_ms`, and the raw provider payload for observability.

Taxonomy (14): `PURE_POSITIVE`, `MIXED_SENTIMENT`, `NEGATIVE`, `QUESTION`,
`IDENTITY_CORRECTION`, `VOLUNTEERED_FUTURE_INTENT`, `INLINE_REFERRAL`,
`DEFERRAL`, `STOP_OPTOUT`, `WRONG_NUMBER`, `UNRELATED`, `REPEAT_BUSINESS_INTENT`,
`REFERRAL_INTENT`, `OPEN_ISSUE`. One message routinely carries several
(John → 3).

The classifier is responsible for **parsing and bounding** whatever the provider
returns: unknown labels dropped, confidences clamped to `[0,1]`,
de-duplicated, sorted by confidence. The provider is swappable
(§10 of ARCHITECTURE):

```
LLMProvider (ABC)
├── MockProvider     deterministic keyword/regex rules — used by all tests + the demo
├── OpenAI / OpenRouter / Local (OpenAI-compatible)   real HTTP providers
└── AnthropicProvider                                  real HTTP provider
```

`MockProvider` is a **transparent rule engine**, not an LLM. Against the 125-row
synthetic dataset it scores **0.976 exact-match, 0.987 salient recall, macro-F1
0.992** (`GET /eval/classifier`, methodology in EVALUATION.md). This number is a
property of the rule engine; a real model would be re-scored with the same
harness. Entity extraction (`EntityExtractor`) produces `issue` / `issue_location`
/ sentiment targets / corrected & wrong names / future who-what-when / opt-out
phrase / question text / URLs / money terms.

## 9. Policy / suppression engine

`policy/engine.py::PolicyEngine.decide(PolicyInput)` — **deterministic, no
randomness, no model calls.** It evaluates an ordered rule set against the
classification, entities, and **prior** state, and folds the hits into one
`PolicyOutcome`:

- `routing` — folded by precedence `AUTO_REPLY < HUMAN_REVIEW < SUPPRESS < ESCALATE` (most severe wins)
- `risk` — `LOW < MEDIUM < HIGH < CRITICAL` (max)
- `forbidden_actions` / `allowed_actions` — union / derived (`policy/actions.py`)
- `suppression_activate` (`SuppressionReason`) / `suppression_release`
- `requires_grounding` (a `QUESTION` routed AUTO must be grounded or it downgrades)
- `primary_intent` (for eval bucketing) and a human-readable `summary` (rule ids + why)

Rules (`_r_*` in `engine.py`): opt-out, already-opted-out, negative→escalate,
wrong-number, mixed/open-issue, prior-open-issue, identity-correction, deferral,
question(+grounding), pure-positive(+blockers,+automation config), unrelated,
low-confidence, no-repeat-referral-ask, no-repeat-offers. No rule match →
`HUMAN_REVIEW` (false-negative bias).

**Suppression is a global invariant, not a campaign-node setting.** An open
issue, negative sentiment, opt-out, wrong-number, or escalation puts the whole
contact into a state where the policy engine **and** the validator block every
campaign action; opt-out and negative/wrong-number additionally block *all*
outbound. Opt-out is sticky (`release()` refuses on an opted-out contact).

Automation is **configurable and starts disabled** (`application/config.py`):
`global_confidence_floor 0.6`; per-intent `IntentAutomationRule` (`enabled`,
`min_confidence`, `graduation_min_approval 0.95`, `graduation_min_sample 50`).
Defaults enable `PURE_POSITIVE`@0.90 and `QUESTION`@0.80 for the demo;
`AutomationConfig.conservative()` routes everything to a human.

## 10. Context / RAG layer

`context/ingestion.py` turns raw client sources (website copy, FAQ list, company
config, sample review replies) into a structured `ClientContext`: brand voice,
persona name, services, service area, hours, booking policy, referral program
(with a **link template**, no invented dollar amounts), review link, crew names,
FAQ (each item `approved: true|false`), escalation rules, **forbidden claims**,
approved phrases, **allowed URL domains**, `max_sms_chars`. Nothing is invented —
fields the sources don't provide stay empty, and the validator treats "not in
context" as "not allowed to claim." Sample pack: `data/clients/greenline_raw.json`.

`context/retrieval.py::ContextRetriever.retrieve(...)` scores FAQ items by
keyword overlap (`score_threshold 0.34`, or ≥2 token overlap) and returns a
`RetrievedContext` with `faq_hits`, `facts_used`, `snippets`, `retrieved_ids`,
and **`grounded`** — true only when an *approved* FAQ item covers a `QUESTION`.
`grounded` is what lets the policy engine's "answer a question automatically"
path actually fire; a pricing FAQ is marked `approved: false` so it never
auto-answers (scenario D).

> **Production note:** retrieval is keyword-overlap over a small FAQ. For more
> than a handful of entries, swap `ContextRetriever` for an embedding/vector
> store behind the same interface.

## 11. Response generation

Runs **only on the `AUTO_REPLY` path** (and to produce a draft for the reviewer).
`ai/generator.py::ResponseGenerator.generate(GenerateSpec)` — the spec is
everything the model is allowed to see: customer message, recent history, state
summary, client context, **allowed actions**, **forbidden actions**, retrieved
context, intents, entities, persona name, contact name, `max_chars`.

The provider returns `{text, actions}`. The text must **reference the actual
conversation** ("Glad the crew took care of everything. I'll flag the grill area
issue for the team.") and must never invent prices/discounts, promise
unavailable actions, claim authority Agent lacks, fabricate facts, or repeat
information. The draft is **not trusted** — it goes straight to the validators.
A rendered variant wrapper (§13) is re-validated before queueing.

## 12. Validation / guardrails

`validation/engine.py::ResponseValidator.validate(text, ValidationContext)` —
**deterministic Python over the rendered outbound text plus the policy's
forbidden-action set.** The LLM prompt is never the enforcement mechanism.

Checks (→ `reject` unless noted): empty; over `max_sms_chars`; money / discount /
percent / coupon terms (unless an approved phrase); URLs whose domain is not
allow-listed; re-sent links; missing required link (review request without the
review link; referral-link action without a referral link); client-configured
forbidden phrases; unsupported claims ("guaranteed", "best in town", …);
over-reaching commitments ("I'll send someone today", "I can refund you"); any
policy-forbidden action the draft performs; referral-ask phrasing while
`referral_ask` is forbidden; ungrounded answer to a `QUESTION`; opt-out /
full-suppression → any non-empty text is rejected. `warn` (not reject):
brand-voice — >1 "!", generic filler ("we appreciate your feedback"), ALL-CAPS.

On `reject`: the pipeline **regenerates once**, then routes to a human. Nothing
invalid is ever sent — see `tests/adversarial/test_adversarial.py::
test_model_cannot_bypass_validator_even_if_it_emits_money`.

## 13. Timing and race-condition handling

**Timing** (`application/timing.py::TimingEngine`) is separate from the model and
deterministic under a seeded `Rng` (`Rng.derived(salt)` per message):

- complexity buckets: `fast` (short question) / `short` / `medium` / `complex`
  → base delay 35 s … 25 min
- outside the send window (default 08:15–20:30 in the client's IANA timezone,
  DST-correct, TCPA-safe) → held to the next morning, spread over 150 minutes;
  re-checked by the send worker at send time
- jitter; never lands on `:00` or `:30` or a whole minute; no two identical
  send timestamps (`_dedupe`)

**Variants** (`application/variants.py::VariantPool`): a pool of real phrasings
(no artificial typos); a contact never gets the same variant twice in a row;
variants are distributed across a `neighborhood` so people who know each other
don't get identical messages. Deterministic given `(contact_id, neighborhood,
history)`.

**Queue invalidation** — the specific race (build brief §8):

```
Customer message #1  →  response queued for +T
Customer message #2 arrives  →  queued response INVALIDATED
   →  state recomputed  →  reclassified  →  new decision
```

Implemented in `application/send_queue.py`. Every `PlannedResponse` records
`built_at_version` = **the `seq` of the latest `SMS_RECEIVED` event** (an
outbound/bookkeeping event must not invalidate a response — see DECISIONS D4). A
new inbound bumps the input version → `invalidate_stale(...)` marks older planned
responses `INVALIDATED` and emits `RESPONSE_INVALIDATED`. The send worker
(`process_due`) re-checks the version at send time as a second guard. Fully
deterministic under `FixedClock` + seeded `Rng`; covered by
`tests/unit/test_queue_invalidation.py`.

## 14. Human review

`application/review_service.py` + the Jinja dashboard (`ui/`). A review row is
opened for every `HUMAN_REVIEW` / `ESCALATE` routing (not for pure `SUPPRESS` /
opt-out — no human needed there). The reviewer sees: customer, current message,
full thread, detected intents + confidence, extracted entities, conversation
state, client context, risk level, recommended action, the generated draft with
validator findings, and the **decision trace**.

Actions: **Approve · Edit & Send · Reject · Suppress · Escalate**. Human-approved
or human-edited text is **still run through the full validator battery** before
it can be queued (approval does not bypass guardrails).

**Every decision is stored as evaluation data** (`reviews` table): model
classification, model confidence, generated response, human action, human edits,
final response, reviewer, notes, timestamps. That row is the input to §15.

## 15. Evaluation / learning loop

- **Classifier quality** (`eval/evaluator.py`): scored against
  `data/seed/inbound_messages.jsonl` (125 rows, synthetic, every intent class +
  adversarial). Metrics: salient recall, exact-match, per-intent precision /
  recall / F1 (precision enforced only on decision-bearing intents), macro-F1.
  `GET /eval/classifier`. Current: 0.976 / 0.987 / 0.992.
- **Operational metrics** (`eval/metrics.py`): messages processed, automation
  rate, human-review rate, suppression rate, approval / edit / reject rate, avg
  classification confidence, avg pipeline latency, responses invalidated,
  follow-ups scheduled, routing breakdown, per-primary-intent breakdown.
  `GET /metrics`.
- **Automation eligibility** (`eval/thresholds.py`): joins each decided review to
  the `primary_intent` on its trace; per intent reports `sample`,
  `approvals_no_edit`, `approval_rate_no_edit`, whether it is currently enabled,
  and whether it is **eligible** (`sample ≥ graduation_min_sample` **and**
  `rate ≥ graduation_min_approval`). Eligibility is a *recommendation* —
  enabling auto-send is a deliberate edit to `AutomationConfig`, never
  automatic (DECISIONS D7). `GET /eval/automation-eligibility`.
- **Cloud-vs-local readiness:** `ProviderResponse` carries `model`,
  `latency_ms`, `cost_usd`; every trace records `model_used` + latency. Swapping
  in a real `LLMProvider` and re-running the harness is a like-for-like
  comparison — no assumption baked in about which wins.

## 16. API

FastAPI (`api/app.py`). Thin handlers over `application/service.py::
SmsEngineService`. API-key auth on every JSON route when `SMS_ENGINE_API_KEYS`
is set; `/health` is open; the Twilio webhook is authenticated by its signature.
The complete, current list is in the README (section 12) and at `/docs`; the v0.2
additions are `POST /webhooks/twilio/sms`, `POST /jobs/completed`,
`POST /conversations/{id}/campaigns`, `POST /conversations/{id}/issues/{issue_id}/resolve`,
`POST /reviews/{id}/validate`, `POST /process-followups`, `POST /worker/run`,
`GET /outbound`, `GET /config/automation`, `GET /metrics/prometheus`, `GET /clients/{id}`.

| Method + path | Purpose |
|---|---|
| `POST /messages/inbound` | ingest one inbound SMS → full decision (intents, entities, policy, retrieved context, draft, validation, planned response, review id, follow-ups, `state_after`, rendered decision trace, latency) |
| `POST /events` | event ingestion — `JOB_COMPLETED`, `CAMPAIGN_TRIGGERED`, … |
| `POST /process-due` | run the send worker for due planned responses (returns the outbound messages created) |
| `GET /conversations` | list with derived status |
| `GET /conversations/{id}` | conversation + state + messages + events + reviews + follow-ups + traces |
| `GET /conversations/{id}/state` | the projected `ConversationState` |
| `GET /conversations/{id}/trace` | rendered decision trace (`?message_id=` optional) |
| `POST /conversations/{id}/review` | decide the open review for this conversation |
| `POST /conversations/{id}/suppress` | manual suppression (invalidates queued responses) |
| `POST /conversations/{id}/release` | release suppression (refused if opted out) |
| `GET /review-queue` | open reviews, risk-ordered |
| `GET /reviews/{id}` | full reviewer payload |
| `POST /reviews/{id}/decision` | `APPROVE \| EDIT_AND_SEND \| REJECT \| SUPPRESS \| ESCALATE` |
| `GET /metrics` | operational metrics |
| `GET /eval/classifier` | classifier evaluation summary |
| `GET /eval/automation-eligibility` | learning-loop eligibility per intent |
| `GET \| POST /clients` | list / ingest a client context pack |
| `GET /health` | liveness + active provider name |

## 17. Testing

`pytest` → **230 passed** (a few seconds, fully offline & deterministic). The v0.1
suites below are unchanged; v0.2 adds `test_storage_and_safety.py` (migrations,
transactions, concurrency, idempotency, fail-safe), `test_review_guards.py`,
`test_campaigns_and_sending.py`, `test_model_independence.py`,
`test_config_and_timing.py`, `test_worker_and_transport.py`,
`test_llm_provider.py` (providers over mocked HTTP), `test_api_security_and_flows.py`
(auth, webhook signatures, endpoints, every UI page), and `test_cli.py`.

| Suite | File(s) | Covers |
|---|---|---|
| unit | `tests/unit/test_classifier_parsing.py` | provider-output parsing, label/confidence bounding, multi-intent |
| unit | `tests/unit/test_policy_engine.py` | routing, risk, suppression directives, allowed/forbidden actions, config variants |
| unit | `tests/unit/test_suppression.py` | global suppression invariants, sticky opt-out, no-repeat-referral |
| unit | `tests/unit/test_state_transitions.py` | the reducer — provenance, identity `previous_name`, seq ordering, resolution |
| unit | `tests/unit/test_validators.py` | every guardrail (money, URLs, links, claims, commitments, opt-out, grounding) |
| unit | `tests/unit/test_timing.py` | determinism under seed, complexity buckets, night hold, no `:00/:30`, no dup timestamps |
| unit | `tests/unit/test_queue_invalidation.py` | new inbound invalidates queued response; send-time second guard |
| integration | `tests/integration/test_pipeline_flow.py` | SMS → classifier → state → policy → response → validator → queue; observability record; variant distribution |
| e2e | `tests/e2e/test_scenarios.py` | scenarios **A–G** (`sms_engine/scenarios.py`) incl. Maya & John |
| adversarial | `tests/adversarial/test_adversarial.py` | prompt injection, malformed/empty/very-long, opt-out hidden mid-message, rapid consecutive messages, identity flips, intent flips, contradiction, **validator-bypass attempt** |
| api | `tests/test_api.py` | every §16 endpoint responds and is wired to the engine |
| eval | `tests/test_eval.py` | dataset coverage + a classifier quality floor + the learning loop populates from decisions |

## 18. EVE evaluation (historical, v0.1)

> The EVE runs and their `.eve-output/` directory belong to a separate tool and
> are not in this repository. The v0.2 review page was rebuilt around the
> finding below (decision first, plain-language reasons, collapsible detail,
> live validation); it has not been re-scored with EVE.

The human-review dashboard was run through **EVE** (Experience Validation
Engine — separate repo `D:\experience-validation-engine`, plugin updated to
`1b09faf`), persona `first-time-user`, `--cognitive --utility`, seed 7, against
`/` and `/ui/inbox`. Three rounds preserved under `.eve-output/{before,after,final}/`;
analysis in `.eve-output/COMPARISON.md`.

| Target | original | targeted fixes (`d6d2e29`) | final (`f44eaaf`) |
|--------|---------:|---------:|------:|
| `/` dashboard | 35 | 33 | **47** — frustration 0.87 → 0.53, nav/click traps removed, `workflowQuality` restored |
| `/ui/inbox` | 60 | 33 | 28 — the "60" was a 2-step non-attempt; the final build gets the simulated reviewer *through* the inbox via the intended controls, then the dense review-detail screen reads as an error state and it abandons |

Four template-only fixes were applied and independently verified (row-click
scoped to the queue table; "already suppressed" state on the conversation page;
compact intent pills; state-aware nav). **The review-detail page's density
reading as an error to a low-context user is a known remaining issue, out of the
fix scope.** EVE is one persona / one seed per run — directional, not a ship
verdict; a population study is the next step.

This is a **UX finding on the demo dashboard**, which exists only to make the
engineering legible. It does not affect the engine.

## 19. What is prototype vs production

| Concern | Prototype (this repo) | Production needs |
|---|---|---|
| **Inbound SMS** | `POST /messages/inbound`, or the Twilio webhook `POST /webhooks/twilio/sms` (signature-verified, idempotent on `MessageSid`) | An **Event Adapter** behind the real SMS provider's inbound webhook (Twilio / Bandwidth / Telnyx / …), verifying the provider signature and de-duplicating on the provider message SID |
| **Outbound SMS** | The send worker calls an `SmsTransport`: `log` (default, nothing leaves the machine) or `twilio`; retries with backoff; `RESPONSE_SENT` on acceptance. Delivery-status callbacks are not consumed yet. | `process_due` calls the provider send API; on the provider's delivery receipt, emit `RESPONSE_SENT`; handle ret/failure/STOP callbacks |
| **Campaign / job events** | `POST /jobs/completed` and `POST /conversations/{id}/campaigns` (or `CAMPAIGN_TRIGGERED` events) exist and are gated by the engine; the existing timer system is not wired to call them yet | the existing campaign/timer system emits `JOB_COMPLETED` / `CAMPAIGN_TRIGGERED` into `POST /events` |
| **CRM** | none | Read customer/job context in; write identity corrections, opt-outs, referral status, and future-intent follow-ups back out |
| **Model** | Default `MockProvider` (rules). Real providers implemented and live-evaluated (`docs/LLM_EVAL_RESULTS.md`); rules fallback + forced review on outage | Implement one `LLMProvider` (cloud and/or local); shadow-run against `MockProvider`; compare with the eval harness before it can send |
| **Persistence** | one SQLite file; thread-safe connection, transactions, migrations, `(conversation_id, seq)` unique | Postgres (or similar). Repositories are the only touch points; the event store needs a real unique constraint on `(conversation_id, seq)` and row-level locking on append |
| **Auth** | API keys on the API, HTTP Basic on the dashboard, Twilio signatures on the webhook; no per-client tenancy | SSO on the API + review UI; signed API keys for the Event Adapter; per-client tenancy checks |
| **Concurrency** | one process, serialized under a lock; the send worker claims before sending; `Clock` / `Rng` injected for determinism | Multiple workers; the append path must serialize per conversation; `process_due` becomes a locked/leased job |
| **Retrieval** | keyword overlap over a ~5-item FAQ | Embedding/vector store behind `ContextRetriever` |
| **Timing** | background worker (in-process or `sms-engine worker`), quiet hours re-checked at send time | A scheduler / queue (e.g. a delayed-job table drained by a worker, or the provider's own scheduled-send) |
| **Review UI** | bundled Jinja dashboard behind Basic auth | Behind SSO, or expose `/review-queue` + `/reviews/{id}/decision` to the existing ops tooling |
| **Observability** | `DecisionTrace` in SQLite, `GET /metrics`, `GET /metrics/prometheus`, JSON logs (`SMS_ENGINE_LOG_JSON`) | Ship traces to the logging/analytics; metrics to their dashboards; alerting on suppression/escalation rate |
| **Data** | 125 **synthetic** labelled messages, clearly marked; no real customer data | Real labelled review outcomes accumulate via §15; retention / PII handling per company policy |
| **Compliance** | opt-out honored in-engine | Opt-out must also propagate to the provider suppression list **and** the CRM; TCPA/quiet-hours per jurisdiction |

**Everything in the "prototype" column is an integration point, not a
redesign.** The domain / policy / validation / state / eval layers do not change.

## 20. Proposed integration path

```
PRODUCTION INTEGRATION

                    ┌───────────────┐
                    │ the CRM  │  customer + job context in;
                    └───────┬───────┘  identity / opt-out / referral / follow-ups out
                            ↓
SMS Provider  →  Event Adapter          inbound webhook → POST /messages/inbound
   (inbound)                            (verify signature, de-dupe on message SID)
                            ↓
                 Conversation Engine    THIS REPO — unchanged
                            ↓
                   Policy + State       deterministic; owns the decision
                            ↓
                 Response / Review      auto (validated) OR human review queue
                            ↓
                    SMS Provider  →  actual send  (process_due → provider API)
                   (outbound)
                            ↓
                         CRM            write back: sent message, state changes,
                                        follow-up tasks, opt-out propagation
```

### Boundary components (status after v0.2)

| # | Component | Status |
|---|---|---|
| 1 | Event Adapter (inbound) | ✅ for Twilio (`/webhooks/twilio/sms`); other providers follow the same 20-line pattern |
| 2 | Campaign bridge | ✅ endpoints exist and gate every step; ⏳ the existing timer system must call them |
| 3 | Send worker (outbound) | ✅ transport interface + Twilio; ⏳ delivery-status callbacks |
| 4 | CRM sync | ⏳ not built |
| 5 | `LLMProvider` implementation | ✅ OpenAI / OpenRouter / local / Anthropic |
| 6 | Datastore swap | ⏳ SQLite → Postgres (repositories are the only SQL touch points) |
| 7 | Auth + tenancy | ✅ auth; ⏳ per-client tenancy on keys |

The original descriptions follow.

1. **Event Adapter (inbound).** One service that terminates the SMS provider's
   inbound webhook, verifies the provider signature, maps the payload to
   `{client_id, contact_id, text, phone, provider_message_sid, received_at}`,
   enforces idempotency on `provider_message_sid`, and calls
   `POST /messages/inbound`. Also receives `STOP`/`HELP` provider callbacks and
   posts `SUPPRESSION_ACTIVATED` where the provider already handled the opt-out.

2. **Campaign bridge.** the existing campaign/timer system posts `JOB_COMPLETED`
   and `CAMPAIGN_TRIGGERED` to `POST /events` instead of firing a template
   directly. The engine's event-driven state machine already models these.

3. **Send worker (outbound).** Replace the body of
   `InboundPipeline.process_due` so that, instead of writing an `OUTBOUND`
   message row, it calls the provider's send API (respecting the computed
   `send_after`), and emits `RESPONSE_SENT` on the provider's acceptance /
   delivery receipt. Timing, variant selection, and race-invalidation are
   unchanged.

4. **CRM sync.** A small consumer of the event stream: on
   `STATE_UPDATED`/`SUPPRESSION_ACTIVATED`/`SCHEDULED_FOLLOWUP`, write identity
   corrections, opt-outs (also to the provider suppression list), referral
   status, and follow-up tasks into production CRM. Pull job/customer context
   in when a conversation is created.

5. **`LLMProvider` implementation.** Implement `classify_intents`,
   `extract_entities`, `generate_reply` against a real model (cloud and/or
   local) behind the existing interface. No other layer changes.

6. **Datastore swap.** SQLite → Postgres in `infrastructure/`; add the
   `(conversation_id, seq)` unique constraint and per-conversation append
   locking.

7. **Auth + tenancy.** SSO in front of the API and the review UI; signed keys
   for the adapters; enforce `client_id` scoping.

### Rollout phasing (de-risked)

| Phase | Engine does | Sends? | Exit criterion |
|---|---|---|---|
| **0 — Shadow** | classify + extract + state + policy + trace on live inbound copies; nothing routed to send | **No** | classifier + policy agree with human judgement on a review sample; suppression fires correctly on Maya-class messages |
| **1 — Human-gated** | full pipeline; **every** outbound goes through the review queue | Only after a human approves | reviewers report the drafts + traces are usable; approval/edit rates stable |
| **2 — Graduated auto-send** | `PURE_POSITIVE` (then `QUESTION`, grounded only) auto-send once `eval/thresholds` shows ≥ `graduation_min_approval` over ≥ `graduation_min_sample` | Yes, for graduated intents only; everything else still human-gated | per-intent false-positive rate within target; no regression in suppression correctness |
| **3 — Steady state** | new intents graduate the same way, always evidence-driven and always a deliberate `AutomationConfig` change | — | — |

Suppression, opt-out, negative-sentiment escalation, and identity-correction
routing stay **human/deterministic at every phase** — they never graduate to
auto.

---

## Appendix — run it locally

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                          # 230 tests
sms-engine demo                 # scenarios A–G + decision traces
sms-engine demo maya john       # just the two failure cases

sms-engine seed-db
sms-engine serve                # → http://127.0.0.1:8099 (dashboard / inbox / review / conversation)
```

No API keys, no network, no external services by default. Provider = `MockProvider`,
transport = `log`. Production settings: `.env.example`; container: `docker compose up --build`.
