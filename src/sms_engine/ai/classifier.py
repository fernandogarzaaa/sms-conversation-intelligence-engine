"""Multi-intent classifier: wraps a provider, parses + bounds its output.

The provider proposes intents. This class is responsible for turning whatever it
returns into a validated `Classification` — clamping confidences, dropping
unknown labels, de-duplicating, and recording observability metadata. Nothing
here decides anything.
"""

from __future__ import annotations

import time

from sms_engine.ai.provider import LLMProvider
from sms_engine.domain.classification import Classification, IntentPrediction
from sms_engine.domain.enums import IntentType


class MultiIntentClassifier:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def classify(self, text: str, state_summary: dict | None = None) -> Classification:
        t0 = time.perf_counter()
        resp = self.provider.classify_intents(text or "", state_summary or {})
        wall_ms = (time.perf_counter() - t0) * 1000.0

        preds: list[IntentPrediction] = []
        seen: set[IntentType] = set()
        for raw in resp.payload.get("intents", []):
            label = str(raw.get("type", "")).strip().upper()
            try:
                it = IntentType(label)
            except ValueError:
                continue  # provider hallucinated a label we don't support -> ignore
            if it in seen:
                continue
            seen.add(it)
            conf = raw.get("confidence", 0.0)
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.0
            conf = min(1.0, max(0.0, conf))
            preds.append(
                IntentPrediction(type=it, confidence=conf, rationale=str(raw.get("rationale", "")))
            )

        preds.sort(key=lambda p: -p.confidence)

        entities = {str(k): str(v) for k, v in resp.payload.get("entities", {}).items()}
        flags = resp.payload.get("flags", {})
        if flags:
            entities["_flags"] = ",".join(f"{k}={v}" for k, v in flags.items())

        return Classification(
            intents=preds,
            entities=entities,
            model_used=resp.model,
            latency_ms=resp.latency_ms or wall_ms,
            raw=resp.raw,
        )
