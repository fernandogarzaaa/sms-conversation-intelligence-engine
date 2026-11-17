# Changelog

## 0.2.0

From a prototype to a service that can run against a real SMS provider and a
real model. The domain, policy, validation, and state layers keep their design;
the work is the missing integration, safety fixes, and operations.

### Fixed (safety)

- **Stale reviews could be approved.** A new customer message invalidated queued
  replies but not open reviews, so a reviewer could send a draft written for an
  older message. Open reviews are now `SUPERSEDED` (event `REVIEW_SUPERSEDED`)
  and cannot be decided.
- **Human-approved text was validated against an empty policy.** A reviewer could
  add a referral ask or a review request to a thread with an open issue. Human
  text is now validated against `outbound_guard(state)`: the actions the current
  conversation state forbids.
- **An opt-out could be overwritten.** A later `SUPPRESSION_ACTIVATED` with another
  reason (manual, escalated) replaced the opt-out and made the contact reachable
  again. The reducer now treats an opt-out as permanent.
- **An opt-out after an open-issue suppression was not recorded as an opt-out**,
  so later messages were not blocked as opted out. The opt-out now upgrades the
  suppression.
- **No send-time state check.** The send worker re-checked only the message
  version. It now rebuilds the state and cancels on opt-out, on a suppression
  added after planning, and (for campaigns and follow-ups) on an open issue.
- **Quiet hours started at 07:15**, before the US TCPA 8am limit. The default
  window is now 08:15–20:30 in the recipient's IANA timezone (DST-correct), and
  a worker catching up at night defers to the morning.
- **Approve with edited text was recorded as a clean approval**, which inflated
  the learning loop's no-edit approval rate. It is now recorded as `EDIT_AND_SEND`.
- **Thread safety.** One SQLite connection was shared across the FastAPI
  threadpool with no locking; `seq` assignment could race. Statements and units
  of work now run under a lock, and each inbound message is processed in
  transactions (stored first, analysed atomically).
- **Partial decisions on crash.** An exception mid-pipeline left half-written
  events. The analysis now rolls back and the message goes to a high-risk review.

### Added

- Real model providers: OpenAI, OpenRouter, local OpenAI-compatible servers
  (Ollama / vLLM / LM Studio), and Anthropic, with retries, timeouts, JSON
  repair, token and cost accounting. `SMS_ENGINE_PROVIDER` selects one; the
  default is still the offline `MockProvider`.
- Fail-safe model handling: a provider error falls back to the rule classifier
  and never auto-sends (`R_PROVIDER_DEGRADED`). A deterministic opt-out backstop
  runs on every message, independent of the model.
- Campaign steps (`review_request`, `referral_ask`, `referral_link`) gated by
  state: suppression, open issues, open reviews, and offers already sent. A
  customer who already committed to refer gets their own referral link instead
  of the pitch (the John case, end to end).
- Follow-up processing: due follow-ups become human-gated review items, or are
  cancelled for suppressed contacts.
- SMS transports (`log`, `twilio`), transport retries with backoff, and stuck-send
  recovery. Twilio inbound webhook with signature verification and `MessageSid`
  idempotency.
- Background worker (in-process or `sms-engine worker`).
- `POST /jobs/completed`, campaign, issue-resolution, validation-preview, outbound,
  config, and Prometheus endpoints; conversation filters and pagination.
- API-key auth for the JSON API, HTTP Basic auth for the dashboard, a same-origin
  check on dashboard POSTs, and security headers.
- `R_REPEATED_ISSUES`: two open issues on one contact escalate (the sample
  client's own escalation rule, previously not enforced).
- Staff can mark an issue resolved and release an open-issue suppression.
- Automation config from TOML (`config/automation.example.toml`), runtime settings
  from environment variables (`.env.example`).
- Schema migrations (`PRAGMA user_version`), idempotency key on messages.
- `sms-engine` CLI: `serve`, `worker`, `demo`, `seed-db`, `eval`, `ingest-client`,
  `check-config`, `migrate`.
- Dockerfile, docker-compose, Makefile, GitHub Actions CI (lint, tests on 3.11–3.13,
  demo, image build), ruff configuration.
- Review page redesign: the decision first (customer message, plain-language
  reasons, draft with live validation), detail in collapsible sections; light
  and dark themes; responsive layout; flash messages that say what happened.
- Tests: 93 → 225.

### Changed

- Variant selection and timestamp de-duplication read history from the database,
  so they survive restarts and agree across workers.
- Variants never add a second "thanks" to a reply that already thanks the customer.
- Mock drafts name the issue location ("I've flagged the grill area") instead of
  pasting the whole clause.
- Data paths resolve without an editable install (`SMS_ENGINE_DATA_DIR`).

## 0.1.0

Initial prototype: conversation state from an event log, multi-intent
classification (rule-based `MockProvider`), deterministic policy and suppression,
grounded FAQ retrieval, constrained generation, the validator battery, timing and
queue invalidation, human review, evaluation and learning loop, API, dashboard.
