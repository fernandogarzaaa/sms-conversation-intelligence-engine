# Live LLM evaluation results

Date: 2026-11-17. Data: the 125-row **synthetic** seed set (`data/seed/inbound_messages.jsonl`) and the
scenarios A–G in `sms_engine/scenarios.py`. No real customer data was sent.

## Setup

| | |
|---|---|
| Provider | `OpenRouterProvider` (`sms_engine.ai.llm`) |
| Model | `nvidia/nemotron-3-ultra-550b-a55b:free` (OpenRouter free tier) |
| Why this model | The available OpenRouter key is a free-tier key with no credits: paid models (e.g. `anthropic/claude-haiku-5.5`, the factory default) returned HTTP 402. Free models are capped at 50 requests/day. |
| Classifier eval mode | batched (25/request) — same system prompt and label definitions as the live path, plus a batch addendum. 125 messages = 5 requests. The live pipeline always classifies one message per request. |
| Harness | `scripts/eval_llm.py classify --batch 25` → `sms_engine.eval.evaluator.evaluate_classifier` (the same scorer used for `MockProvider`). |
| Scenarios | `scripts/eval_llm.py scenarios` → the full pipeline, one request per call (classify → extract → policy → retrieve → generate → validate). |

Reproduce:

```bash
SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free SMS_ENGINE_LLM_TIMEOUT=240 \
  python scripts/eval_llm.py classify --batch 25 --out classify.json
SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free SMS_ENGINE_LLM_TIMEOUT=120 \
  python scripts/eval_llm.py scenarios --out scenarios.json
```

## Classifier quality

| Metric | Live LLM (final prompt) | Live LLM (first full run) | MockProvider (rules) |
|---|---:|---:|---:|
| n | 125 | 125 | 125 |
| Exact match | **0.984** | 0.968 | 0.976 |
| Salient recall | **0.993** | 0.980 | 0.987 |
| Macro-F1 | **0.996** | 0.981 | 0.992 |
| Mismatches | 2 | 4 | 3 |

Per intent (final prompt):

| Intent | Support | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| DEFERRAL | 3 | 1.000 | 1.000 | 1.000 |
| IDENTITY_CORRECTION | 8 | 1.000 | 1.000 | 1.000 |
| INLINE_REFERRAL | 2 | 1.000 | 1.000 | 1.000 |
| MIXED_SENTIMENT | 13 | 1.000 | 0.923 | 0.960 |
| NEGATIVE | 8 | 1.000 | 1.000 | 1.000 |
| OPEN_ISSUE | 16 | 1.000 | 1.000 | 1.000 |
| PURE_POSITIVE | 35 | 0.972 | 1.000 | 0.986 |
| QUESTION | 21 | 1.000 | 1.000 | 1.000 |
| REFERRAL_INTENT | 8 | 1.000 | 1.000 | 1.000 |
| REPEAT_BUSINESS_INTENT | 5 | 1.000 | 1.000 | 1.000 |
| STOP_OPTOUT | 8 | 1.000 | 1.000 | 1.000 |
| UNRELATED | 12 | 1.000 | 1.000 | 1.000 |
| VOLUNTEERED_FUTURE_INTENT | 6 | 1.000 | 1.000 | 1.000 |
| WRONG_NUMBER | 4 | 1.000 | 1.000 | 1.000 |

### Remaining mismatches

| id | message | expected | predicted |
|---|---|---|---|
| seed_0068 | "Great, I'll book you again when the basement is ready." | REPEAT_BUSINESS_INTENT | PURE_POSITIVE, REPEAT_BUSINESS_INTENT |
| seed_0078 | "Thanks the move was fine but honestly stop texting me now." | MIXED_SENTIMENT, STOP_OPTOUT | STOP_OPTOUT |

Both are in the safe direction or harmless. `seed_0068`: "Great, ..." read as praise. This is the same extra label the
rule engine emits, and `REPEAT_BUSINESS_INTENT` alone routes to human review anyway. `seed_0078`: the
"fine but honestly stop texting me" contrast is not tagged mixed. `STOP_OPTOUT` is still detected, so the
contact is suppressed exactly as expected.

### Prompt iteration (honest note)

The first full run (0.968 / 0.980 / 0.981) had 3 calm "work not finished" reports labelled `NEGATIVE`
instead of `OPEN_ISSUE` (safe direction: escalation instead of review). One edit changed the label
definitions: `NEGATIVE` now needs hostile or emotional wording, and a calm factual report of unfinished
work is `OPEN_ISSUE`. A one-line note on "will keep you in mind" was also added. No seed rows were pasted
into the prompt. The definitions were tuned while looking at mismatches on this same set, so the final
numbers are a **development-set** score. A held-out set (or real labelled review outcomes from the
learning loop) is needed for an unbiased figure.

An earlier attempt with a lower output-token cap returned truncated JSON for 3 of 5 batches (scored
0.408). The batch helper now raises on a missing `results` list, and the cap scales with batch size.
That run is not counted above.

## Scenarios A–G through the full pipeline (live model)

**35/35 checks passed** (28 model requests, $0.00 on the free tier).

| Step | Scenario | Intents | Routing | Checks | Reply (queued or draft for reviewer) | Pipeline latency |
|---|---|---|---|---|---|---:|
| A.1 | Pure positive | PURE_POSITIVE | AUTO_REPLY | 3/3 | Glad the crew took care of everything - thanks for the note. Thanks for taking a second to let us know. | 11.3 s |
| B.1 | Maya — mixed sentiment + open issue | MIXED_SENTIMENT, OPEN_ISSUE | HUMAN_REVIEW | 7/7 | Thanks for the feedback - glad the crew was fantastic. I've flagged the wasp nest issue for the team. | 26.6 s |
| C.1 | John — identity correction + positive + referral | IDENTITY_CORRECTION, PURE_POSITIVE, REFERRAL_INTENT | HUMAN_REVIEW | 6/6 | Thanks, John! Glad the crew took care of everything. Really appreciate you spreading the word. | 18.1 s |
| D.1 | Question | QUESTION | AUTO_REPLY | 4/4 | We move upright pianos with advance notice; grand pianos we refer to a specialty partner. Let me know the type and I'll get you a firm answer. Happy to help. | 10.9 s |
| D.2 | Question | QUESTION | HUMAN_REVIEW | 2/2 | Thanks for asking about piano moving. The office will follow up with you on pricing. | 10.8 s |
| E.1 | Opt out | STOP_OPTOUT | SUPPRESS | 4/4 | — (nothing sent) | 5.8 s |
| E.2 | Opt out | PURE_POSITIVE | SUPPRESS | 2/2 | — (nothing sent) | 11.1 s |
| F.1 | Volunteered future intent | VOLUNTEERED_FUTURE_INTENT | HUMAN_REVIEW | 3/3 | Thanks for letting us know — we'll be ready when your daughter's move comes up next spring. | 10.2 s |
| G.1 | Race condition | PURE_POSITIVE | AUTO_REPLY | 1/1 | Glad the crew took care of everything — thanks for the note! Good to hear. | 17.9 s |
| G.2 | Race condition | OPEN_ISSUE | HUMAN_REVIEW | 3/3 | Thanks for letting us know about the dresser left in the driveway — I've flagged that for the team! | 12.3 s |

Every reply drafted by the live model passed the deterministic validator battery on the first attempt.
The opt-out scenario (E) produced no outbound text at all.

## Latency and cost

| | Value |
|---|---|
| Classifier eval (batched) | 5 requests, 191.3 s wall, 10,149 input / 14,832 output tokens |
| Scenario pipeline latency | 5.8–26.6 s per inbound message (up to 3 sequential model calls; free reasoning model) |
| Cost | $0.00 (free tier). Paid example at list price: `claude-haiku-5-5` is $0.10 / $0.50 per MTok, so roughly 1.4k input + 0.2k output tokens per classification is ≈ $0.0002 per message per call. |

The free reasoning model is slow: most of its output tokens are hidden reasoning. For production, use
a paid low-latency model (factory defaults: `anthropic/claude-haiku-5.5` on OpenRouter,
`claude-haiku-5-5` direct). The timing engine already delays replies by minutes, so seconds of model
latency are not user-visible, but the pipeline call is synchronous.

## Takeaways

- With the shared label definitions, a general-purpose LLM matches the hand-tuned rule engine on this
  synthetic set, and it does so without per-phrase regexes.
- The decision layer did not change. All routing, suppression, and send/no-send outcomes come from
  the deterministic policy engine and validators. The model only proposed intents, entities, and
  draft text.
- Deterministic backstops stay on for real providers: carrier opt-out keywords (`STOP`, `UNSUBSCRIBE`, ...)
  always yield `STOP_OPTOUT`, and the prompt-injection regex always sets the injection flag.
- On any provider failure (timeout, HTTP error, unparseable output) the wrappers return an empty
  classification / entities / draft, and the policy routes to human review. See `tests/unit/test_llm_provider.py`.
