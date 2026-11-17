"""Entity extractor: wraps a provider, coerces its output into `Entities`.

Fail-safe: a provider error yields empty `Entities` (with `error` set), and a
field the provider returned with the wrong type is dropped individually rather
than discarding the whole extraction.
"""

from __future__ import annotations

import logging
import time

from pydantic import ValidationError

from sms_engine.ai.provider import LLMProvider
from sms_engine.domain.classification import Classification
from sms_engine.domain.entities import Entities

log = logging.getLogger("sms_engine.ai")

_META = {"model_used", "latency_ms", "raw", "error"}


class EntityExtractor:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def extract(self, text: str, classification: Classification) -> Entities:
        intents = [i.type.value for i in classification.intents]
        t0 = time.perf_counter()
        try:
            resp = self.provider.extract_entities(text or "", intents)
        except Exception as exc:  # noqa: BLE001 — fail safe on anything
            err = f"{type(exc).__name__}: {exc}"
            log.error("extractor provider %s failed: %s", self.provider.name, err)
            m = getattr(self.provider, "model", None)
            return Entities(
                model_used=f"{self.provider.name}:{m}" if m else self.provider.name,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                raw=f"ERROR: {err}"[:500],
            )
        wall_ms = (time.perf_counter() - t0) * 1000.0

        p = dict(resp.payload) if isinstance(resp.payload, dict) else {}
        # merge any entity hints the classifier surfaced (authoritative extractor wins)
        for k, v in (classification.entities or {}).items():
            if k.startswith("_"):
                continue
            p.setdefault(k, v)

        allowed = set(Entities.model_fields.keys()) - _META
        clean = {k: v for k, v in p.items() if k in allowed}
        meta = {"model_used": resp.model, "latency_ms": resp.latency_ms or wall_ms, "raw": resp.raw}
        try:
            return Entities.model_validate({**clean, **meta})
        except ValidationError:
            good = {}
            for k, v in clean.items():
                try:
                    Entities.model_validate({k: v})
                    good[k] = v
                except ValidationError:
                    log.warning("extractor dropped malformed field %s=%r", k, v)
            return Entities.model_validate({**good, **meta})
