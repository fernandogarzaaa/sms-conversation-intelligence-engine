"""Multi-intent classifier: wraps a provider, parses + bounds its output.

The provider proposes intents. This class is responsible for turning whatever it
returns into a validated `Classification` — clamping confidences, dropping
unknown labels, de-duplicating, and recording observability metadata. Nothing
here decides anything.

Fail-safe: if the provider raises (network, timeout, bad output, anything), the
classifier returns an EMPTY classification with `error` set. The policy engine's
R_NO_INTENT rule then routes the message to human review. An optional
`fallback` provider (e.g. MockProvider) is tried first if one is configured.
"""

from __future__ import annotations

import logging
import time

from sms_engine.ai.provider import LLMProvider
from sms_engine.domain.classification import Classification, IntentPrediction
from sms_engine.domain.enums import IntentType

log = logging.getLogger("sms_engine.ai")


def _model_name(provider: LLMProvider) -> str:
    m = getattr(provider, "model", None)
    return f"{provider.name}:{m}" if m else provider.name


class MultiIntentClassifier:
    def __init__(self, provider: LLMProvider, *, fallback: LLMProvider | None = None) -> None:
        self.provider = provider
        self.fallback = fallback

    def classify(self, text: str, state_summary: dict | None = None) -> Classification:
        t0 = time.perf_counter()
        try:
            resp = self.provider.classify_intents(text or "", state_summary or {})
            out = self._parse(resp, t0)
        except Exception as exc:  # noqa: BLE001 — fail safe on anything
            err = f"{type(exc).__name__}: {exc}"
            log.error("classifier provider %s failed: %s", self.provider.name, err)
            if self.fallback is not None:
                try:
                    out = self._parse(self.fallback.classify_intents(text or "", state_summary or {}), t0)
                    out.error = f"primary failed, used fallback {self.fallback.name}: {err}"
                    out.model_used = f"{self.fallback.name}(fallback)"
                    return out
                except Exception as exc2:  # noqa: BLE001
                    err += f"; fallback failed: {type(exc2).__name__}: {exc2}"
                    log.error("classifier fallback %s failed: %s", self.fallback.name, exc2)
            return Classification(
                intents=[],
                model_used=_model_name(self.provider),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                raw=getattr(exc, "raw", "") or "",
                error=err[:500],
            )
        return out

    @staticmethod
    def _parse(resp, t0: float) -> Classification:
        wall_ms = (time.perf_counter() - t0) * 1000.0
        payload = resp.payload if isinstance(resp.payload, dict) else {}

        preds: list[IntentPrediction] = []
        seen: set[IntentType] = set()
        raw_intents = payload.get("intents", [])
        for raw in raw_intents if isinstance(raw_intents, list) else []:
            if not isinstance(raw, dict):
                continue
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
            if conf != conf:  # NaN
                conf = 0.0
            conf = min(1.0, max(0.0, conf))
            preds.append(
                IntentPrediction(type=it, confidence=conf, rationale=str(raw.get("rationale", ""))[:300])
            )

        preds.sort(key=lambda p: -p.confidence)

        ents = payload.get("entities", {})
        entities = {str(k): str(v) for k, v in (ents.items() if isinstance(ents, dict) else [])}
        flags = payload.get("flags", {})
        if isinstance(flags, dict) and flags:
            entities["_flags"] = ",".join(f"{k}={v}" for k, v in flags.items())

        return Classification(
            intents=preds,
            entities=entities,
            model_used=resp.model,
            latency_ms=resp.latency_ms or wall_ms,
            raw=resp.raw,
        )
