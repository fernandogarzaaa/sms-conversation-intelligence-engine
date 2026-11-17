# Demo walkthrough

Two ways to demo: **console** (fast, shows decision traces) and **web dashboard**
(shows the reviewer's view). Both are deterministic and offline.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows path shown
python -m pytest -q                                     # 93 tests, ~1s
```

## A. Console demo (2 minutes)

```bash
python scripts/demo.py
```

Runs scenarios A–G. For each step it prints the customer message, detected
intents, routing, the queued/held response, the **decision trace**, and each
check with PASS/FAIL. Ends with `35/35 scenario checks passed` and a metrics dump.

Run a subset:

```bash
python scripts/demo.py maya john
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
*NO review request*, *no automated message sent*.

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
python scripts/demo.py --seed-db
python -m uvicorn sms_engine.api.app:get_app --factory --port 8099
# open http://127.0.0.1:8099
```

- **Dashboard** (`/`): operational metrics, routing breakdown, the classifier
  evaluation table (per-intent P/R/F1 over the 125-row synthetic set), the
  automation-eligibility (learning-loop) table, the review queue, all
  conversations, and a "simulate an inbound SMS" box.
- **Inbox** (`/ui/inbox`): everything waiting for a human, risk-ordered.
- **Review** (`/ui/reviews/{id}`): the full reviewer view — conversation thread,
  current message, detected intents, extracted entities, conversation state,
  policy (routing / risk / forbidden / allowed + rule summary), client context,
  the generated draft with validator findings, and the decision trace. Buttons:
  **Approve**, **Edit & Send**, **Reject**, **Suppress**, **Escalate**. Every
  click is stored as evaluation data.
- **Conversation** (`/ui/conversations/{id}`): full thread, latest decision
  trace, the append-only event log, and the projected state (identity with
  previous name, facts with provenance, open issues, future intents/follow-ups),
  plus manual Suppress / Release.

### Live simulation

In the dashboard's "simulate an inbound SMS" box, try:

| Type this | You should see |
|-----------|----------------|
| `Everything went great, thanks!` | AUTO_REPLY, a planned response with a human-plausible `send_after` |
| `Do you guys move pianos?` | AUTO_REPLY, answer grounded in the pianos FAQ |
| `How much to haul a couch?` | HUMAN_REVIEW — pricing FAQ is not approved for auto-answer |
| `The crew was great but the shed is still full` | HUMAN_REVIEW + suppression, nothing queued |
| `Please stop texting me` | SUPPRESS, contact opted out; try a positive message after — still suppressed |
| `My sister is moving in the fall, might need help` | HUMAN_REVIEW + a scheduled follow-up task |

Then click **Process due send-queue** on the dashboard after the demo clock
passes a `send_after` to turn planned replies into sent outbound messages.

## C. API (for the engineering audience)

```bash
curl -s localhost:8099/messages/inbound -H 'content-type: application/json' \
  -d '{"client_id":"greenline","contact_id":"c1","text":"The crew was fantastic but there was a wasp nest so they could not clear the grill area."}' | python -m json.tool
```

Returns the full decision: intents, entities, policy outcome, retrieved context,
draft, validation, planned response (if any), review id, follow-ups,
`state_after`, and the rendered `decision_trace`.

Other endpoints: `GET /conversations/{id}`, `/conversations/{id}/state`,
`/conversations/{id}/trace`, `POST /conversations/{id}/review|suppress|release`,
`GET /review-queue`, `GET /metrics`, `GET /eval/classifier`,
`GET /eval/automation-eligibility`, `POST /events`.

## The one-line takeaway

> We don't make an LLM pretend to be human. We built a system that knows when it
> should speak, what it's allowed to say, what it knows, when it should stop, and
> when a human needs to take over.
