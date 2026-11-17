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

## D2 — Deterministic MockProvider as the only provider in this build

**Decision.** `MockProvider` is a transparent keyword/regex classifier +
template generator. `OpenAIProvider` / `LocalProvider` exist only as interface
stubs that raise `NotImplementedError`.

**Why.** The deliverable is the *system*. A deterministic provider makes the
whole pipeline testable end-to-end with no network, no keys, no flakiness, and
lets the demo be reproducible. The `LLMProvider` seam is the real artifact — a
production model drops in without changing state/policy/validation/review.

**Trade-off.** The classifier's language understanding is shallow (it will miss
paraphrases outside its lexicons). Accepted for a prototype; the eval harness
measures exactly this and a real model is a one-class change.

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
