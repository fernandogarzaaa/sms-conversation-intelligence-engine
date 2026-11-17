"""Score a real LLM provider with the same harness that scores MockProvider.

    SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=<model> python scripts/eval_llm.py classify
    SMS_ENGINE_PROVIDER=openrouter SMS_ENGINE_MODEL=<model> python scripts/eval_llm.py scenarios
    ... python scripts/eval_llm.py classify --batch 25 --out results.json

`classify` runs `sms_engine.eval.evaluator.evaluate_classifier` over the 125-row
synthetic seed set. With `--batch N` the provider classifies N messages per
request (same label definitions; useful on rate-limited free tiers), otherwise
one request per message exactly like the live pipeline.

`scenarios` runs scenarios A-G (sms_engine.scenarios) through the full pipeline
(classify -> extract -> policy -> retrieve -> generate -> validate) with the
real provider and reports which checks pass.

This script calls the network. It is not part of the test suite.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from sms_engine.ai.classifier import MultiIntentClassifier
from sms_engine.ai.factory import provider_from_env
from sms_engine.ai.provider import GenerateSpec, LLMProvider, ProviderResponse
from sms_engine.eval.dataset import load_seed
from sms_engine.eval.evaluator import evaluate_classifier


class _Precomputed(LLMProvider):
    """Serves batch-classified payloads to the per-message eval harness."""

    def __init__(self, name: str, table: dict[str, dict]) -> None:
        self.name = name
        self.table = table

    def classify_intents(self, text, state_summary):
        return ProviderResponse(payload=self.table.get(text, {"intents": []}), raw="", model=self.name)

    def extract_entities(self, text, intents):  # pragma: no cover
        return ProviderResponse(payload={}, raw="", model=self.name)

    def generate_reply(self, spec: GenerateSpec):  # pragma: no cover
        return ProviderResponse(payload={"text": "", "actions": []}, raw="", model=self.name)


def run_classify(provider, batch: int) -> dict:
    rows = load_seed()
    t0 = time.perf_counter()
    if batch and batch > 1:
        table: dict[str, dict] = {}
        for i in range(0, len(rows), batch):
            chunk = [r.text for r in rows[i:i + batch]]
            for attempt in range(4):
                try:
                    outs = provider.classify_batch(chunk)
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  chunk {i // batch} attempt {attempt + 1} failed: {exc}", file=sys.stderr)
                    time.sleep(15 * (attempt + 1))
            else:
                outs = [{"intents": []} for _ in chunk]
            table.update(dict(zip(chunk, outs, strict=True)))
            print(f"  classified {min(i + batch, len(rows))}/{len(rows)}", file=sys.stderr)
        clf = MultiIntentClassifier(_Precomputed(f"{provider.name}:{provider.model}", table))
        mode = f"batched ({batch}/request)"
    else:
        clf = MultiIntentClassifier(provider)
        mode = "one message per request"
    ev = evaluate_classifier(clf, rows)
    wall = time.perf_counter() - t0
    out = ev.summary()
    out.update({
        "mode": mode,
        "wall_seconds": round(wall, 1),
        "requests": getattr(provider, "calls", None),
        "cost_usd": round(getattr(provider, "total_cost_usd", 0.0), 6),
        "input_tokens": getattr(provider, "total_input_tokens", None),
        "output_tokens": getattr(provider, "total_output_tokens", None),
        "mismatches": ev.mismatches,
    })
    return out


def run_scenarios(provider) -> dict:
    from sms_engine.application.pipeline import InboundPipeline
    from sms_engine.application.wiring import deterministic_engine
    from sms_engine.context.packs import default_context
    from sms_engine.scenarios import SCENARIOS, run_scenario

    engine = deterministic_engine(datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc), seed=42, provider=provider)
    engine.clients.upsert(default_context())
    pipe = InboundPipeline(engine)
    report = []
    for scn in SCENARIOS:
        for s in run_scenario(pipe, scn):
            r = s["result"]
            report.append({
                "scenario": scn.key, "title": scn.title, "step": s["step"], "text": s["text"],
                "intents": s["intents"], "routing": s["routing"], "checks": s["checks"],
                "classification_error": r.classification.error,
                "latency_ms": round(r.latency_ms),
                "response": (r.planned_response.text if r.planned_response else r.draft_text),
                "validation_passed": r.validation.passed if r.validation else None,
                "validation_rejects": [f.message for f in r.validation.rejects] if r.validation else [],
            })
            print(f"  {scn.key}.{s['step']} {s['routing']} {sum(s['checks'].values())}/{len(s['checks'])}", file=sys.stderr)
    total = sum(len(x["checks"]) for x in report)
    passed = sum(sum(x["checks"].values()) for x in report)
    return {
        "checks_passed": passed, "checks_total": total, "steps": report,
        "requests": getattr(provider, "calls", None),
        "cost_usd": round(getattr(provider, "total_cost_usd", 0.0), 6),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["classify", "scenarios"])
    ap.add_argument("--batch", type=int, default=0, help="messages per request for `classify` (0 = one each)")
    ap.add_argument("--out", help="write the JSON report here")
    a = ap.parse_args(argv)
    provider = provider_from_env()
    res = run_classify(provider, a.batch) if a.mode == "classify" else run_scenarios(provider)
    res["provider"] = provider.name
    res["model"] = getattr(provider, "model", provider.name)
    text = json.dumps(res, indent=2, default=str)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
