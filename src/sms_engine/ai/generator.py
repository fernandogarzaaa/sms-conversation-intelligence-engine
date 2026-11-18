"""Constrained response generator: wraps a provider's draft.

Builds the `GenerateSpec` (customer message + history + state + client context +
allowed/forbidden actions + retrieved context) and returns the provider's draft
text and claimed actions. The draft is NOT trusted — it goes straight to the
deterministic validators next.

Fail-safe: a provider error yields an empty draft with `error` set. The
validator rejects empty text, so the pipeline retries once and then routes the
conversation to a human.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sms_engine.ai.provider import GenerateSpec, LLMProvider

log = logging.getLogger("sms_engine.ai")


@dataclass
class GeneratedDraft:
    text: str
    actions: list[str]
    model_used: str
    latency_ms: float
    raw: str
    error: str | None = None
    cost_usd: float = 0.0


class ResponseGenerator:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def generate(self, spec: GenerateSpec) -> GeneratedDraft:
        t0 = time.perf_counter()
        try:
            resp = self.provider.generate_reply(spec)
        except Exception as exc:  # noqa: BLE001 — fail safe on anything
            err = f"{type(exc).__name__}: {exc}"
            log.error("generator provider %s failed: %s", self.provider.name, err)
            m = getattr(self.provider, "model", None)
            return GeneratedDraft(
                text="",
                actions=[],
                model_used=f"{self.provider.name}:{m}" if m else self.provider.name,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                raw=getattr(exc, "raw", "") or "",
                error=err[:500],
            )
        wall_ms = (time.perf_counter() - t0) * 1000.0
        payload = resp.payload if isinstance(resp.payload, dict) else {}
        text = str(payload.get("text", "") or "").strip()
        acts = payload.get("actions", [])
        actions = [str(a) for a in acts] if isinstance(acts, (list, tuple)) else []
        return GeneratedDraft(
            text=text,
            actions=actions,
            model_used=resp.model,
            latency_ms=resp.latency_ms or wall_ms,
            raw=resp.raw,
            cost_usd=getattr(resp, "cost_usd", 0.0) or 0.0,
        )
