# SMS Conversation Intelligence Engine

> Independent project by Fernando Garza. Not affiliated with any company.
> All demo data is synthetic. No real customer data is included.

> The language model writes text. The system keeps the state, makes the
> decisions, controls what the persona can say, and stops when a person must
> take over.

This document uses ASD-STE100 Simplified Technical English. Sentences are short.
Each instruction is one sentence. The voice is active and the tense is present.

---

## Quick start

You need Python 3.11 or a later version. You need no API key and no network.

1. Install the project:

   ```bash
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```

2. Run the tests:

   ```bash
   pytest
   ```

3. Run the two failure cases in the console:

   ```bash
   sms-engine demo maya john
   ```

4. Start the web dashboard with demo data:

   ```bash
   sms-engine seed-db
   sms-engine serve
   ```

5. Open `http://127.0.0.1:8099` in a browser.

You can also use `make install`, `make test`, `make demo`, `make seed`, and
`make serve`. To run the service in a container, use `docker compose up --build`.

---

## 1. Purpose

The project is a conversation layer for post-job SMS. After a completed job, a
persona with the name "Agent" sends referral messages and review requests, and
answers the customer.

The project replaces three parts of a common design:

- fixed message templates
- fixed campaign timers
- a positive-or-negative sentiment test

The project puts these parts in their place:

- one conversation record for each customer, built from an event log
- a classifier that finds more than one intent in one message
- a rule engine that makes every decision
- replies that use the real conversation and approved facts
- a safety check on every outbound message, including human-approved text
- a campaign gate that sends a review request or a referral message only when it is safe
- a human review step
- a measurement loop

## 2. The two failures that the project fixes

### 2.1 Maya

The customer wrote that the crew was fantastic, but a wasp nest stopped the crew
from clearing the junk near the grill.

The old system marked the message as positive. It sent the referral message.
Later it sent a review request. The problem near the grill was still open.

This project does these steps:

1. It finds the mixed sentiment and the open issue.
2. It stops all campaign messages for the customer.
3. It sends the conversation to a human with a short draft reply.
4. It refuses a later review request or referral ask while the issue is open.
5. It refuses a human edit that adds a referral ask or a review request.

### 2.2 John

The customer wrote that this is actually John, that everything went great, and
that he will recommend the company.

The old system ignored the name correction. It repeated the referral terms. It
used the wrong personal referral link.

This project does these steps:

1. It reads one message as three intents.
2. It records the name change (`Olivia` to `John`) and marks the referral as committed.
3. It sends the reply to a human, because the name changed.
4. When the campaign system asks for a referral pitch, it sends John's own
   referral link instead. It builds the link from the client template and John's
   contact id. It does not send the pitch again.

## 3. How the system works

The system processes each inbound message in these steps:

1. **Receive the message.** The API or the SMS provider webhook delivers the
   message. If the provider message id was seen before, the system returns the
   earlier decision and does nothing more.
2. **Store the message first.** The system stores the message and adds an event.
   It cancels every queued reply and closes every open review that was written
   for an older message.
3. **Build the state.** The system builds the conversation record again from the
   full event log.
4. **Classify the message.** The classifier returns every intent that it finds,
   each with a confidence value.
5. **Extract the entities.** The system finds the issue, the location, the names,
   the dates, and the opt-out words. A rule-based opt-out check runs on every
   message. It does not depend on the model.
6. **Apply the policy.** The rule engine sets the route, the risk, the
   suppression, and the permitted actions.
7. **Retrieve the context.** The system marks an answer as grounded only when an
   approved FAQ entry covers it.
8. **Generate and validate.** The generator writes a short reply. The validators
   check money terms, links, claims, promises, length, opt-out rules, and
   campaign rules. A reply that fails goes back for one retry, then to a human.
9. **Route the result.** The system puts the reply in the timed send queue, or it
   opens a human review item.

If any step fails, the system rolls back the analysis, keeps the message, and
opens a high-risk review. An error never sends a message and never loses one.

The route is one of four values: automatic reply, human review, suppress, or
escalate. When the signals conflict, the system selects human review.

## 4. Sending

A background worker sends the queued messages. Before each send it does these
checks again:

- A newer customer message makes the reply stale. The worker cancels it.
- An opt-out cancels every message. A new suppression cancels every automatic message.
- An open issue cancels every campaign message and follow-up message.
- Outside the send window in the customer's local time, the worker moves the
  message to the next morning.

The default send window is 08:15 to 20:30 local time. The window uses the
client's IANA timezone, so daylight saving time is correct. No send time ends
in ":00" or ":30", and no two send times are the same.

The `log` transport records the message and sends nothing. The `twilio`
transport sends the message through Twilio. If the provider fails, the worker
tries again with a longer wait each time. After the last attempt, it marks the
message as failed.

## 5. Campaign steps

The campaign system asks for a step. The engine decides if the step is safe.

| Step | The engine refuses the step when |
|---|---|
| `review_request` | the contact opted out or is suppressed, an issue is open, a review is open, or the review link was already sent |
| `referral_ask` | the same conditions, or the referral terms were already sent. If the customer already said that they will refer, the engine sends `referral_link` instead |
| `referral_link` | the same conditions, or the link was already sent |

The campaign text comes from client-approved templates, not from the model. The
validators check it like every other message.

## 6. Global suppression

Suppression is a property of the customer. It is not a setting on one campaign
node.

When the customer has an open issue, a complaint, an opt-out, a wrong-number
message, or an escalation, the system stops all campaign actions for that
customer. An opt-out also stops all outbound messages. An opt-out is permanent.
A later suppression or a release request cannot change it.

When two issues are open at the same time, the system escalates the contact.
Staff can mark an issue as resolved on the conversation page.

## 7. Human review

The review page shows the decision first: the customer message, the reasons in
plain language, and the draft reply. The thread, the intents, the entities, and
the decision trace are below it.

The reviewer can send, edit and send, reject, suppress, or escalate. The page
checks the text while the reviewer types. The server checks the text again
against the current conversation state. Approval does not skip the checks.

The system stores every decision as evaluation data. If the reviewer changes the
text and selects "Send reply", the system records an edit, not an approval.

Follow-ups come from needs that the customer volunteered, for example "my
daughter is moving next spring". When a follow-up is due, the system writes a
draft. A person must approve each follow-up.

## 8. The language model

| `SMS_ENGINE_PROVIDER` | Model |
|---|---|
| `mock` (default) | A transparent rule engine. No network. All tests and the demo use it. |
| `openai` | OpenAI chat completions |
| `openrouter` | OpenRouter (many hosted models) |
| `local` | An OpenAI-compatible local server, for example Ollama, vLLM, or LM Studio |
| `anthropic` | The Anthropic Messages API |

Set `SMS_ENGINE_MODEL` and `SMS_ENGINE_API_KEY` for the selected provider. If the
model fails, the rule engine classifies the message and the message goes to a
human. The model never makes a decision.

To score a provider on the labelled data set:

```bash
SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=<model> sms-engine eval --mismatches
```

[docs/LLM_EVAL_RESULTS.md](docs/LLM_EVAL_RESULTS.md) gives the result for a live model.

## 9. Configuration

All settings are environment variables. [.env.example](.env.example) lists them.
The important ones are:

| Variable | Function |
|---|---|
| `SMS_ENGINE_DB` | The SQLite file. |
| `SMS_ENGINE_API_KEYS` | API keys for the JSON API. Set this in every shared environment. |
| `SMS_ENGINE_UI_PASSWORD` | The password for the review dashboard (HTTP Basic). |
| `SMS_ENGINE_PROVIDER`, `SMS_ENGINE_MODEL`, `SMS_ENGINE_API_KEY` | The language model. |
| `SMS_ENGINE_TRANSPORT` | `log` or `twilio`. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio credentials. The token also verifies the webhook signature. |
| `SMS_ENGINE_PUBLIC_URL` | The public URL that Twilio calls. It is necessary behind a proxy. |
| `SMS_ENGINE_WORKER_INTERVAL` | Seconds between worker passes in the server process. `0` disables the worker. |
| `SMS_ENGINE_AUTOMATION_CONFIG` | The automation file. |

The automation file controls which intents can send automatically, the
confidence floor, the escalation limit, the campaign review setting, and the
retry limits. Copy [config/automation.example.toml](config/automation.example.toml)
to `config/automation.toml`. Make sure that the file is correct:

```bash
sms-engine check-config config/automation.toml
```

## 10. Connect an SMS provider (Twilio)

1. Set `SMS_ENGINE_TRANSPORT=twilio`, `TWILIO_ACCOUNT_SID`, and `TWILIO_AUTH_TOKEN`.
2. Add the Twilio number to the client pack: `config.phone_numbers` and `config.sms_from_number`.
3. Set the messaging webhook of the number to `https://<your host>/webhooks/twilio/sms` (HTTP POST).
4. Set `SMS_ENGINE_PUBLIC_URL` to `https://<your host>`.
5. Run the worker: `SMS_ENGINE_WORKER_INTERVAL=15` in the server, or `sms-engine worker` in a separate process.

The webhook rejects a request with a wrong signature. It does not process the
same `MessageSid` two times. Replies go through the timed, validated send queue.
They never go back inline in the webhook response.

## 11. Command line

| Command | Function |
|---|---|
| `sms-engine serve` | Start the API and the dashboard (port 8099). |
| `sms-engine worker [--once]` | Send due messages and process due follow-ups. |
| `sms-engine demo [names]` | Run scenarios A to G in the console with decision traces. |
| `sms-engine seed-db` | Write a demo database that shows every flow. |
| `sms-engine eval [--mismatches]` | Score the configured model on the labelled data set. |
| `sms-engine ingest-client <file>` | Add a client context pack. |
| `sms-engine check-config [file]` | Make sure that an automation file is correct. |
| `sms-engine migrate` | Create or upgrade the database schema. |

## 12. API endpoints

The interactive API documentation is at `/docs`. When `SMS_ENGINE_API_KEYS` is
set, send a key in the `X-API-Key` header or as `Authorization: Bearer <key>`.

| Method and path | Function |
|---|---|
| `POST /messages/inbound` | Process one inbound SMS. Return the full decision. `provider_message_id` makes the call idempotent. |
| `POST /webhooks/twilio/sms` | The Twilio inbound webhook (signature-verified). |
| `POST /jobs/completed` | Record a completed job, before any SMS. |
| `POST /events` | Add an external event (`JOB_COMPLETED`, `CAMPAIGN_TRIGGERED`, `SUPPRESSION_ACTIVATED`, `ESCALATED`). |
| `POST /conversations/{id}/campaigns` | Request a campaign step. `dry_run` shows the decision and writes nothing. |
| `POST /process-due` | Send every message that is due. |
| `POST /process-followups` | Turn due follow-ups into review items. |
| `POST /worker/run` | Do one full worker pass. |
| `GET /outbound` | List recent planned and sent messages. |
| `GET /conversations` | List the conversations. Filters: `client_id`, `status`, `limit`, `offset`. |
| `GET /conversations/{id}` | Return one conversation with its state, messages, events, and outbound history. |
| `GET /conversations/{id}/state` | Return the conversation state. |
| `GET /conversations/{id}/trace` | Return the decision trace. |
| `POST /conversations/{id}/review` | Decide the newest open review for this conversation. |
| `POST /conversations/{id}/suppress` | Suppress this conversation and cancel its queue. |
| `POST /conversations/{id}/release` | Release the suppression. The system refuses this call after an opt-out. |
| `POST /conversations/{id}/issues/{issue_id}/resolve` | Mark an issue as resolved. |
| `GET /review-queue` | List the open reviews, highest risk first. |
| `GET /reviews/{id}` | Return the full review payload. |
| `POST /reviews/{id}/validate` | Check a reply text without a decision. |
| `POST /reviews/{id}/decision` | Submit a review decision. |
| `GET /metrics` | Return the operational metrics. |
| `GET /metrics/prometheus` | Return the metrics in Prometheus text format. |
| `GET /eval/classifier` | Return the classifier evaluation. |
| `GET /eval/automation-eligibility` | Return the automation eligibility per intent. |
| `GET /config/automation` | Return the active automation configuration. |
| `GET /clients`, `POST /clients`, `GET /clients/{id}` | List, add, and read client context packs. |
| `GET /health` | Return the service status. It needs no key. |

## 13. Repository layout

```
src/sms_engine/
  domain/          data models: state, intents, entities, events, policy, validation, trace
  policy/          the policy engine, the suppression and campaign gates, the opt-out backstop
  ai/              the LLMProvider interface, MockProvider, the HTTP model providers, prompts
  context/         client context ingestion and retrieval
  validation/      the deterministic validator battery
  application/     pipeline, review, campaigns, follow-ups, send worker, timing, settings
  infrastructure/  SQLite storage and migrations, SMS transports, clock, RNG, logging
  eval/            data set loader, classifier scoring, operational metrics, thresholds
  api/             the FastAPI application
  ui/              the server-rendered review dashboard
  cli.py           the sms-engine command
config/            automation.example.toml
data/clients/      one sample client context pack
data/seed/         125 synthetic labelled inbound messages, for evaluation only
docs/              LLM_EVAL_RESULTS.md
scripts/           demo.py, eval_llm.py, generate_seed_dataset.py
tests/             unit, integration, end-to-end, adversarial, API, CLI
```

## 14. Tests and evaluation

```bash
pytest                     # 225 tests, offline and deterministic
pytest --cov               # with coverage
ruff check src tests scripts
```

The classifier runs against 125 synthetic labelled messages:

| Model | Exact match | Salient recall | Macro F1 |
|---|---:|---:|---:|
| `MockProvider` (rules) | 0.976 | 0.987 | 0.992 |
| Live LLM through OpenRouter (see the results file) | 0.984 | 0.993 | 0.996 |

The live score is a development-set score: the prompt was tuned on the same
data. See [EVALUATION.md](EVALUATION.md) for the method.

## 15. Limits

- The storage is one SQLite file. It is correct for one server process. For more
  than one process, move the repositories to Postgres (see [HANDOFF.md](HANDOFF.md)).
- The retrieval uses word overlap. It does not use vector search.
- There is no CRM connection. The `POST /jobs/completed` and `POST /events`
  endpoints are the connection points.
- The seed data is synthetic. It is not real customer data.

## 16. Related documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — the layers and the data flow
- [DECISIONS.md](DECISIONS.md) — the design decisions and the reasons
- [EVALUATION.md](EVALUATION.md) — the measurement method
- [SECURITY.md](SECURITY.md) — the threat model and the controls
- [DEMO.md](DEMO.md) — a walk-through of the demonstration
- [HANDOFF.md](HANDOFF.md) — the production boundary and the integration plan
- [CHANGELOG.md](CHANGELOG.md) — the changes in each version
- [IMPLEMENTATION_LOG.md](IMPLEMENTATION_LOG.md) — the build log

## 17. License and author

Author: Fernando Garza. License: MIT.
