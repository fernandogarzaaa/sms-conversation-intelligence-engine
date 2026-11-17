# SMS Conversation Intelligence Engine (Prototype)

> Independent prototype by Fernando Garza. Not affiliated with any company.
> All demo data is synthetic. No real customer data, no real SMS provider,
> no real CRM, and no real language model are connected.

> The language model writes text. The system keeps the state, makes the
> decisions, controls what the persona can say, and stops when a person must
> take over.

This document uses ASD-STE100 Simplified Technical English. Sentences are short.
Each instruction is one sentence. The voice is active and the tense is present.

---

## Quick start

You need Python 3.11 or a later version. You need no API key and no network.

1. Install the project:

   ```powershell
   py -m venv .venv
   .venv\Scripts\python.exe -m pip install -e ".[dev]"
   ```

2. Run the tests:

   ```powershell
   .venv\Scripts\python.exe -m pytest
   ```

3. Run the two failure cases:

   ```powershell
   .venv\Scripts\python.exe scripts\demo.py maya john
   ```

4. Start the web dashboard:

   ```powershell
   .venv\Scripts\python.exe scripts\demo.py --seed-db
   .venv\Scripts\python.exe -m uvicorn sms_engine.api.app:get_app --factory --port 8099
   ```

5. Open `http://127.0.0.1:8099` in a browser.

Section 12 explains the dashboard. Section 9 to section 13 give the full detail.

---

## 1. Purpose

This repository is a working prototype. It shows a new conversation layer for the
the SMS system.

The SMS system sends referral messages and review requests after a
completed job. A persona with the name "Agent" sends the messages.

The prototype replaces three parts of the current design:

- fixed message templates
- fixed campaign timers
- a positive-or-negative sentiment test

The prototype puts these parts in their place:

- one conversation record for each customer
- a classifier that finds more than one intent in one message
- a rule engine that makes every decision
- replies that use the real conversation and approved facts
- a safety check on every reply
- a human review step
- a measurement loop

## 2. What the prototype is and is not

The prototype **includes** these parts:

- the conversation state model
- the multi-intent classifier and the entity extractor
- the policy engine and the suppression engine
- the context layer and the retrieval step
- the reply generator
- the deterministic validators
- the timing engine and the queue-invalidation logic
- the human review service and a small web dashboard
- the evaluation loop and the learning loop
- an HTTP API
- automated tests

The prototype **does not include** these parts:

- a connection to a real SMS provider
- a connection to the CRM
- a connection to the current campaign system or timer system
- a production database
- a large language model

The model layer is `MockProvider`. `MockProvider` is a set of text rules. It is
not a language model. The interfaces for a real model exist, but they are not
connected. An inbound SMS is an HTTP request. An outbound SMS is a row in a local
database.

For the full list of limits and the plan to connect the real systems, read
[HANDOFF.md](HANDOFF.md).

## 3. The problem

The current flow has this shape:

```
SMS provider
   |
campaign / timer
   |
template
   |
customer
```

The flow has these problems:

- The timers are fixed. They do not react to the customer.
- The sentiment test gives only "positive" or "negative".
- The system keeps almost no memory of the conversation.
- Each campaign node acts alone.

Two real failures show the effect.

### 3.1 Case 1 — Maya

The customer wrote that the crew was fantastic, but a wasp nest stopped the crew
from clearing the junk near the grill.

The current system marked the message as positive. It sent the referral message.
Later it sent a review request. The problem near the grill was still open.

The correct behavior is:

1. Find the mixed sentiment.
2. Find the open issue.
3. Stop all campaign messages.
4. Send the conversation to a human.

### 3.2 Case 2 — John

The customer wrote that this is actually John, that everything went great, and
that he will recommend the company.

The current system did three things wrong:

1. It ignored the name correction.
2. It repeated referral terms that the customer already accepted.
3. It used the wrong personal referral link.

The correct behavior is to read one message as several intents, to update the
record, and to reply with context.

## 4. How the system works

The prototype processes each inbound message in nine steps:

1. **Receive the event.** The API gets the message and stores it. The system adds
   an event to the log.
2. **Build the state.** The system builds the conversation record again from the
   full event log. A late message cannot change an old record.
3. **Classify the message.** The classifier returns every intent that it finds.
   Each intent has a confidence value.
4. **Extract the entities.** The system finds the issue, the location, the names,
   the dates, and the opt-out words.
5. **Apply the policy.** The rule engine reads the intents, the entities, and the
   earlier state. It sets the route, the risk, the suppression, and the permitted
   actions.
6. **Retrieve the context.** The system selects the parts of the client context
   pack that match the message. It marks an answer as grounded only when an
   approved FAQ entry covers it.
7. **Generate the reply.** The generator runs only on the automatic route. It
   writes a short reply that uses the conversation.
8. **Validate the reply.** The validators check the money terms, the links, the
   claims, the promises, the length, and the opt-out rules. A reply that fails
   goes back for one retry. If it fails again, it goes to a human.
9. **Route the result.** The system puts the reply in the timed send queue, or it
   opens a human review item.

The route is one of four values: automatic reply, human review, suppress, or
escalate. When the signals conflict, the system selects human review.

## 5. Global suppression

Suppression is a property of the customer. It is not a setting on one campaign
node.

When the customer has an open issue, a complaint, an opt-out, a wrong-number
message, or an escalation, the system stops all campaign actions for that
customer. An opt-out also stops all outbound messages. An opt-out is permanent.
The system does not release it.

## 6. Timing and the message race

The timing engine is separate from the model. A seeded random generator makes it
repeatable in tests.

The rules are:

- A short message gets a short delay.
- A complex message gets a longer delay.
- A message that arrives at night waits for the morning window.
- No send time ends in ":00" or ":30".
- No two send times are the same.

The system also handles this race:

1. Message 1 arrives. The system queues a reply for a later time.
2. Message 2 arrives before the send time.
3. The system marks the queued reply as not valid.
4. The system builds the state again, classifies again, and makes a new decision.

The send worker checks the version again at send time. A stale reply cannot reach
the customer.

## 7. Human review and learning

A human review item shows the reviewer these fields: the customer, the current
message, the full thread, the intents, the entities, the state, the client
context, the risk, the recommended action, the draft reply, and the decision
trace.

The reviewer can approve, edit and send, reject, suppress, or escalate. The
system validates human-edited text with the same checks. Approval does not skip
the checks.

The system stores every decision. Each record holds the model classification, the
model confidence, the draft reply, the human action, the human edits, and the
final reply. The learning loop reads these records. For each intent, it reports
whether the approval rate over a minimum sample is high enough for automatic
send. The operator must still turn on automatic send by hand.

## 8. Repository layout

```
src/sms_engine/
  domain/          data models: state, intents, entities, events, policy, validation, trace
  policy/          the deterministic policy engine and suppression engine
  ai/              the LLMProvider interface, MockProvider, classifier, extractor, generator
  context/         client context ingestion and retrieval
  validation/      the deterministic validator battery
  application/     the pipeline, the state machine, review, timing, queue, metrics
  infrastructure/  SQLite storage, clock, seeded RNG, id factory
  eval/            dataset loader, classifier scoring, operational metrics, thresholds
  api/             the FastAPI application
  ui/              the server-rendered review dashboard
data/
  clients/         one sample client context pack
  seed/            125 synthetic labelled inbound messages, for evaluation only
scripts/           demo.py, generate_seed_dataset.py
tests/             unit, integration, end-to-end, adversarial, API
```

## 9. How to install

You need Python 3.11 or a later version.

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The prototype needs no API key. The prototype needs no network.

## 10. How to run the tests

```powershell
.venv\Scripts\python.exe -m pytest
```

The result is `93 passed`. The tests are deterministic and offline.

## 11. How to run the demonstration

To run all scenarios with the decision trace for each step:

```powershell
.venv\Scripts\python.exe scripts\demo.py
```

To run only the two failure cases:

```powershell
.venv\Scripts\python.exe scripts\demo.py maya john
```

## 12. How to start the web dashboard

1. Create the demo database:

   ```powershell
   .venv\Scripts\python.exe scripts\demo.py --seed-db
   ```

2. Start the server:

   ```powershell
   .venv\Scripts\python.exe -m uvicorn sms_engine.api.app:get_app --factory --port 8099
   ```

3. Open `http://127.0.0.1:8099` in a browser.

The dashboard has no authentication. Use it on your machine only.

## 13. API endpoints

| Method and path | Function |
|---|---|
| `POST /messages/inbound` | Process one inbound SMS. Return the full decision. |
| `POST /events` | Add a job or campaign event. |
| `POST /process-due` | Send every reply that is due. |
| `GET /conversations` | List the conversations. |
| `GET /conversations/{id}` | Return one conversation with its state, messages, and events. |
| `GET /conversations/{id}/state` | Return the conversation state. |
| `GET /conversations/{id}/trace` | Return the decision trace. |
| `POST /conversations/{id}/review` | Decide the open review for this conversation. |
| `POST /conversations/{id}/suppress` | Suppress this conversation. |
| `POST /conversations/{id}/release` | Release the suppression. The system refuses this call after an opt-out. |
| `GET /review-queue` | List the open reviews, highest risk first. |
| `GET /reviews/{id}` | Return the full review payload. |
| `POST /reviews/{id}/decision` | Submit a review decision. |
| `GET /metrics` | Return the operational metrics. |
| `GET /eval/classifier` | Return the classifier evaluation. |
| `GET /eval/automation-eligibility` | Return the automation eligibility per intent. |
| `GET /clients` | List the client context packs. |
| `POST /clients` | Add a client context pack. |
| `GET /health` | Return the service status. |

## 14. Evaluation results

The classifier runs against 125 synthetic labelled messages. The current scores
are:

- exact match: 0.976
- salient recall: 0.987
- macro F1: 0.992

`MockProvider` produces these scores. `MockProvider` is a rule engine. A real
model gets a new score from the same tool. See [EVALUATION.md](EVALUATION.md) for
the method.

## 15. Limits

- The storage is one SQLite file with one shared connection.
- The API and the dashboard have no authentication.
- There is no SMS transport and no CRM link.
- The retrieval uses word overlap. It does not use vector search.
- The seed data is synthetic. It is not real customer data.

## 16. Related documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — the layers and the data flow
- [DECISIONS.md](DECISIONS.md) — the design decisions and the reasons
- [EVALUATION.md](EVALUATION.md) — the measurement method
- [SECURITY.md](SECURITY.md) — the threat model and the controls
- [DEMO.md](DEMO.md) — a walk-through of the demonstration
- [HANDOFF.md](HANDOFF.md) — the prototype boundary and the integration plan
- [IMPLEMENTATION_LOG.md](IMPLEMENTATION_LOG.md) — the build log

## 17. License and author

Author: Fernando Garza.

This is a prototype for evaluation. It is not production software.
