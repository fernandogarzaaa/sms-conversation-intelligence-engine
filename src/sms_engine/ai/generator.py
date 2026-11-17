"""Constrained response generator: wraps a provider's draft.

Builds the `GenerateSpec` (customer message + history + state + client context +
allowed/forbidden actions + retrieved context) and returns the provider's draft
text and claimed actions. The draft is NOT trusted — it goes straight to the
deterministic validators next.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sms_engine.ai.provider import GenerateSpec, LLMProvider


@dataclass
class GeneratedDraft:
    text: str
    actions: list[str]
    model_used: str
    latency_ms: float
    raw: str


class ResponseGenerator:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def generate(self, spec: GenerateSpec) -> GeneratedDraft:
        t0 = time.perf_counter()
        resp = self.provider.generate_reply(spec)
        wall_ms = (time.perf_counter() - t0) * 1000.0
        text = str(resp.payload.get("text", "")).strip()
        actions = [str(a) for a in resp.payload.get("actions", [])]
        return GeneratedDraft(
            text=text,
            actions=actions,
            model_used=resp.model,
            latency_ms=resp.latency_ms or wall_ms,
            raw=resp.raw,
        )
