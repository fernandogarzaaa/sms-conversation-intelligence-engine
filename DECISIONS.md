# Design decisions

Numbered, with the reasoning and the trade-off accepted. These are the choices a
reviewer would otherwise have to reverse-engineer.

## D1 — The model has no policy authority

**Decision.** The LLM/provider produces exactly two things: a list of proposed
intents+entities, and draft language. Routing, risk, suppression, allowed
actions, state mutations, and "send / don't send" are decided by
`sms_engine.policy` and enforced by `sms_engine.validation`, both deterministic.

**Why.** Safety needs to be falsifiable. "Why did Agent not send a referral ask
to Maya" must be answerable as *rule `R_OPEN_ISSUE` fired* + a trace, not "the
prompt probably handled it". It also means a prompt tweak or model swap cannot
silently change whether `STOP` suppresses globally.

**Trade-off.** The rule engine needs maintenance as new intent patterns appear.
Accepted: rules are cheap to read, test, and diff; a wrong rule is visible.

## D2 — Deterministic MockProvider as the default provider

**Decision.** `MockProvider` is a transparent keyword/regex classifier +
template generator and remains the default. Since v0.2, real providers
(OpenAI, OpenRouter, local OpenAI-compatible servers, Anthropic) implement the
same `LLMProvider` interface and are selected with `SMS_ENGINE_PROVIDER`.

**Why.** The deliverable is the *system*. A deterministic default makes the
whole pipeline testable end-to-end with no network, no keys, no flakiness, and
keeps the demo reproducible. Swapping in a real model changed no state, policy,
validation, or review code — which is the claim the seam was built to prove.

**Trade-off.** The default classifier's language understanding is shallow.
Accepted: `sms-engine eval` scores any provider on the same set, so moving to a
real model is a measured decision (see `docs/LLM_EVAL_RESULTS.md`).

## D3 — Conversation state is a projection of an append-only event log

**Decision.** `ConversationState` is never stored; it is `fold(reduce, events)`
recomputed on every inbound message. `events` is append-only with a
per-conversation monotonic `seq`.

**Why.** It makes the race conditions described here *explicit* and testable: a
late or rapid second message just appends an event and the next projection sees
it. No cache to invalidate, no partial-update bugs. Provenance is natural — every
fact points at the message/event that produced it.

**Trade-off.** Recompute cost grows with thread length. At the business's volume
(hundreds of messages/week, short threads) this is irrelevant; a snapshot cache
keyed by `seq` is a drop-in optimization if needed.

## D4 — "Input version" (not raw event count) is the queue-invalidation key

**Decision.** A `PlannedResponse` records `built_at_version = seq of the latest
SMS_RECEIVED event`. It is stale iff a newer inbound message exists. Outbound /
bookkeeping events (`RESPONSE_PLANNED`, `RESPONSE_SENT`, …) do **not**
invalidate a response.

**Why.** The first implementation used the raw max `seq`, which meant the
`RESPONSE_PLANNED` event a plan writes on enqueue immediately made that plan
"stale" and it was never sent. The invariant we actually want is *"a new customer
message invalidates a queued reply"*.

**Trade-off.** None material. The send worker also re-checks at send time as a
second guard.

## D5 — Suppression is a global invariant, activated via events

**Decision.** Suppression lives on `ConversationState.suppression` and is set by
`SUPPRESSION_ACTIVATED` / released by `SUPPRESSION_RELEASED` events. Any open
unresolved issue, negative sentiment, opt-out, wrong-number, or escalation puts
the whole contact into a state where the validator hard-rejects every campaign
action and every outbound message (for opt-out / wrong-number / escalation).

**Why.** The Maya failure was a campaign node acting without global context.
Making suppression a property of the contact, checked by the policy engine *and*
re-checked by the validator, means no code path can send around it.

**Trade-off.** Opt-out is deliberately sticky — `release()` refuses on an
opted-out contact. That is the safe direction.

## D6 — `QUESTION` can be auto-answered, but only if grounded

**Decision.** Policy routes a high-confidence `QUESTION` to `AUTO_REPLY` with
`requires_grounding=True`. The pipeline then checks retrieval: if no *approved*
FAQ item covers it, it downgrades to `HUMAN_REVIEW`. Pricing questions are
deliberately marked `approved: false` in the sample context so they never
auto-answer.

**Why.** Matches scenario D ("if grounded and approved, response eligible;
otherwise human review") without letting the model invent business facts.

**Trade-off.** Retrieval is keyword-overlap, not embeddings — fine for a
~5-entry FAQ; swap `ContextRetriever` for a vector store at scale.

## D7 — Automation thresholds are configurable and start disabled

**Decision.** `AutomationConfig` holds per-intent `{enabled, min_confidence,
graduation_min_approval, graduation_min_sample}`. In this build `PURE_POSITIVE`
and `QUESTION` are enabled for the demo; `AutomationConfig.conservative()` turns
everything off. `eval/thresholds.py` reports eligibility from real approval data
but never flips the switch itself.

**Why.** "95%+ approval over a sufficient sample → eligible for auto-send" must
be evidence-driven and an explicit operator action, not a hardcoded graduation.

## D8 — Timing/variants use an injected seeded RNG, never `random`

**Decision.** `TimingEngine` and `VariantPool` take an `Rng` wrapper.
`Rng.derived(salt)` produces child streams so per-message decisions are
reproducible and independent.

**Why.** Determinism under test is a hard requirement (brief §14/§15). A global
`random` would make timing untestable and order-dependent.

## D9 — Customer text is untrusted data end-to-end

**Decision.** Inbound text is stored verbatim, never interpolated into a
provider "instruction" position, and prompt-injection markers force
`HUMAN_REVIEW` regardless of other signals. Generated drafts that echo customer
text still pass the full validator battery.

**Why.** The persona receives adversarial input by design. See
[SECURITY.md](SECURITY.md).

## D10 — Server-rendered UI, minimal JS

**Decision.** Jinja2 templates + a little vanilla `fetch`. No SPA, no build step.

**Why.** Brief §25: don't spend the project on a beautiful UI. The dashboard
exists to make the engineering legible — inbox, full thread, intents, entities,
state, client context, risk, recommended action, generated response, and the
decision trace, with Approve / Edit&Send / Reject / Suppress / Escalate.

## D11 — Store the message first, analyse atomically, fail safe

**Decision.** An inbound message is handled in two transactions under one lock:
(1) store the message, append `SMS_RECEIVED`, invalidate stale replies, supersede
stale reviews; (2) the whole analysis and decision. If (2) raises, it rolls back
and a third transaction opens a HIGH-risk review with a `PROCESSING_FAILED` event.

**Why.** Two properties matter more than anything else: a customer message is
never lost, and a half-made decision is never acted on. One transaction for
everything would lose the message on a crash; no transaction leaves partial events.

**Trade-off.** A crash costs a human review instead of an automated reply. That
is the intended direction of failure.

## D12 — Idempotency on the provider message id

**Decision.** `messages.provider_message_id` is unique. A repeated id returns the
stored decision (rebuilt from the trace) and changes nothing.

**Why.** SMS providers retry webhooks. Without this, a retry is a second inbound
message: it invalidates the reply to itself and opens a duplicate review.

## D13 — New inbound supersedes open reviews

**Decision.** A new customer message closes every open review on the conversation
(`SUPERSEDED`), the same way it invalidates queued replies. A superseded review
cannot be decided.

**Why.** The review draft answers the old message. Approving it after the
customer said something new is the same race as D4, through a human.

**Trade-off.** A reviewer may lose a half-written edit. Safety of the persona
wins; the new review carries the full thread.

## D14 — Human text is validated against the state, not against nothing

**Decision.** `outbound_guard(state)` derives forbidden actions from the current
conversation: opt-out forbids all outbound; suppression or an open issue forbids
every campaign action; a committed referral forbids a referral ask; anything
already presented cannot be repeated. Human-approved text, campaign text, and
follow-up text are validated against it.

**Why.** "Approval does not bypass the guardrails" was true for money terms and
URLs but not for campaign rules, which were enforced only through the pipeline's
own policy outcome. The Maya rule must hold no matter who wrote the text.

## D15 — Campaign steps are requests, decided by the engine

**Decision.** A campaign trigger (`POST /conversations/{id}/campaigns`, or a
`CAMPAIGN_TRIGGERED` event with a `kind`) is a request. `campaign_check` decides
it deterministically, and the copy comes from client-approved templates — not
the model. A `referral_ask` for a customer who already committed becomes a
`referral_link` built from the client template and *that* contact's id.

**Why.** This is the other half of both failure stories: the existing timer
system keeps firing, but it can no longer pitch Maya or re-pitch John, and the
link John receives is his.

**Trade-off.** Campaign copy is less varied than generated text. Accepted: it is
marketing copy the client approves once.

## D16 — Opt-out detection does not depend on the model

**Decision.** `policy/optout.py` runs on every inbound message. If it matches and
the model did not flag an opt-out, the pipeline sets the opt-out entity and the
policy's `R_STOP_OPTOUT` rule fires. With a real model, the rule classifier is
the fallback during an outage, and any provider error forces human review.

**Why.** Honouring STOP is a legal obligation. A model that misreads "thanks, but
please don't text me again" must not be able to cause a TCPA violation.

**Trade-off.** The backstop list is narrow (carrier keywords as the whole
message, explicit phrases) to avoid false opt-outs from "they didn't stop working".

## D17 — Send worker: claim, send, finalise; at-most-once delivery

**Decision.** The worker commits `SENDING` before the network call and records
the result after it. Transport errors retry with exponential backoff up to
`sending.max_attempts`. A message stuck in `SENDING` (worker died mid-call) is
marked `FAILED`, never retried automatically.

**Why.** The database lock must never be held across network I/O, two workers
must never send the same message, and a duplicate SMS is worse than a missing
one that a person can resend.

## D18 — Quiet hours in the recipient's timezone

**Decision.** The send window defaults to 08:15–20:30 and is evaluated in the
client's IANA timezone (`zoneinfo`), at planning time and again at send time.

**Why.** The previous 07:15 morning window was earlier than the US TCPA 8am
limit, and a fixed UTC offset is wrong for half of the year.

