# Implementation Log

Chronological, honest record of what was built and its state.

## Phase 1 — Repository inspection & architecture
- `E:\` is not a git repo and holds no prior work → greenfield build, nothing to reuse.
- Stack (with user): Python 3.12 + FastAPI, pytest, stdlib `sqlite3`.
- LLM layer: `MockProvider` only (deterministic); real providers are interface stubs.
- Project at `E:\sms_engine-sms`, branch `build/sms-conversation-engine`.
- `ARCHITECTURE.md` written before implementation.

## Phase 2 — Domain models + persistent conversation state
- `sms_engine.domain`: enums, `Message`, `ConversationState` (+ `Identity`, `Fact` with
  provenance, `IntentRecord`, `OpenIssue`, `Offer`, `FutureIntent`, `Suppression`),
  `Classification`, `Entities`, `Event`, `PolicyOutcome`, `ClientContext`,
  `ValidationResult`, `PlannedResponse`, `DecisionTrace`. Pydantic v2, no IO.
- `sms_engine.infrastructure`: `Clock` (System/Fixed), seeded `Rng`, `IdFactory`,
  SQLite schema + `Database`, repositories (clients, conversations, messages, events,
  planned_responses, reviews, traces, followups).

## Phase 3 — Event system + state machine
- `application/state_builder.build_state`: pure reducer, `state = fold(apply, events)`,
  ordered by `seq`. `EventRepo.append` assigns monotonic per-conversation `seq`.

## Phase 4 — Deterministic policy / suppression engine
- `sms_engine.policy`: `actions` vocab; `PolicyEngine` with an ordered rule set folded
  by precedence into one `PolicyOutcome` (routing / risk / forbidden+allowed actions /
  suppression directive / `requires_grounding` / `primary_intent`). False-negative bias.
- Rules: opt-out, already-opted-out, negative→escalate, wrong-number, mixed/open-issue,
  prior-open-issue, identity-correction, deferral, question(+grounding), pure-positive
  (+blockers, +automation config), unrelated, low-confidence, no-repeat-referral-ask,
  no-repeat-offers.

## Phase 5 — Multi-intent classifier abstraction
- `ai/provider.py`: `LLMProvider` ABC + `ProviderResponse` + `GenerateSpec`;
  `OpenAIProvider`/`LocalProvider` stubs raising `NotImplementedError`.
- `ai/mock_provider.py`: transparent regex/keyword multi-intent classifier.
- `ai/classifier.py`: parses/bounds provider output → `Classification` (clamps
  confidence, drops unknown labels, dedups).

## Phase 6 — Entity extraction
- `ai/mock_provider.extract_entities` + `ai/extractor.py` → `Entities`
  (issue/location, positive target, corrected/wrong name, future who/what/when,
  opt-out phrase, wrong-number phrase, question text, urls, money terms).

## Phase 7 — Context ingestion / retrieval
- `context/ingestion.py`: raw sources (website/faq/config/sample replies) →
  `ClientContext`, deterministic, invents nothing.
- `context/retrieval.py`: keyword-overlap FAQ scoring; `grounded` True only for an
  approved FAQ hit on a question.
- `data/clients/greenline_raw.json` sample pack; `context/packs.py` loader.

## Phase 8 — Constrained response generation
- `ai/mock_provider.generate_reply` + `ai/generator.py`: contextual template reply
  referencing the actual conversation; emits claimed `actions`. Runs only on the
  AUTO path and to produce a reviewer draft.

## Phase 9 — Response validation
- `sms_engine.validation`: deterministic battery — empty/length, money/discount, URL
  allowlist + re-sent links, required links, client forbidden phrases, unsupported
  claims, over-reaching commitments, policy-forbidden actions (incl. referral-ask
  phrasing), question grounding, brand-voice warnings, opt-out hard block.

## Phase 10 — Timing + queue invalidation
- `application/timing.py`: seeded-RNG delays by complexity, night→morning hold,
  never :00/:30/whole-minute, no duplicate timestamps.
- `application/variants.py`: variant pool, no repeat to same contact, neighborhood
  distribution.
- `application/send_queue.py`: `built_at_version` = latest `SMS_RECEIVED` seq;
  `invalidate_stale` on new inbound; `due()` re-checks at send time.
- Fix (D4): invalidation keyed on inbound-message version, not raw event count, so a
  plan's own `RESPONSE_PLANNED` event does not invalidate it.

## Phase 11 — Human review UI
- `ui/` Jinja2: dashboard, inbox, review detail (thread, message, intents, entities,
  state, client context, risk, recommended action, draft + validator findings,
  decision trace; Approve/Edit&Send/Reject/Suppress/Escalate), conversation detail.

## Phase 12 — Evaluation / feedback loop
- `eval/dataset.py`, `eval/evaluator.py` (per-intent P/R/F1, salient recall, exact
  match, macro-F1), `eval/metrics.py` (operational), `eval/thresholds.py`
  (per-intent automation eligibility from human decisions; configurable, not auto).
- `application/review_service.py`: applies decisions, re-validates human text,
  stores model classification/confidence/response + human action/edits/final.

## Phase 13 — API
- `api/app.py`: `POST /messages/inbound`, `POST /events`, `POST /process-due`,
  `GET /conversations[/{id}[/state|/trace]]`, `POST /conversations/{id}/review|suppress|release`,
  `GET /review-queue`, `GET /reviews/{id}`, `POST /reviews/{id}/decision`,
  `GET /metrics`, `GET /eval/classifier`, `GET /eval/automation-eligibility`,
  `GET|POST /clients`, `GET /health`. `application/service.py` facade.
- Fix: `check_same_thread=False` for the FastAPI threadpool.

## Phase 14 — Observability
- `DecisionTrace` persisted per inbound message with message / classification /
  confidence / entities / state-before / state-after / policy / retrieved context /
  response / validation / routing / latency / model. `render()` = ASCII vertical
  trace. Exposed at `/conversations/{id}/trace` and in every inbound response.

## Phase 15 — Tests & adversarial testing
- 93 tests: unit (classifier parsing, policy, suppression, state transitions,
  validators, timing, queue invalidation), integration (full pipeline), e2e
  (scenarios A–G via `sms_engine.scenarios`), adversarial (injection, malformed,
  long, hidden opt-out, rapid consecutive, identity flips, intent flips,
  contradictory, validator-bypass attempt), API surface.
- `python -m pytest` → **93 passed**.

## Phase 16 — Dashboard polish
- Theme-consistent dark dashboard; metric cards, routing breakdown, classifier
  eval table, learning-loop table, queue, conversations, live simulate box,
  process-due button.

## Phase 17 — Documentation
- `README.md`, `ARCHITECTURE.md`, `DECISIONS.md` (D1–D10), `EVALUATION.md`,
  `SECURITY.md`, `DEMO.md`, this log.

## Phase 18 — End-to-end demo
- `scripts/demo.py` (console, all scenarios + traces + checks) and
  `scripts/demo.py --seed-db` (writes `sms_engine.db` for the web UI).
- Classifier eval on the 125-row synthetic set: exact 0.976 / salient recall
  0.987 / macro-F1 0.992 (3 residual mismatches, all in the safe direction —
  documented in EVALUATION.md).

## Not done / integration points (honest)
- Real LLM providers not wired (interface only).
- SQLite single shared connection; no API auth; no real SMS transport.
- Retrieval is keyword-overlap, not embeddings.
