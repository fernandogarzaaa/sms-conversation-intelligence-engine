"""Human-readable decision trace for operator trust."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TraceStep(BaseModel):
    stage: str          # INBOUND, CLASSIFIED, ENTITY, POLICY, SUPPRESSION, ROUTE, ...
    detail: str
    data: dict = Field(default_factory=dict)


class DecisionTrace(BaseModel):
    conversation_id: str
    message_id: str
    created_at: datetime
    steps: list[TraceStep] = Field(default_factory=list)

    # full observability record (section 20 of the brief)
    message_text: str = ""
    classification: dict = Field(default_factory=dict)
    entities: dict = Field(default_factory=dict)
    state_before: dict = Field(default_factory=dict)
    state_after: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)
    retrieved_context: dict = Field(default_factory=dict)
    response_text: str | None = None
    validation: dict = Field(default_factory=dict)
    routing: str = ""
    latency_ms: float = 0.0
    model_used: str = "unknown"

    def add(self, stage: str, detail: str, **data) -> None:
        self.steps.append(TraceStep(stage=stage, detail=detail, data=data))

    def render(self, arrow: str = "v") -> str:
        """ASCII vertical decision trace (terminal-safe)."""
        blocks = [f"{s.stage}: {s.detail}" if s.detail else s.stage for s in self.steps]
        sep = f"\n  {arrow}\n"
        return sep.join(blocks)
