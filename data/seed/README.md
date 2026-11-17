# Synthetic evaluation data

**Everything in this folder is invented for evaluation. It is NOT real the business
customer data and makes no claims about real the business customers.**

`inbound_messages.jsonl` — 125 labeled inbound SMS bodies, one JSON object per
line:

```json
{"id": "seed_0001", "text": "...", "expected_intents": ["PURE_POSITIVE"], "notes": "", "synthetic": true}
```

Coverage: pure positive, mixed sentiment, open issues, complaints/negative,
questions (grounded and pricing/ungrounded), identity corrections, volunteered
future intent, referral / inline referral, repeat-business intent, deferral,
opt-out (including opt-out hidden inside another message), wrong number,
unrelated, prompt-injection attempts, contradictory/ambiguous, and one long
multi-signal message.

Regenerate deterministically with:

```bash
python scripts/generate_seed_dataset.py
```

`expected_intents` is a labelling of the *salient* intents for scoring recall; the
classifier may additionally return lower-signal intents (e.g. `REPEAT_BUSINESS_INTENT`
alongside `VOLUNTEERED_FUTURE_INTENT`) without being counted wrong. See
`EVALUATION.md` for how the metrics are computed.
