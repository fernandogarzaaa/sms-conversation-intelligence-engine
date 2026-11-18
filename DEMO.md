# Demo walkthrough

Two ways to demo: **console** (fast, shows decision traces) and **web dashboard**
(shows the reviewer's view). Both are deterministic and offline.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                           # 230 tests, a few seconds
```

## A. Console demo (2 minutes)

```bash
sms-engine demo                  # same as: python scripts/demo.py
```

Runs scenarios A–G. For each step it prints the customer message, detected
intents, routing, the queued/held response, the **decision trace**, and each
check with PASS/FAIL. Ends with `35/35 scenario checks passed` and a metrics dump.

Run a subset:

```bash
sms-engine demo maya john
```

### What to point at

**Maya (scenario B).** One message, two intents:

```
CLASSIFIED: MIXED_SENTIMENT 0.96, OPEN_ISSUE 0.93
  v
ENTITY: issue=wasp nest so they couldn't clear the grill area, issue_location=grill area, sentiment_positive_target=crew
  v
POLICY: [R_OPEN_ISSUE] suppress campaign, human review. No referral ask, no review request, no promo.
  v
SUPPRESSION: ACTIVATED (OPEN_ISSUE)
  v
ROUTE: HUMAN_REVIEW
```

The old system sent a referral pitch here. This one suppresses every campaign
action, routes to a human, and queues nothing. Checks assert *NO referral ask*,
*NO review request*, *no automated message sent*. The draft for the reviewer
reads *"Thanks for the kind words about the crew. I've flagged the grill area for
the team to sort out."*

**John (scenario C).** One message, three intents + a state mutation:

```
CLASSIFIED: PURE_POSITIVE 0.96, IDENTITY_CORRECTION 0.95, REFERRAL_INTENT 0.90
  v
ENTITY: corrected_name=John
  v
POLICY: [R_IDENTITY_CORRECTION] update identity, human review.
        [R_NO_REPEAT_REFERRAL_ASK] customer already expressed referral intent: do not ask again.
```

`state_after.identity` is now `John` (previous_name `Olivia`), `referral_status`
is `COMMITTED`, and `referral_ask` is a forbidden action. The draft reads
*"Thanks John, I've updated your name on our end. …"* — it references the actual
conversation.

**Race condition (scenario G).** Step 1 queues a reply. Step 2 ("the crew left a
dresser in the driveway") shows:

```
QUEUE_INVALIDATION: invalidated 1 stale queued response(s)
```

then re-classifies, re-builds state (now has an open issue), and re-decides.

## B. Web dashboard (5 minutes)

```bash
sms-engine seed-db               # writes ./sms_engine.db with every flow
sms-engine serve                 # http://127.0.0.1:8099
```

The seeded database runs scenarios A–G and then plays the campaign system:

- **John**: the reviewer approves the name-correction reply; the campaign system
  then asks for a *referral ask*. John already committed, so the engine sends
  **his own referral link** (`https://ref.greenline.example/scen_john`) instead
  of the pitch. Open his conversation to see both messages and
  `Already sent: referral_link`.
- **Scenario A**: the reply goes out, then a review request.
- **Maya**: the campaign system asks for a review request. The dashboard's
  "Why campaign steps were blocked" table shows `contact is suppressed (OPEN_ISSUE)`.

Pages:

- **Dashboard** (`/`): what needs a human, operational metrics (including
  superseded reviews, campaign steps allowed/blocked, failed sends), routing
  breakdown, classifier evaluation, the learning-loop table, conversations, a
  "simulate an inbound SMS" box, and **Run worker pass**.
- **Inbox** (`/ui/inbox`): everything waiting for a human, risk-ordered —
  replies, campaign drafts (when review is required), and due follow-ups.
- **Review** (`/ui/reviews/{id}`): the decision first — what the customer wrote,
  *why this needs you* in plain language, and the reply to send with **live
  validation** as you type. Try adding "could you leave us a review?" to Maya's
  draft: the send button disables and says why. Thread, intents, entities,
  decision trace, and client context are in collapsible sections below.
- **Conversation** (`/ui/conversations/{id}`): thread, what is waiting to send,
  state, issues with **Mark resolved** (optionally releasing the suppression),
  **campaign step buttons** (the engine tells you if a step is blocked and why),
  facts with provenance, follow-ups, suppress / release, and the event log.

### Live simulation

In the dashboard's "simulate an inbound SMS" box, try:

| Type this | You should see |
|-----------|----------------|
| `Everything went great, thanks!` | AUTO_REPLY, a planned response with a human-plausible `send_after` |
| `Do you guys move pianos?` | AUTO_REPLY, answer grounded in the pianos FAQ |
| `How much to haul a couch?` | HUMAN_REVIEW — pricing FAQ is not approved for auto-answer |
| `The crew was great but the shed is still full` | HUMAN_REVIEW + suppression, nothing queued |
| …then on the same contact: `They also scratched the dresser` | ESCALATE — two open issues |
| `Please stop texting me` | SUPPRESS, contact opted out; try a positive message after — still suppressed |
| `My sister is moving in the fall, might need help` | HUMAN_REVIEW + a scheduled follow-up task |

Then click **Run worker pass** to send what is due (through the `log` transport;
nothing leaves the machine) and turn due follow-ups into reviews.

### Against a real model

```bash
SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=<model> SMS_ENGINE_API_KEY=<key> sms-engine serve
SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=<model> SMS_ENGINE_API_KEY=<key> sms-engine eval --mismatches
```

The decisions do not change shape: the model only proposes intents and writes
drafts; the same policy, validators, and review queue apply.

## C. API (for the engineering audience)

```bash
curl -s localhost:8099/messages/inbound -H 'content-type: application/json' \
  -d '{"client_id":"greenline","contact_id":"c1","text":"The crew was fantastic but there was a wasp nest so they could not clear the grill area."}' | python -m json.tool
```

Returns the full decision: intents, entities, policy outcome, retrieved context,
draft, validation, planned response (if any), review id, follow-ups,
`state_after`, and the rendered `decision_trace`.

Campaign step (try it on Maya's conversation id, then on a positive one):

```bash
curl -s localhost:8099/conversations/<id>/campaigns -H 'content-type: application/json' \
  -d '{"kind":"review_request","dry_run":true}' | python -m json.tool
```

Idempotency — send the same `provider_message_id` twice; the second response has
`"duplicate": true` and nothing new is stored.

The full endpoint list is in the README and at `/docs`.

## The one-line takeaway

> We don't make an LLM pretend to be human. We built a system that knows when it
> should speak, what it's allowed to say, what it knows, when it should stop, and
> when a human needs to take over.
