"""Entity extractor: wraps a provider, coerces its output into `Entities`."""

from __future__ import annotations

import time

from sms_engine.ai.provider import LLMProvider
from sms_engine.domain.classification import Classification
from sms_engine.domain.entities import Entities


class EntityExtractor:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def extract(self, text: str, classification: Classification) -> Entities:
        intents = [i.type.value for i in classification.intents]
        t0 = time.perf_counter()
        resp = self.provider.extract_entities(text or "", intents)
        wall_ms = (time.perf_counter() - t0) * 1000.0

        p = dict(resp.payload)
        # merge any entity hints the classifier surfaced (authoritative extractor wins)
        for k, v in (classification.entities or {}).items():
            if k.startswith("_"):
                continue
            p.setdefault(k, v)

        allowed = set(Entities.model_fields.keys())
        clean = {k: v for k, v in p.items() if k in allowed}
        clean["model_used"] = resp.model
        clean["latency_ms"] = resp.latency_ms or wall_ms
        clean["raw"] = resp.raw
        return Entities.model_validate(clean)
