"""Authoritative entity extraction output."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Entities(BaseModel):
    # issue / complaint
    issue: str | None = None
    issue_location: str | None = None

    # sentiment targets
    sentiment_positive_target: str | None = None
    sentiment_negative_target: str | None = None

    # identity correction
    corrected_name: str | None = None
    stated_wrong_name: str | None = None

    # future intent
    future_person: str | None = None      # "daughter"
    future_service: str | None = None     # "moving"
    future_timeframe: str | None = None   # "spring"

    # referral / repeat business
    referral_mentioned: bool = False
    repeat_business_mentioned: bool = False

    # opt-out
    opt_out_phrase: str | None = None

    # wrong number
    wrong_number_phrase: str | None = None

    # questions
    question_text: str | None = None

    # links / money the customer themselves mentioned (rare, but tracked)
    urls: list[str] = Field(default_factory=list)
    money_terms: list[str] = Field(default_factory=list)

    model_used: str = "unknown"
    latency_ms: float = 0.0
    raw: str = ""

    def as_facts_dict(self) -> dict[str, str]:
        """Flatten the non-empty scalar fields for fact storage."""
        out: dict[str, str] = {}
        for k, v in self.model_dump().items():
            if k in {"model_used", "latency_ms", "raw", "urls", "money_terms"}:
                continue
            if isinstance(v, bool):
                if v:
                    out[k] = "true"
            elif v not in (None, ""):
                out[k] = str(v)
        return out
